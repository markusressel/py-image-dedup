"""
Tests of the experimental matching pipeline (py_image_dedup.matching).
"""
import os
import random
import shutil
import tempfile
import unittest
from datetime import datetime
from itertools import combinations
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image, ImageOps

from py_image_dedup.matching import (
    ImageMetadata, ImageSignature, Match, MatchReason, MatchingPipeline, MatchingThresholds, SignatureStore,
    burst_veto, cluster, compute_signature, find_image_files, find_matches,
)
from py_image_dedup.matching.evaluation import evaluate_pairs, ground_truth_by_folder, pairs_of_groups
from py_image_dedup.matching.grouping import NOTE_COLOUR, NOTE_SAME_CAPTURE, near_duplicate_confidence
from py_image_dedup.matching.metadata import parse_capture_time
from py_image_dedup.matching.search import find_candidate_pairs, hamming_distance, to_array
from py_image_dedup.matching.signatures import compute_full_pixel_hash, hash_to_int, is_grayscale

IMAGES = Path(__file__).parent / "images"
ORIGINAL = IMAGES / "bottles" / "IMG_20190903_193151.jpg"
GRAYSCALE = IMAGES / "bottles" / "IMG_20190903_193151-grayscale.jpg"
TELEGRAM = IMAGES / "bottles" / "IMG_20190903_193151-telegram-compression.jpg"

CAMERA = ImageMetadata(camera_make="OnePlus", camera_model="ONEPLUS A6013",
                       capture_time=datetime(2019, 9, 3, 19, 31, 51, 264445), capture_time_has_subsec=True)


def _signature(path: str, phash: int = 0, dhash: int = 0, sha256: str = None, preview: str = None,
               metadata: ImageMetadata = None, width: int = 4000, height: int = 3000,
               grayscale: bool = False) -> ImageSignature:
    return ImageSignature(path=path, file_size=1, mtime_ns=1, sha256=sha256 or f"sha-{path}",
                          preview_pixel_hash=preview or f"px-{path}", phash=phash, dhash=dhash, width=width,
                          height=height, is_grayscale=grayscale, metadata=metadata or ImageMetadata())


class TempDirTestCase(unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="py-image-dedup-matching-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def small_copy(self, name: str, source: Path = ORIGINAL, size: int = 400, **save_args) -> Path:
        """
        Writes a downscaled copy of a test image (fast to decode), keeping its EXIF data.
        """
        target = self.temp_dir / name
        with Image.open(source) as image:
            exif = image.getexif()
            image.thumbnail((size, size))
            image.save(target, exif=exif, **save_args)
        return target


class MetadataTest(unittest.TestCase):

    def test_parse_capture_time_with_subsec(self):
        self.assertEqual((datetime(2019, 9, 3, 19, 31, 51, 264445), True),
                         parse_capture_time("2019:09:03 19:31:51", "264445"))
        self.assertEqual((datetime(2019, 9, 3, 19, 31, 51, 500000), True),
                         parse_capture_time("2019:09:03 19:31:51", "5"))
        self.assertEqual((datetime(2019, 9, 3, 19, 31, 51, 50000), True),
                         parse_capture_time("2019:09:03 19:31:51", "050"))

    def test_parse_capture_time_without_subsec_or_invalid(self):
        self.assertEqual((datetime(2019, 9, 3, 19, 31, 51), False), parse_capture_time("2019:09:03 19:31:51", None))
        self.assertEqual((None, False), parse_capture_time("0000:00:00 00:00:00", None))
        self.assertEqual((None, False), parse_capture_time("   ", None))
        self.assertEqual((None, False), parse_capture_time(None, "123"))


class SignatureTest(TempDirTestCase):

    @classmethod
    def setUpClass(cls):
        cls.original = compute_signature(ORIGINAL)

    def test_signature_of_test_image(self):
        signature = self.original
        self.assertEqual((4656, 3492), (signature.width, signature.height))
        self.assertEqual(os.path.getsize(ORIGINAL), signature.file_size)
        self.assertEqual(64, len(signature.sha256))
        self.assertFalse(signature.is_grayscale)
        self.assertEqual("ONEPLUS A6013", signature.metadata.camera_model)
        self.assertEqual("google", signature.metadata.camera_make)  # GCam port
        self.assertEqual(datetime(2019, 9, 3, 19, 31, 51, 264445), signature.metadata.capture_time)
        self.assertTrue(signature.metadata.capture_time_has_subsec)
        self.assertIsNotNone(signature.metadata.software)

    def test_telegram_copy_has_no_metadata_but_similar_hashes(self):
        signature = compute_signature(TELEGRAM)
        self.assertIsNone(signature.metadata.capture_time)
        self.assertIsNone(signature.metadata.camera)
        self.assertLessEqual(hamming_distance(self.original.phash, signature.phash), 2)
        self.assertLessEqual(hamming_distance(self.original.dhash, signature.dhash), 2)

    def test_grayscale_detection(self):
        self.assertTrue(compute_signature(GRAYSCALE).is_grayscale)
        self.assertTrue(is_grayscale(Image.new("RGB", (64, 64), (80, 80, 80))))
        self.assertFalse(is_grayscale(Image.new("RGB", (64, 64), (200, 30, 30))))

    def test_hash_to_int_matches_hex_representation(self):
        image_hash = imagehash.phash(Image.open(TELEGRAM))
        self.assertEqual(int(str(image_hash), 16), hash_to_int(image_hash))

    def test_exif_orientation_is_applied(self):
        """
        A copy whose pixels are rotated but that has a compensating orientation tag looks the same
        and must get the same hashes and dimensions.
        """
        upright = self.small_copy("upright.jpg", quality=95)
        with Image.open(upright) as image:
            exif = image.getexif()
            exif[0x0112] = 6  # rotate 90° clockwise to display
            rotated = image.transpose(Image.Transpose.ROTATE_90)
            rotated_path = self.temp_dir / "rotated.jpg"
            rotated.save(rotated_path, exif=exif, quality=95)

        a, b = compute_signature(upright), compute_signature(rotated_path)
        self.assertEqual((a.width, a.height), (b.width, b.height))
        self.assertLessEqual(hamming_distance(a.phash, b.phash), 2)
        self.assertLessEqual(hamming_distance(a.dhash, b.dhash), 2)

    def test_full_pixel_hash_ignores_metadata_and_format(self):
        jpeg = self.small_copy("small.jpg")
        with Image.open(jpeg) as image:
            ImageOps.exif_transpose(image).save(self.temp_dir / "small.png")
            image.save(self.temp_dir / "no-exif.jpg", quality=50)
        self.assertEqual(compute_full_pixel_hash(jpeg), compute_full_pixel_hash(self.temp_dir / "small.png"))
        self.assertNotEqual(compute_full_pixel_hash(jpeg), compute_full_pixel_hash(self.temp_dir / "no-exif.jpg"))


class StoreTest(unittest.TestCase):

    def setUp(self):
        self.store = SignatureStore(":memory:")
        self.addCleanup(self.store.close)

    def test_roundtrip_with_unsigned_64_bit_hashes(self):
        signature = _signature("/a.jpg", phash=(1 << 64) - 1, dhash=1 << 63, metadata=CAMERA, grayscale=True)
        self.store.upsert([signature])
        self.assertEqual(signature, self.store.get("/a.jpg"))
        self.assertEqual([signature], self.store.get_all())

    def test_full_pixel_hash_and_remove(self):
        self.store.upsert([_signature("/a.jpg"), _signature("/b.jpg")])
        self.store.set_full_pixel_hash("/a.jpg", "abc")
        self.assertEqual("abc", self.store.get("/a.jpg").full_pixel_hash)
        self.store.remove(["/a.jpg"])
        self.assertIsNone(self.store.get("/a.jpg"))
        self.assertEqual(1, self.store.count())

    def test_needs_analysis(self):
        self.store.upsert([_signature("/a.jpg")])
        state = self.store.get_file_states()["/a.jpg"]
        self.assertFalse(SignatureStore.needs_analysis(state, 1, 1))
        self.assertTrue(SignatureStore.needs_analysis(state, 2, 1))
        self.assertTrue(SignatureStore.needs_analysis(state, 1, 2))
        self.assertTrue(SignatureStore.needs_analysis(None, 1, 1))


class SearchTest(unittest.TestCase):

    def test_matches_brute_force(self):
        rng = random.Random(4)
        for n in (0, 1, 2, 7, 70):
            # few distinct bits so that there are matches
            phashes = [rng.getrandbits(12) for _ in range(n)]
            dhashes = [rng.getrandbits(8) for _ in range(n)]
            found = {(c.i, c.j, c.phash_distance, c.dhash_distance)
                     for c in find_candidate_pairs(to_array(phashes), to_array(dhashes), 4, 3, block_size=16)}
            expected = set()
            for i, j in combinations(range(n), 2):
                dp, dd = hamming_distance(phashes[i], phashes[j]), hamming_distance(dhashes[i], dhashes[j])
                if dp <= 4 and dd <= 3:
                    expected.add((i, j, dp, dd))
            self.assertEqual(expected, found, n)

    def test_high_bit_hashes(self):
        hashes = to_array([(1 << 64) - 1, (1 << 64) - 2, 0])
        pairs = find_candidate_pairs(hashes, hashes, 1, 1)
        self.assertEqual([(0, 1, 1, 1)], [(c.i, c.j, c.phash_distance, c.dhash_distance) for c in pairs])
        self.assertIsInstance(hashes, np.ndarray)


class BurstVetoTest(unittest.TestCase):

    def _at(self, seconds: float, subsec: bool = True, model: str = "ONEPLUS A6013") -> ImageMetadata:
        base = datetime(2019, 9, 3, 19, 31, 50)
        return ImageMetadata(camera_make="OnePlus", camera_model=model, capture_time_has_subsec=subsec,
                             capture_time=base.replace(second=50 + int(seconds),
                                                       microsecond=int(round(seconds % 1 * 1_000_000))))

    def test_same_camera_seconds_apart_is_a_burst(self):
        self.assertIsNotNone(burst_veto(self._at(0), self._at(1.5), 10))
        self.assertIsNotNone(burst_veto(self._at(0), self._at(0.1), 10))

    def test_no_veto(self):
        # same capture time: a copy
        self.assertIsNone(burst_veto(self._at(0), self._at(0), 10))
        # outside of the window
        self.assertIsNone(burst_veto(self._at(0), self._at(9), 5))
        # different camera
        self.assertIsNone(burst_veto(self._at(0), self._at(1, model="Pixel 7"), 10))
        # metadata missing (e.g. messenger copy)
        self.assertIsNone(burst_veto(self._at(0), ImageMetadata(), 10))
        # only one file has sub-seconds: compared at second precision
        self.assertIsNone(burst_veto(self._at(0.3), self._at(0, subsec=False), 10))


class MatchingTest(unittest.TestCase):

    def test_stages_and_reasons(self):
        signatures = [
            _signature("/a.jpg", phash=0b1111, sha256="same", preview="p1"),
            _signature("/a-copy.jpg", phash=0b1111, sha256="same", preview="p1"),
            _signature("/a-no-exif.jpg", phash=0b1111, sha256="other", preview="p1"),
            _signature("/a-small.jpg", phash=0b0111, dhash=0b1),
            _signature("/unrelated.jpg", phash=(1 << 64) - 1, dhash=(1 << 64) - 1),
        ]
        matches, vetoes = find_matches(signatures, MatchingThresholds())
        by_pair = {(m.a, m.b): m for m in matches}
        self.assertEqual(MatchReason.EXACT, by_pair[("/a-copy.jpg", "/a.jpg")].reason)
        self.assertEqual(1.0, by_pair[("/a-copy.jpg", "/a.jpg")].confidence)
        self.assertEqual(MatchReason.PIXEL_IDENTICAL, by_pair[("/a-no-exif.jpg", "/a.jpg")].reason)
        near = by_pair[("/a-small.jpg", "/a.jpg")]
        self.assertEqual(MatchReason.NEAR_DUPLICATE, near.reason)
        self.assertEqual((1, 1), (near.phash_distance, near.dhash_distance))
        self.assertLess(near.confidence, 0.99)
        self.assertFalse(any("/unrelated.jpg" in (m.a, m.b) for m in matches))
        self.assertEqual([], vetoes)

        groups = cluster(signatures, matches)
        self.assertEqual(1, len(groups))
        self.assertEqual(["/a-copy.jpg", "/a-no-exif.jpg", "/a-small.jpg", "/a.jpg"], groups[0].paths)
        self.assertEqual(MatchReason.NEAR_DUPLICATE, groups[0].reason)

    def test_pixel_identical_is_verified_with_resolver(self):
        signatures = [
            _signature("/a.jpg", preview="same", phash=1),
            _signature("/b.jpg", preview="same", phash=1),
        ]
        matches, _ = find_matches(signatures, MatchingThresholds(), lambda s: s.path)
        # preview hashes collide but full pixels differ: only a near-duplicate
        self.assertEqual([MatchReason.NEAR_DUPLICATE], [m.reason for m in matches])

        matches, _ = find_matches(signatures, MatchingThresholds(), lambda s: "identical")
        self.assertEqual([MatchReason.PIXEL_IDENTICAL], [m.reason for m in matches])

    def test_burst_is_vetoed(self):
        later = ImageMetadata(camera_make=CAMERA.camera_make, camera_model=CAMERA.camera_model,
                              capture_time=CAMERA.capture_time.replace(second=52), capture_time_has_subsec=True)
        signatures = [_signature("/1.jpg", metadata=CAMERA), _signature("/2.jpg", phash=1, metadata=later)]
        matches, vetoes = find_matches(signatures, MatchingThresholds())
        self.assertEqual([], matches)
        self.assertEqual(1, len(vetoes))
        self.assertIn("burst", vetoes[0].reason)

    def test_confidence_notes(self):
        a = _signature("/a.jpg", metadata=CAMERA)
        b = _signature("/b.jpg", metadata=CAMERA, grayscale=True)
        c = _signature("/c.jpg", width=3000, height=3000)
        thresholds = MatchingThresholds()
        same_capture, notes = near_duplicate_confidence(a, b, 0, 0, thresholds)
        self.assertEqual((NOTE_SAME_CAPTURE, NOTE_COLOUR), notes)
        plain, notes = near_duplicate_confidence(a, _signature("/x.jpg"), 0, 0, thresholds)
        self.assertEqual((), notes)
        self.assertGreater(same_capture, plain)
        far, _ = near_duplicate_confidence(a, _signature("/x.jpg"), 10, 12, thresholds)
        self.assertLess(far, plain)
        cropped, notes = near_duplicate_confidence(a, c, 0, 0, thresholds)
        self.assertLess(cropped, plain)
        self.assertEqual(1, len(notes))


class ClusterTest(unittest.TestCase):

    def test_transitive_groups(self):
        signatures = [_signature(p) for p in ("/a", "/b", "/c", "/d", "/e")]
        matches = [
            Match("/a", "/b", MatchReason.EXACT, 1.0),
            Match("/b", "/c", MatchReason.NEAR_DUPLICATE, 0.7),
            Match("/d", "/e", MatchReason.NEAR_DUPLICATE, 0.8),
        ]
        groups = cluster(signatures, matches)
        self.assertEqual([["/a", "/b", "/c"], ["/d", "/e"]], [g.paths for g in groups])
        self.assertEqual(0.7, groups[0].confidence)
        self.assertEqual(MatchReason.NEAR_DUPLICATE, groups[0].reason)
        self.assertEqual(MatchReason.EXACT, cluster(signatures, matches[:1])[0].reason)

    def test_chain_through_a_burst_is_not_joined(self):
        """
        A and C are burst shots, B is a messenger copy without metadata that is similar to both:
        B must not join A and C into one group.
        """
        burst_c = ImageMetadata(camera_make=CAMERA.camera_make, camera_model=CAMERA.camera_model,
                                capture_time=CAMERA.capture_time.replace(second=53), capture_time_has_subsec=True)
        signatures = [_signature("/a", metadata=CAMERA), _signature("/b"), _signature("/c", metadata=burst_c)]
        matches = [
            Match("/a", "/b", MatchReason.NEAR_DUPLICATE, 0.9),
            Match("/b", "/c", MatchReason.NEAR_DUPLICATE, 0.6),
        ]
        groups = cluster(signatures, matches)
        self.assertEqual([["/a", "/b"]], [g.paths for g in groups])
        self.assertEqual([matches[1]], groups[0].conflicts)


class EvaluationTest(unittest.TestCase):

    def test_pairs_and_metrics(self):
        expected = pairs_of_groups(ground_truth_by_folder(["/x/1", "/x/2", "/x/3", "/y/1"]))
        self.assertEqual({("/x/1", "/x/2"), ("/x/1", "/x/3"), ("/x/2", "/x/3")}, expected)
        metrics = evaluate_pairs({("/x/1", "/x/2"), ("/x/1", "/y/1")}, expected)
        self.assertEqual(0.5, metrics.precision)
        self.assertAlmostEqual(1 / 3, metrics.recall)


class PipelineTest(TempDirTestCase):

    def test_test_images(self):
        """
        tests/images: each folder holds one photo (original, edited, grayscale, telegram copy).
        """
        store = SignatureStore(":memory:")
        self.addCleanup(store.close)
        pipeline = MatchingPipeline(store)
        files = find_image_files(IMAGES)
        self.assertEqual(12, len(files))

        analysis = pipeline.analyse(files)
        self.assertEqual((12, 0, {}), (analysis.analysed, analysis.skipped_unchanged, analysis.failed))

        result = pipeline.match()
        self.assertEqual(3, len(result.groups))
        expected = pairs_of_groups(ground_truth_by_folder([str(f) for f in files]))
        metrics = evaluate_pairs(pairs_of_groups(g.paths for g in result.groups), expected)
        self.assertEqual((1.0, 1.0), (metrics.precision, metrics.recall))
        for group in result.groups:
            self.assertEqual(MatchReason.NEAR_DUPLICATE, group.reason)
            self.assertGreater(group.confidence, 0.5)

        # unchanged files are not analysed again
        analysis = pipeline.analyse(files)
        self.assertEqual((0, 12), (analysis.analysed, analysis.skipped_unchanged))

    def test_incremental_analysis_and_exact_copies(self):
        store = SignatureStore(self.temp_dir / "signatures.sqlite")
        self.addCleanup(store.close)
        pipeline = MatchingPipeline(store, workers=2)
        original = self.small_copy("original.jpg")
        shutil.copy(original, self.temp_dir / "copy.jpg")
        broken = self.temp_dir / "broken.jpg"
        broken.write_bytes(b"not an image")

        analysis = pipeline.analyse(find_image_files(self.temp_dir))
        self.assertEqual(2, analysis.analysed)
        self.assertEqual([str(broken)], list(analysis.failed))
        groups = pipeline.match().groups
        self.assertEqual(1, len(groups))
        self.assertEqual(MatchReason.EXACT, groups[0].reason)

        # a lossless copy (PNG) is pixel-identical, verified with a full decode
        with Image.open(original) as image:
            ImageOps.exif_transpose(image).save(self.temp_dir / "lossless.png")
        (self.temp_dir / "copy.jpg").unlink()
        analysis = pipeline.analyse(find_image_files(self.temp_dir))
        self.assertEqual((1, 1, 1), (analysis.analysed, analysis.skipped_unchanged, analysis.removed_missing))
        groups = pipeline.match().groups
        self.assertEqual(1, len(groups))
        self.assertEqual(MatchReason.PIXEL_IDENTICAL, groups[0].reason)
        self.assertIsNotNone(store.get(str(original)).full_pixel_hash)


class BenchmarkTest(unittest.TestCase):

    def test_synthetic_benchmark_runs(self):
        import contextlib
        import io
        from argparse import Namespace

        from py_image_dedup.matching.benchmark import synthetic_benchmark
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            synthetic_benchmark(300, Namespace(max_phash=10, max_dhash=12))
        self.assertIn("candidate search", output.getvalue())
