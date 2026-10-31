"""
Opt-in crop detection for the experimental matching pipeline: image embeddings of a copy detection model
(SSCD, https://github.com/facebookresearch/sscd-copy-detection, MIT license) run with onnxruntime on the CPU,
compared by cosine similarity. See the "Crop detection (embeddings)" section of docs/matching-prototype.md.

Perceptual hashes do not survive crops (a 10 % crop is already at pHash distance 8 .. 12). This stage only
looks at pairs the hash stages did not match and tags them as `MatchReason.EMBEDDING` with a low confidence,
so they never qualify for automatic actions. Groups containing a crop and its source always go to review,
`DuplicateGroup.crops` tells the prioritization which member is the crop (preferred by default, see
`rank_crops_first`).

`onnxruntime` is an optional dependency (`pip install py-image-dedup[embeddings]`), it is only imported when an
`OnnxEmbeddingExtractor` is created. The model is not bundled: it is downloaded on demand into a cache directory
(SHA-256 verified) or given by path, e.g. exported with `tools/export_sscd_onnx.py`.
"""
import hashlib
import logging
import os
import sqlite3
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Protocol, Sequence, Tuple

import numpy as np
from PIL import Image

from py_image_dedup.matching.decoding import decode_preview
from py_image_dedup.matching.grouping import burst_note, variant_reason
from py_image_dedup.matching.models import (
    CropRelation, DuplicateGroup, ImageSignature, Match, MatchReason, MatchingThresholds, VariantKind,
)

LOGGER = logging.getLogger(__name__)

# embedding matches are scored in [MIN, MAX] by similarity: always below any auto-act threshold
CONFIDENCE_EMBEDDING_MIN = 0.5
CONFIDENCE_EMBEDDING_MAX = 0.75
DEFAULT_MIN_SIMILARITY = 0.6

NOTE_EMBEDDING = "crop/embedding similarity {similarity:.2f}"

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass(frozen=True)
class ModelSource:
    """
    Where to download an ONNX embedding model from.
    """
    name: str
    url: str
    sha256: str
    size_bytes: int
    # edge length of the (square) model input
    image_size: int = 320


# SSCD disc_mixup (ResNet-50, 512 dimensions), ONNX export of Meta's official TorchScript release
# (sha256 9f26bd4c...6db56, MIT license) published on Hugging Face by a third party, pinned to a revision.
# Its embeddings were verified to be identical (max abs difference 1e-7) to an own export with
# tools/export_sscd_onnx.py. Fixed input shape 1x3x320x320. For a self-made export (dynamic shapes) use
# tools/export_sscd_onnx.py and pass the file as model path.
SSCD_DISC_MIXUP = ModelSource(
    name="sscd_disc_mixup",
    url="https://huggingface.co/nutszebra/sscd_onnx/resolve/0f03b67cec4f8b542f7adc883d431b8873cd5250/"
        "sscd_disc_mixup.onnx",
    sha256="e65e2a5b9a5e44a0b0f011bddaa6f28621021f6120ca7ce8b526c19f2f6156f8",
    size_bytes=98156202,
)
DEFAULT_MODEL = SSCD_DISC_MIXUP


class ModelNotAvailableError(RuntimeError):
    """
    The model file is missing (and must not / could not be downloaded) or has a wrong checksum.
    """


def default_cache_dir() -> Path:
    """
    :return: $PY_IMAGE_DEDUP_MODEL_CACHE, else $XDG_CACHE_HOME/py-image-dedup/models (~/.cache/...)
    """
    configured = os.environ.get("PY_IMAGE_DEDUP_MODEL_CACHE")
    if configured:
        return Path(configured)
    cache_home = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache_home) / "py-image-dedup" / "models"


def sha256_of_file(path: Path or str) -> str:
    """
    :param path: file
    :return: hex SHA-256 of its content
    """
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(source: ModelSource = DEFAULT_MODEL, cache_dir: Path or str = None, download: bool = True) -> Path:
    """
    Returns the path of a cached model, downloading it first if needed. The checksum is verified after
    the download and for an already cached file.

    :param source: model to fetch
    :param cache_dir: cache directory, default: `default_cache_dir()`
    :param download: False to only use an already cached file
    :return: path of the verified model file
    :raises ModelNotAvailableError: if the model is not cached and cannot be downloaded or its checksum is wrong
    """
    cache_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    target = cache_dir / f"{source.name}-{source.sha256[:12]}.onnx"
    if target.exists():
        if sha256_of_file(target) == source.sha256:
            return target
        raise ModelNotAvailableError(f"Checksum mismatch of cached model {target}, delete it to download it again")
    if not download:
        raise ModelNotAvailableError(f"Model {source.name} is not cached in {cache_dir}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info(f"Downloading {source.name} ({source.size_bytes / 1e6:.0f} MB) from {source.url}")
    handle, temp_name = tempfile.mkstemp(dir=cache_dir, suffix=".part")
    os.close(handle)
    try:
        try:
            urllib.request.urlretrieve(source.url, temp_name)
        except OSError as ex:
            raise ModelNotAvailableError(f"Could not download {source.url}: {ex}") from ex
        actual = sha256_of_file(temp_name)
        if actual != source.sha256:
            raise ModelNotAvailableError(f"Checksum mismatch of {source.url}: expected {source.sha256}, got {actual}")
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)
    return target


class EmbeddingExtractor(Protocol):
    """
    Computes L2 normalised embeddings of images.
    """
    # identifies model + preprocessing, stored embeddings with another id are recomputed
    model_id: str

    def embed(self, images: Sequence[Image.Image]) -> np.ndarray:
        """
        :param images: RGB images (EXIF orientation applied)
        :return: float32 array [len(images), dimensions], rows L2 normalised
        """
        ...


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    """
    :param vectors: [n, d]
    :return: float32 copy with unit length rows (zero rows stay zero)
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


class OnnxEmbeddingExtractor:
    """
    Embeddings of an SSCD ONNX model via onnxruntime on the CPU.
    """

    def __init__(self, model_path: Path or str, image_size: int = 320, threads: int = None,
                 expected_sha256: str = None):
        """
        :param model_path: ONNX file (input [N, 3, H, W] ImageNet normalised, output [N, D])
        :param image_size: the image is resized to image_size x image_size (ignored if the model has a fixed size)
        :param threads: onnxruntime intra-op threads, default: onnxruntime's choice (all cores)
        :param expected_sha256: verify the model file before loading it
        """
        try:
            import onnxruntime
        except ImportError as ex:
            raise ImportError("Crop detection needs the optional 'embeddings' dependencies: "
                              "pip install 'py-image-dedup[embeddings]'") from ex
        model_path = Path(model_path)
        if not model_path.exists():
            raise ModelNotAvailableError(f"Model file {model_path} does not exist")
        checksum = sha256_of_file(model_path)
        if expected_sha256 is not None and checksum != expected_sha256:
            raise ModelNotAvailableError(f"Checksum mismatch of {model_path}: expected {expected_sha256}, got {checksum}")

        options = onnxruntime.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        self._session = onnxruntime.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        model_input = self._session.get_inputs()[0]
        self._input_name = model_input.name
        batch, _, height, width = model_input.shape
        self._fixed_batch = batch if isinstance(batch, int) else None
        if isinstance(height, int) and isinstance(width, int):
            image_size = height
        self.image_size = image_size
        self.model_path = model_path
        self.model_size_bytes = model_path.stat().st_size
        self.model_id = f"{model_path.stem}:{checksum[:12]}:{image_size}"

    def preprocess(self, image: Image.Image) -> np.ndarray:
        """
        :param image: RGB image
        :return: float32 [3, size, size], ImageNet normalised
        """
        resized = image.convert("RGB").resize((self.image_size, self.image_size), Image.Resampling.BILINEAR)
        array = np.asarray(resized, dtype=np.float32) / 255.0
        array = (array - IMAGENET_MEAN) / IMAGENET_STD
        return array.transpose(2, 0, 1)

    def embed(self, images: Sequence[Image.Image]) -> np.ndarray:
        if not images:
            return np.zeros((0, 0), dtype=np.float32)
        batch = np.stack([self.preprocess(image) for image in images])
        if self._fixed_batch is None:
            outputs = self._session.run(None, {self._input_name: batch})[0]
        else:
            outputs = np.concatenate([
                self._session.run(None, {self._input_name: batch[i:i + self._fixed_batch]})[0]
                for i in range(0, len(batch), self._fixed_batch)
            ])
        return normalize_rows(outputs)


class EmbeddingStore:
    """
    SQLite cache of embeddings, one row per file, invalidated by size, mtime and model id.
    Separate from the signature store, so it can be deleted independently (and the feature stays opt-in).
    """

    def __init__(self, db_path: Path or str = ":memory:"):
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            "path TEXT PRIMARY KEY, file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, "
            "model_id TEXT NOT NULL, vector BLOB NOT NULL)")
        self._connection.commit()

    def get(self, signatures: Iterable[ImageSignature], model_id: str) -> Dict[str, np.ndarray]:
        """
        :param signatures: files to look up
        :param model_id: model id of the extractor
        :return: path -> embedding for all files with an up to date entry
        """
        wanted = {s.path: s for s in signatures}
        result = {}
        rows = self._connection.execute("SELECT path, file_size, mtime_ns, model_id, vector FROM embeddings")
        for path, file_size, mtime_ns, stored_model_id, vector in rows:
            signature = wanted.get(path)
            if (signature is not None and stored_model_id == model_id
                    and file_size == signature.file_size and mtime_ns == signature.mtime_ns):
                result[path] = np.frombuffer(vector, dtype=np.float32)
        return result

    def put(self, entries: Iterable[Tuple[ImageSignature, np.ndarray]], model_id: str):
        """
        :param entries: (signature, embedding) pairs
        :param model_id: model id of the extractor
        """
        self._connection.executemany(
            "INSERT OR REPLACE INTO embeddings (path, file_size, mtime_ns, model_id, vector) VALUES (?, ?, ?, ?, ?)",
            [(s.path, s.file_size, s.mtime_ns, model_id, np.asarray(v, dtype=np.float32).tobytes())
             for s, v in entries])
        self._connection.commit()

    def remove_except(self, paths: Iterable[str]):
        """
        :param paths: paths to keep, all other entries are deleted
        """
        keep = set(paths)
        stale = [(p,) for (p,) in self._connection.execute("SELECT path FROM embeddings") if p not in keep]
        self._connection.executemany("DELETE FROM embeddings WHERE path = ?", stale)
        self._connection.commit()


def find_similar_pairs(vectors: np.ndarray, min_similarity: float,
                       block_size: int = 256) -> List[Tuple[int, int, float]]:
    """
    Brute force all-pairs cosine similarity (rows must be L2 normalised), block-wise to bound memory
    (`block_size x n` float32). Fine for ~100k images; above that an ANN index (sqlite-vec, FAISS) is needed.

    :param vectors: [n, d] normalised embeddings
    :param min_similarity: minimum cosine similarity
    :param block_size: rows per block
    :return: (i, j, similarity) with i < j
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    result = []
    n = len(vectors)
    for start in range(0, n, block_size):
        block = vectors[start:start + block_size]
        similarities = block @ vectors[start:].T
        rows, columns = np.nonzero(similarities >= min_similarity)
        for row, column in zip(rows.tolist(), columns.tolist()):
            i, j = start + row, start + column
            if i < j:
                result.append((i, j, float(similarities[row, column])))
    return result


def _orientation_free_aspect_ratio(signature: ImageSignature) -> float:
    """
    :return: long edge / short edge (a rotated copy is not a crop)
    """
    short, long = sorted((signature.width, signature.height))
    return long / short if short else 0.0


def same_framing(a: ImageSignature, b: ImageSignature, thresholds: MatchingThresholds = None) -> bool:
    """
    :return: True if both images have the same aspect ratio (within the tolerance), ignoring the orientation
    """
    thresholds = thresholds or MatchingThresholds()
    ratio_a, ratio_b = _orientation_free_aspect_ratio(a), _orientation_free_aspect_ratio(b)
    if not (ratio_a and ratio_b):
        return True
    return abs(ratio_a - ratio_b) / max(ratio_a, ratio_b) <= thresholds.aspect_ratio_tolerance


def find_crops(group: DuplicateGroup, signatures: Dict[str, ImageSignature],
               thresholds: MatchingThresholds = None) -> List[CropRelation]:
    """
    Finds the members of a group that are crops. A pairwise comparison cannot tell a crop from a downscaled copy
    of the source (both have fewer pixels), so the group is looked at as a whole:

    - the member with the most pixels is the reference (the presumed original)
    - members connected to it by hash matches (exact, pixel-identical, near-duplicate) between images with the
      same aspect ratio show the full frame (resized, recompressed, edited, rotated copies)
    - all other members are crops: they were only connected through an embedding match or a hash match with a
      different aspect ratio

    A crop that was upscaled beyond the source is still detected (only its framing counts), but if the reference
    itself is a crop (the source is smaller than the crop), the roles are swapped. That is rare and the group goes
    to review anyway.

    :param group: group to inspect
    :param signatures: path -> signature of (at least) all members
    :param thresholds: aspect ratio tolerance, defaults if None
    :return: one relation per crop (source = the reference)
    """
    members = [signatures[p] for p in group.paths if p in signatures]
    if len(members) < 2:
        return []
    reference = max(members, key=lambda s: (s.pixel_count, s.path))
    neighbours: Dict[str, List[str]] = {s.path: [] for s in members}
    best_similarity: Dict[str, float] = {}
    for match in group.matches:
        if match.a not in neighbours or match.b not in neighbours:
            continue
        if match.reason == MatchReason.EMBEDDING:
            for path in (match.a, match.b):
                best_similarity[path] = max(best_similarity.get(path, 0.0), match.embedding_similarity or 0.0)
        elif same_framing(signatures[match.a], signatures[match.b], thresholds):
            neighbours[match.a].append(match.b)
            neighbours[match.b].append(match.a)

    full_frame = {reference.path}
    todo = [reference.path]
    while todo:
        for neighbour in neighbours[todo.pop()]:
            if neighbour not in full_frame:
                full_frame.add(neighbour)
                todo.append(neighbour)

    return [
        CropRelation(crop=member.path, source=reference.path,
                     area_ratio=member.pixel_count / reference.pixel_count if reference.pixel_count else 0.0,
                     aspect_ratio_differs=not same_framing(member, reference, thresholds),
                     embedding_similarity=best_similarity.get(member.path))
        for member in members if member.path not in full_frame
    ]


def embedding_confidence(similarity: float, min_similarity: float) -> float:
    """
    :return: confidence in [CONFIDENCE_EMBEDDING_MIN, CONFIDENCE_EMBEDDING_MAX], linear in the similarity
    """
    closeness = (similarity - min_similarity) / max(1e-9, 1 - min_similarity)
    closeness = max(0.0, min(1.0, closeness))
    return CONFIDENCE_EMBEDDING_MIN + (CONFIDENCE_EMBEDDING_MAX - CONFIDENCE_EMBEDDING_MIN) * closeness


def _pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


class EmbeddingStage:
    """
    Extra matching stage after the hash stages: pairs with a high embedding similarity that are not already
    matched become `MatchReason.EMBEDDING` matches of kind CROP (BURST with a confidence penalty for burst shots).
    """

    def __init__(self,
                 extractor: EmbeddingExtractor,
                 store: EmbeddingStore = None,
                 min_similarity: float = DEFAULT_MIN_SIMILARITY,
                 workers: int = None,
                 batch_size: int = 16,
                 preview_size: int = 512,
                 decoder: Callable[[str], Image.Image] = None):
        """
        :param extractor: embedding extractor (e.g. OnnxEmbeddingExtractor)
        :param store: embedding cache, in memory if None
        :param min_similarity: minimum cosine similarity of an embedding match
        :param workers: decoding threads, default: CPU count
        :param batch_size: images per extractor call
        :param preview_size: JPEG draft size used for decoding (the model input is much smaller)
        :param decoder: path -> RGB image, default: EXIF oriented draft decode
        """
        self.extractor = extractor
        self.store = store or EmbeddingStore()
        self.min_similarity = min_similarity
        self.workers = workers or os.cpu_count() or 1
        self.batch_size = batch_size
        self.preview_size = preview_size
        self.decoder = decoder or (lambda path: decode_preview(path, self.preview_size).preview)
        # path -> error message of the last update
        self.failed: Dict[str, str] = {}

    def update(self, signatures: Sequence[ImageSignature]) -> Dict[str, np.ndarray]:
        """
        Computes missing embeddings.

        :param signatures: all files
        :return: path -> embedding for all files that could be embedded
        """
        model_id = self.extractor.model_id
        known = self.store.get(signatures, model_id)
        todo = [s for s in signatures if s.path not in known]
        self.failed = {}

        def decode(signature: ImageSignature):
            try:
                return signature, self.decoder(signature.path), None
            except Exception as ex:
                LOGGER.warning(f"Could not decode {signature.path} for embedding: {ex}")
                return signature, None, f"{type(ex).__name__}: {ex}"

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            for start in range(0, len(todo), self.batch_size):
                decoded = list(executor.map(decode, todo[start:start + self.batch_size]))
                for signature, _, error in decoded:
                    if error is not None:
                        self.failed[signature.path] = error
                ok = [(signature, image) for signature, image, error in decoded if error is None]
                if not ok:
                    continue
                vectors = self.extractor.embed([image for _, image in ok])
                entries = list(zip([signature for signature, _ in ok], vectors))
                self.store.put(entries, model_id)
                known.update({signature.path: vector for signature, vector in entries})
        return known

    def find_matches(self, signatures: Sequence[ImageSignature], existing: Iterable[Match],
                     thresholds: MatchingThresholds = None) -> List[Match]:
        """
        :param signatures: all files
        :param existing: matches of the hash stages, these pairs are skipped
        :param thresholds: burst window and penalty, defaults if None
        :return: new embedding matches
        """
        thresholds = thresholds or MatchingThresholds()
        embeddings = self.update(signatures)
        indexed = [s for s in signatures if s.path in embeddings]
        if len(indexed) < 2:
            return []
        matched = {_pair(m.a, m.b) for m in existing}
        vectors = normalize_rows(np.stack([embeddings[s.path] for s in indexed]))

        matches = []
        for i, j, similarity in find_similar_pairs(vectors, self.min_similarity):
            a, b = indexed[i], indexed[j]
            pair = _pair(a.path, b.path)
            if pair in matched or a.sha256 == b.sha256:
                continue
            confidence = embedding_confidence(similarity, self.min_similarity)
            notes = [NOTE_EMBEDDING.format(similarity=similarity)]
            kind = VariantKind.CROP
            burst = burst_note(a.metadata, b.metadata, thresholds.burst_window_seconds)
            if burst is not None:
                # another shot of a series, not a crop: still grouped, but only for review
                confidence = max(0.0, confidence - thresholds.burst_confidence_penalty)
                notes.append(burst)
                kind = VariantKind.BURST
            matches.append(Match(*pair, reason=MatchReason.EMBEDDING, confidence=confidence,
                                 embedding_similarity=similarity, notes=tuple(notes), kind=kind))
        return matches


def annotate_crops(groups: Iterable[DuplicateGroup], signatures: Sequence[ImageSignature],
                   thresholds: MatchingThresholds = None):
    """
    Sets `DuplicateGroup.crops` (see `find_crops`). Groups with crops go to review: the crops become
    `VariantKind.CROP` members (unless they are burst shots) and the group gets a "crop variant" review reason.

    :param groups: groups to annotate (modified in place)
    :param signatures: signatures of all group members
    :param thresholds: aspect ratio tolerance, defaults if None
    """
    by_path = {s.path: s for s in signatures}
    for group in groups:
        group.crops = find_crops(group, by_path, thresholds)
        if not group.crops:
            continue
        for relation in group.crops:
            if group.member_kinds.get(relation.crop) is not VariantKind.BURST:
                group.member_kinds[relation.crop] = VariantKind.CROP
        reason = variant_reason(VariantKind.CROP, sorted({relation.crop for relation in group.crops}))
        existing = [i for i, r in enumerate(group.review_reasons) if r.startswith(f"{VariantKind.CROP.value} ")]
        if existing:
            group.review_reasons[existing[0]] = reason
        else:
            bursts = sum(1 for r in group.review_reasons if r.startswith("burst:"))
            group.review_reasons.insert(bursts, reason)


def rank_crops_first(paths: Sequence[str], crops: Iterable[CropRelation]) -> List[str]:
    """
    Default ranking hint of the owner: a crop usually is the deliberately edited version, so it is preferred
    over its source. The group still goes to review, this only decides the proposed keeper.

    :param paths: members in their current priority order (best first)
    :param crops: crop relations of the group
    :return: paths reordered: crops first, otherwise the order is kept
    """
    crop_paths = {relation.crop for relation in crops}
    return [p for p in paths if p in crop_paths] + [p for p in paths if p not in crop_paths]
