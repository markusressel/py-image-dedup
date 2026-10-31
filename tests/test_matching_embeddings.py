"""
Tests of the opt-in crop detection stage (py_image_dedup.matching.embeddings).

The search, grouping and caching logic is tested with a fake extractor. The test with the real SSCD model only
runs if onnxruntime is installed and the model is already cached (it never downloads), so the default run needs
neither the model nor network access.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np
from PIL import Image

from py_image_dedup.matching import (
    ImageMetadata, ImageSignature, Match, MatchReason, MatchingThresholds, VariantKind, cluster, find_matches,
)
from py_image_dedup.matching.embeddings import (
    CONFIDENCE_EMBEDDING_MAX, EmbeddingStage, EmbeddingStore, ModelNotAvailableError, ModelSource, annotate_crops,
    ensure_model, find_similar_pairs, normalize_rows, rank_crops_first,
)

IMAGES = Path(__file__).parent / "images"


def _signature(path: str, width: int = 4000, height: int = 3000, phash: int = 0, dhash: int = 0,
               sha256: str = None, mtime_ns: int = 1, metadata: ImageMetadata = None) -> ImageSignature:
    return ImageSignature(path=path, file_size=1, mtime_ns=mtime_ns, sha256=sha256 or f"sha-{path}",
                          preview_pixel_hash=f"px-{path}", phash=phash, dhash=dhash, width=width, height=height,
                          is_grayscale=False, metadata=metadata or ImageMetadata())


class FakeExtractor:
    """
    Embeds a decoded image by its mean colour (the fake decoder encodes the "photo" as a colour).
    """
    model_id = "fake:1"

    def __init__(self):
        self.embedded: List[int] = []

    def embed(self, images: Sequence[Image.Image]) -> np.ndarray:
        self.embedded.append(len(images))
        return normalize_rows(np.stack([np.asarray(image, dtype=np.float32).mean(axis=(0, 1)) for image in images]))


def _stage(colours: Dict[str, tuple], **kwargs) -> (EmbeddingStage, FakeExtractor):
    extractor = FakeExtractor()
    stage = EmbeddingStage(extractor, decoder=lambda path: Image.new("RGB", (4, 4), colours[path]), workers=2,
                           **kwargs)
    return stage, extractor


class SearchTest(unittest.TestCase):

    def test_matches_brute_force_across_blocks(self):
        rng = np.random.default_rng(1)
        vectors = normalize_rows(rng.standard_normal((50, 8)))
        vectors[30] = vectors[3]
        vectors[49] = normalize_rows(vectors[3:4] + 0.05 * rng.standard_normal((1, 8)))[0]
        expected = {(i, j) for i in range(50) for j in range(i + 1, 50) if vectors[i] @ vectors[j] >= 0.9}
        found = find_similar_pairs(vectors, 0.9, block_size=7)
        self.assertEqual(expected, {(i, j) for i, j, _ in found})
        self.assertIn((3, 30), expected)
        for i, j, similarity in found:
            self.assertAlmostEqual(float(vectors[i] @ vectors[j]), similarity, places=5)


class FindCropsTest(unittest.TestCase):

    @staticmethod
    def _group(signatures, matches):
        groups = cluster(signatures, matches)
        annotate_crops(groups, signatures)
        return groups[0]

    @staticmethod
    def _match(a, b, reason=MatchReason.NEAR_DUPLICATE, similarity=None):
        a, b = sorted((a, b))
        return Match(a, b, reason=reason, confidence=0.6 if similarity else 0.9, embedding_similarity=similarity)

    def test_crop_with_other_aspect_ratio_matched_by_hashes(self):
        signatures = [_signature("/a.jpg", 4000, 3000), _signature("/b.jpg", 2000, 2000)]
        group = self._group(signatures, [self._match("/a.jpg", "/b.jpg")])
        self.assertEqual(1, len(group.crops))
        relation = group.crops[0]
        self.assertEqual(("/b.jpg", "/a.jpg"), (relation.crop, relation.source))
        self.assertAlmostEqual(4 / 12, relation.area_ratio)
        self.assertTrue(relation.aspect_ratio_differs)
        self.assertIsNone(relation.embedding_similarity)

    def test_resized_and_rotated_copies_are_not_crops(self):
        signatures = [_signature("/a.jpg", 4000, 3000), _signature("/b.jpg", 1280, 960),
                      _signature("/c.jpg", 3000, 4000)]
        group = self._group(signatures, [self._match("/a.jpg", "/b.jpg"), self._match("/b.jpg", "/c.jpg")])
        self.assertFalse(group.has_crops)

    def test_crop_larger_than_a_resized_copy(self):
        # pairwise the downscaled copy has fewer pixels than the crop, the group view gets it right
        signatures = [_signature("/a.jpg", 4000, 3000), _signature("/small.jpg", 1280, 960),
                      _signature("/crop.jpg", 3600, 2700)]
        group = self._group(signatures, [
            self._match("/a.jpg", "/small.jpg"),
            self._match("/crop.jpg", "/small.jpg", MatchReason.EMBEDDING, 0.8),
            self._match("/a.jpg", "/crop.jpg", MatchReason.EMBEDDING, 0.85),
        ])
        self.assertEqual([("/crop.jpg", "/a.jpg")], [(r.crop, r.source) for r in group.crops])
        self.assertFalse(group.crops[0].aspect_ratio_differs)
        self.assertAlmostEqual(0.85, group.crops[0].embedding_similarity)

    def test_rank_crops_first(self):
        signatures = [_signature("/a.jpg", 4000, 3000), _signature("/b.jpg", 2000, 2000)]
        group = self._group(signatures, [self._match("/a.jpg", "/b.jpg")])
        self.assertEqual(["/b.jpg", "/a.jpg", "/c.jpg"], rank_crops_first(["/a.jpg", "/b.jpg", "/c.jpg"], group.crops))
        self.assertEqual(["/a.jpg", "/c.jpg"], rank_crops_first(["/a.jpg", "/c.jpg"], []))


class EmbeddingStageTest(unittest.TestCase):

    def test_crop_is_matched_and_grouped_for_review(self):
        # the crop's hashes are far away from the source, only the embedding stage finds it
        signatures = [
            _signature("/photo.jpg", 4000, 3000, phash=0),
            _signature("/photo-telegram.jpg", 1280, 960, phash=1),
            _signature("/photo-crop.jpg", 2400, 2000, phash=0xFFFF_FFFF),
            _signature("/other.jpg", 4000, 3000, phash=0xFFFF_0000_FFFF_0000),
        ]
        colours = {"/photo.jpg": (200, 10, 10), "/photo-telegram.jpg": (200, 12, 10),
                   "/photo-crop.jpg": (190, 20, 10), "/other.jpg": (10, 10, 200)}
        stage, _ = _stage(colours, min_similarity=0.9)
        thresholds = MatchingThresholds()

        hash_matches = find_matches(signatures, thresholds)
        self.assertEqual({("/photo-telegram.jpg", "/photo.jpg")}, {(m.a, m.b) for m in hash_matches})

        reported = []
        embedding_matches = stage.find_matches(signatures, hash_matches, thresholds,
                                               lambda done, total: reported.append((done, total)))
        # all 4 files are missing an embedding, one batch
        self.assertEqual([(4, 4)], reported)
        pairs = {(m.a, m.b): m for m in embedding_matches}
        # already matched by the hashes: not reported again
        self.assertEqual({("/photo-crop.jpg", "/photo-telegram.jpg"), ("/photo-crop.jpg", "/photo.jpg")}, set(pairs))
        match = pairs[("/photo-crop.jpg", "/photo.jpg")]
        self.assertEqual(MatchReason.EMBEDDING, match.reason)
        self.assertLessEqual(match.confidence, CONFIDENCE_EMBEDDING_MAX)
        self.assertRegex(match.notes[0], r"^crop/embedding similarity (0\.9\d|1\.00)$")
        self.assertGreaterEqual(match.embedding_similarity, 0.9)
        self.assertEqual(VariantKind.CROP, match.kind)

        groups = cluster(signatures, hash_matches + embedding_matches, thresholds)
        annotate_crops(groups, signatures, thresholds)
        self.assertEqual(1, len(groups))
        group = groups[0]
        self.assertEqual(["/photo-crop.jpg", "/photo-telegram.jpg", "/photo.jpg"], group.paths)
        self.assertEqual(MatchReason.EMBEDDING, group.reason)
        self.assertTrue(group.has_crops)
        self.assertEqual([("/photo-crop.jpg", "/photo.jpg")], [(r.crop, r.source) for r in group.crops])
        self.assertEqual("/photo-crop.jpg", rank_crops_first(["/photo.jpg", "/photo-telegram.jpg",
                                                               "/photo-crop.jpg"], group.crops)[0])
        # a crop is a variant: never acted on automatically
        self.assertFalse(group.auto_act_allowed)
        self.assertEqual(VariantKind.CROP, group.member_kinds["/photo-crop.jpg"])
        self.assertEqual(VariantKind.COPY, group.member_kinds["/photo.jpg"])
        self.assertIn("crop variant: photo-crop.jpg", group.review_reasons)

    def test_hash_only_groups_without_crops(self):
        signatures = [_signature("/a.jpg", 4000, 3000), _signature("/b.jpg", 2000, 1500, phash=1)]
        matches = find_matches(signatures)
        groups = cluster(signatures, matches)
        annotate_crops(groups, signatures)
        self.assertFalse(groups[0].has_crops)
        self.assertTrue(groups[0].auto_act_allowed)

    def test_crop_found_by_hashes_goes_to_review(self):
        """
        A light crop can still match by hashes (different aspect ratio): the group goes to review.
        """
        signatures = [_signature("/a.jpg", 4000, 3000), _signature("/b.jpg", 3000, 3000, phash=1)]
        groups = cluster(signatures, find_matches(signatures))
        annotate_crops(groups, signatures)
        group = groups[0]
        self.assertTrue(group.has_crops)
        self.assertEqual(VariantKind.CROP, group.member_kinds["/b.jpg"])
        self.assertEqual(1, sum(r.startswith("crop variant") for r in group.review_reasons))
        self.assertIn("crop variant: b.jpg", group.review_reasons)

    def test_burst_is_not_a_crop(self):
        time = datetime(2020, 1, 1, 12, 0, 0)
        first = ImageMetadata(camera_make="X", camera_model="Y", capture_time=time)
        second = ImageMetadata(camera_make="X", camera_model="Y", capture_time=time + timedelta(seconds=2))
        signatures = [_signature("/a.jpg", metadata=first, phash=0),
                      _signature("/b.jpg", 3000, 3000, metadata=second, phash=0xFFFF_FFFF)]
        stage, _ = _stage({"/a.jpg": (200, 0, 0), "/b.jpg": (200, 0, 0)})
        matches = stage.find_matches(signatures, [])
        self.assertEqual(1, len(matches))
        self.assertEqual(VariantKind.BURST, matches[0].kind)
        self.assertEqual("burst: same camera (X Y), 2 s apart", matches[0].notes[-1])
        groups = cluster(signatures, matches)
        annotate_crops(groups, signatures)
        self.assertFalse(groups[0].auto_act_allowed)
        self.assertEqual(VariantKind.BURST, groups[0].member_kinds["/b.jpg"])

    def test_embeddings_are_cached_and_failures_reported(self):
        colours = {"/a.jpg": (200, 0, 0), "/b.jpg": (0, 200, 0)}
        store = EmbeddingStore()
        stage, extractor = _stage(colours, store=store)
        signatures = [_signature("/a.jpg"), _signature("/b.jpg"), _signature("/broken.jpg")]
        self.assertEqual({"/a.jpg", "/b.jpg"}, set(stage.update(signatures)))
        self.assertIn("/broken.jpg", stage.failed)
        self.assertEqual([2], extractor.embedded)

        stage.update(signatures[:2])
        self.assertEqual([2], extractor.embedded)

        # a changed file is embedded again
        stage.update([_signature("/a.jpg", mtime_ns=2), signatures[1]])
        self.assertEqual([2, 1], extractor.embedded)

        store.remove_except(["/b.jpg"])
        self.assertEqual({"/b.jpg"}, set(store.get(signatures, FakeExtractor.model_id)))
        self.assertEqual({}, store.get(signatures, "another-model"))


class ModelDownloadTest(unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="py-image-dedup-model-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.model = self.temp_dir / "upstream.onnx"
        self.model.write_bytes(b"not really a model")
        self.sha256 = hashlib.sha256(b"not really a model").hexdigest()

    def test_download_verifies_checksum_and_caches(self):
        cache = self.temp_dir / "cache"
        source = ModelSource(name="fake", url=self.model.as_uri(), sha256=self.sha256, size_bytes=18)
        with self.assertRaises(ModelNotAvailableError):
            ensure_model(source, cache, download=False)
        path = ensure_model(source, cache)
        self.assertEqual(b"not really a model", path.read_bytes())
        self.model.unlink()
        self.assertEqual(path, ensure_model(source, cache, download=False))
        self.assertEqual([path.name], [p.name for p in cache.iterdir()])

    def test_checksum_mismatch(self):
        cache = self.temp_dir / "cache"
        source = ModelSource(name="fake", url=self.model.as_uri(), sha256="0" * 64, size_bytes=18)
        with self.assertRaises(ModelNotAvailableError):
            ensure_model(source, cache)
        self.assertEqual([], list(cache.iterdir()))

    def test_unreachable_url(self):
        source = ModelSource(name="fake", url=(self.temp_dir / "missing.onnx").as_uri(), sha256=self.sha256,
                             size_bytes=18)
        with self.assertRaises(ModelNotAvailableError):
            ensure_model(source, self.temp_dir / "cache")

    def test_matching_package_does_not_import_onnxruntime(self):
        code = ("import sys, py_image_dedup.matching, py_image_dedup.matching.embeddings; "
                "assert 'onnxruntime' not in sys.modules")
        subprocess.run([sys.executable, "-c", code], check=True)


def _cached_model() -> Path or None:
    try:
        import onnxruntime  # noqa: F401
        return ensure_model(download=False)
    except (ImportError, ModelNotAvailableError):
        return None


@unittest.skipIf(_cached_model() is None, "onnxruntime or the cached SSCD model is not available")
class RealModelTest(unittest.TestCase):

    def test_crops_are_similar_other_photos_are_not(self):
        from py_image_dedup.matching.decoding import decode_preview
        from py_image_dedup.matching.embeddings import OnnxEmbeddingExtractor
        extractor = OnnxEmbeddingExtractor(_cached_model())
        original = decode_preview(IMAGES / "bottles" / "IMG_20190903_193151.jpg").preview
        other = decode_preview(IMAGES / "clouds" / "IMG_20190903_193537.jpg").preview
        width, height = original.size
        crop = original.crop((int(width * 0.2), int(height * 0.1), int(width * 0.9), int(height * 0.9)))
        vectors = extractor.embed([original, crop, other])
        self.assertEqual(3, len(vectors))
        self.assertAlmostEqual(1.0, float(np.linalg.norm(vectors[0])), places=4)
        self.assertGreater(float(vectors[0] @ vectors[1]), 0.6)
        self.assertLess(float(vectors[0] @ vectors[2]), 0.6)
