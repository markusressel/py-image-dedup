"""
Experimental matching pipeline (prototype): SQLite + staged grouping instead of image-match + Elasticsearch.

Not used by the deduplicator or the daemon yet.
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from py_image_dedup.matching.decoding import DEFAULT_PREVIEW_SIZE
from py_image_dedup.matching.embeddings import EmbeddingStage, annotate_crops
from py_image_dedup.matching.grouping import cluster, find_matches
from py_image_dedup.matching.models import DuplicateGroup, ImageSignature, Match, MatchingThresholds
from py_image_dedup.matching.signatures import compute_full_pixel_hash, compute_signature
from py_image_dedup.matching.store import SignatureStore

LOGGER = logging.getLogger(__name__)

DEFAULT_FILE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif")


@dataclass
class AnalysisResult:
    """
    Outcome of analysing a set of files.
    """
    analysed: int = 0
    skipped_unchanged: int = 0
    removed_missing: int = 0
    # path -> error message
    failed: Dict[str, str] = field(default_factory=dict)


@dataclass
class MatchingResult:
    """
    Outcome of the matching stage.
    """
    groups: List[DuplicateGroup]
    matches: List[Match]
    # stage name -> seconds
    timings: Dict[str, float] = field(default_factory=dict)


def find_image_files(directory: Path or str,
                     file_extensions: Iterable[str] = DEFAULT_FILE_EXTENSIONS) -> List[Path]:
    """
    :param directory: root directory, searched recursively
    :param file_extensions: lower case extensions (with dot) to include
    :return: sorted list of image files
    """
    extensions = {e.lower() for e in file_extensions}
    return sorted(p for p in Path(directory).rglob("*") if p.is_file() and p.suffix.lower() in extensions)


class MatchingPipeline:
    """
    Analyses image files into a SignatureStore and groups duplicates.
    """

    def __init__(self,
                 store: SignatureStore,
                 thresholds: MatchingThresholds = None,
                 workers: int = None,
                 preview_size: int = DEFAULT_PREVIEW_SIZE,
                 verify_pixel_identical: bool = True,
                 embedding_stage: EmbeddingStage = None):
        """
        :param store: signature store
        :param thresholds: matching thresholds (incl. options like `match_rotations`), defaults if None
        :param workers: number of analysis threads (Pillow releases the GIL while decoding), default: CPU count
        :param preview_size: minimum edge length of the JPEG draft preview
        :param verify_pixel_identical: verify preview pixel hash collisions with a full resolution decode
        :param embedding_stage: opt-in crop detection (embeddings), off if None
        """
        self.store = store
        self.thresholds = thresholds or MatchingThresholds()
        self.workers = workers or os.cpu_count() or 1
        self.preview_size = preview_size
        self.verify_pixel_identical = verify_pixel_identical
        self.embedding_stage = embedding_stage
        # full resolution pixel hashes computed during this matching run (signatures are immutable)
        self._full_pixel_hashes: Dict[str, str] = {}

    def analyse(self, files: List[Path], progress: Callable[[int, int], None] = None) -> AnalysisResult:
        """
        Computes and stores signatures of all files that are new or changed (size, mtime or signature version)
        and removes store entries of files that are not part of `files` anymore.

        :param files: all image files that should be known to the store
        :param progress: optional callback (done, total)
        :return: statistics
        """
        result = AnalysisResult()
        states = self.store.get_file_states()
        paths = {str(f) for f in files}
        missing = [p for p in states if p not in paths]
        if missing:
            self.store.remove(missing)
            result.removed_missing = len(missing)

        todo = []
        for file in files:
            stat = file.stat()
            if SignatureStore.needs_analysis(states.get(str(file)), stat.st_size, stat.st_mtime_ns):
                todo.append(file)
            else:
                result.skipped_unchanged += 1

        def analyse_one(path: Path) -> Tuple[Path, Optional[ImageSignature], Optional[str]]:
            try:
                return path, compute_signature(path, self.preview_size, self.thresholds.match_rotations), None
            except Exception as ex:
                LOGGER.warning(f"Could not analyse {path}: {ex}")
                return path, None, f"{type(ex).__name__}: {ex}"

        batch = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            for done, (path, signature, error) in enumerate(executor.map(analyse_one, todo), start=1):
                if signature is not None:
                    batch.append(signature)
                    result.analysed += 1
                else:
                    result.failed[str(path)] = error
                if len(batch) >= 500:
                    self.store.upsert(batch)
                    batch = []
                if progress is not None:
                    progress(done, len(todo))
        if batch:
            self.store.upsert(batch)
        return result

    def _resolve_full_pixel_hash(self, signature: ImageSignature) -> str:
        if signature.full_pixel_hash is not None:
            return signature.full_pixel_hash
        full_pixel_hash = self._full_pixel_hashes.get(signature.path)
        if full_pixel_hash is None:
            full_pixel_hash = compute_full_pixel_hash(signature.path)
            self.store.set_full_pixel_hash(signature.path, full_pixel_hash)
            self._full_pixel_hashes[signature.path] = full_pixel_hash
        return full_pixel_hash

    def match(self) -> MatchingResult:
        """
        Groups all files of the store.

        :return: groups and matches
        """
        timings = {}
        start = time.perf_counter()
        signatures = self.store.get_all()
        timings["load"] = time.perf_counter() - start

        start = time.perf_counter()
        resolver = self._resolve_full_pixel_hash if self.verify_pixel_identical else None
        matches = find_matches(signatures, self.thresholds, resolver)
        timings["match"] = time.perf_counter() - start

        if self.embedding_stage is not None:
            start = time.perf_counter()
            matches = matches + self.embedding_stage.find_matches(signatures, matches, self.thresholds)
            timings["embeddings"] = time.perf_counter() - start

        start = time.perf_counter()
        groups = cluster(signatures, matches, self.thresholds)
        annotate_crops(groups, signatures, self.thresholds)
        timings["cluster"] = time.perf_counter() - start
        return MatchingResult(groups=groups, matches=matches, timings=timings)
