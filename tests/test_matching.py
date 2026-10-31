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
from dataclasses import replace
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image, ImageOps

from py_image_dedup.matching import (
    ImageMetadata, ImageSignature, Match, MatchReason, MatchingPipeline, MatchingThresholds, Orientation,
    SignatureStore, VariantKind, burst_note, classify_variant, cluster, compute_signature, find_image_files,
    find_matches,
)
from py_image_dedup.matching.evaluation import evaluate_pairs, ground_truth_by_folder, pairs_of_groups
from py_image_dedup.matching.grouping import (
    NOTE_COLOUR, NOTE_COLOUR_EDITED, NOTE_SAME_CAPTURE, near_duplicate_confidence,
)
from py_image_dedup.matching.metadata import parse_capture_time
from py_image_dedup.matching.orientation import (
    TRANSFORMS, decode_orientation_hashes, dihedral_hashes, encode_orientation_hashes,
)
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
               grayscale: bool = False, orientation_hashes: tuple = None,
               colour_moments: tuple = None) -> ImageSignature:
    return ImageSignature(path=path, file_size=1, mtime_ns=1, sha256=sha256 or f"sha-{path}",
                          preview_pixel_hash=preview or f"px-{path}", phash=phash, dhash=dhash, width=width,
                          height=height, is_grayscale=grayscale, metadata=metadata or ImageMetadata(),
                          orientation_hashes=orientation_hashes, colour_moments=colour_moments)


def _at(seconds: float, subsec: bool = True, model: str = "ONEPLUS A6013") -> ImageMetadata:
    """
    :return: metadata of a photo of the same camera, taken `seconds` after 19:31:50
    """
    base = datetime(2019, 9, 3, 19, 31, 50)
    return ImageMetadata(camera_make="OnePlus", camera_model=model, capture_time_has_subsec=subsec,
                         capture_time=base.replace(second=50 + int(seconds),
                                                   microsecond=int(round(seconds % 1 * 1_000_000))))


_FAR = (1 << 64) - 1
# 32 bits away from 0, _FAR and _HALF
_FILL = 0xFFFF0000FFFF0000
_HALF = 0xFFFFFFFF


def _rotated_hashes(orientation: Orientation, phash: int, dhash: int) -> tuple:
    """
    :return: orientation_hashes in which `orientation` has the given hashes and all others are far away
    """
    return tuple((phash, dhash) if o is orientation else (_FILL, _FILL) for o in TRANSFORMS)


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

    def test_orientation_hashes_and_colour_moments(self):
        signature = self.original
        self.assertEqual(len(TRANSFORMS), len(signature.orientation_hashes))
        self.assertEqual(6, len(signature.colour_moments))
        # the identity hashes are the imagehash ones
        with Image.open(ORIGINAL) as image:
            image.draft("RGB", (512, 512))
            preview = ImageOps.exif_transpose(image).convert("RGB")
        self.assertEqual(hash_to_int(imagehash.phash(preview)), signature.phash)
        self.assertEqual(hash_to_int(imagehash.dhash(preview)), signature.dhash)

        without = compute_signature(ORIGINAL, orientations=False)
        self.assertIsNone(without.orientation_hashes)
        self.assertEqual((signature.phash, signature.dhash), (without.phash, without.dhash))

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


class OrientationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with Image.open(TELEGRAM) as image:
            cls.image = image.convert("RGB")
        cls.hashes = dihedral_hashes(cls.image)

    def test_identity_is_imagehash(self):
        self.assertEqual((hash_to_int(imagehash.phash(self.image)), hash_to_int(imagehash.dhash(self.image))),
                         self.hashes[Orientation.IDENTITY])

    def test_hashes_of_transformed_image(self):
        """
        The derived hashes are (almost) the hashes of the physically rotated/mirrored image.
        """
        for orientation in Orientation:
            transformed = orientation.apply_to_image(self.image)
            phash, dhash = self.hashes[orientation]
            self.assertLessEqual(hamming_distance(hash_to_int(imagehash.phash(transformed)), phash), 2, orientation)
            self.assertLessEqual(hamming_distance(hash_to_int(imagehash.dhash(transformed)), dhash), 2, orientation)
            self.assertEqual(orientation.transposes, transformed.size != self.image.size, orientation)

    def test_transforms_and_inverse(self):
        array = np.arange(12).reshape(3, 4)
        results = set()
        for orientation in Orientation:
            transformed = orientation.apply(array)
            results.add(transformed.tobytes() + bytes(transformed.shape))
            np.testing.assert_array_equal(array, orientation.inverse.apply(transformed), str(orientation))
            # numpy and PIL implementations agree
            image = Image.fromarray(array.astype(np.uint8))
            np.testing.assert_array_equal(transformed, np.asarray(orientation.apply_to_image(image)), str(orientation))
        self.assertEqual(8, len(results))
        np.testing.assert_array_equal(np.rot90(array, k=-1), Orientation.ROTATE_90.apply(array))

    def test_encoding_roundtrip(self):
        hashes = tuple((_FAR - i, i << 60) for i in range(len(TRANSFORMS)))
        self.assertEqual(hashes, decode_orientation_hashes(encode_orientation_hashes(hashes)))


class StoreTest(unittest.TestCase):

    def setUp(self):
        self.store = SignatureStore(":memory:")
        self.addCleanup(self.store.close)

    def test_roundtrip_with_unsigned_64_bit_hashes(self):
        signature = _signature("/a.jpg", phash=(1 << 64) - 1, dhash=1 << 63, metadata=CAMERA, grayscale=True,
                               orientation_hashes=tuple((_FAR - i, i) for i in range(len(TRANSFORMS))),
                               colour_moments=(1.5, 2.0, 255.0, 0.0, 10.1, 3.3))
        self.store.upsert([signature])
        self.assertEqual(signature, self.store.get("/a.jpg"))
        self.assertEqual([signature], self.store.get_all())

    def test_store_of_schema_version_1_is_rebuilt(self):
        """
        Stores of the first prototype lack the orientation/colour columns: they are rebuilt (re-analysed).
        """
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "old.sqlite"
            import sqlite3
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE files (path TEXT PRIMARY KEY, file_size INTEGER, mtime_ns INTEGER, "
                               "sha256 TEXT, preview_pixel_hash TEXT, full_pixel_hash TEXT, phash INTEGER, "
                               "dhash INTEGER, width INTEGER, height INTEGER, is_grayscale INTEGER, "
                               "capture_time TEXT, capture_time_has_subsec INTEGER, camera_make TEXT, "
                               "camera_model TEXT, image_unique_id TEXT, software TEXT, signature_version INTEGER)")
            connection.execute("INSERT INTO files VALUES ('/a.jpg', 1, 1, 's', 'p', NULL, 1, 2, 4, 3, 0, NULL, 0, "
                               "NULL, NULL, NULL, NULL, 1)")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()

            store = SignatureStore(db)
            self.addCleanup(store.close)
            self.assertIsNone(store.get("/a.jpg"))
            store.upsert([_signature("/b.jpg", orientation_hashes=((1, 2),) * len(TRANSFORMS))])
            self.assertEqual(((1, 2),) * len(TRANSFORMS), store.get("/b.jpg").orientation_hashes)

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

    def test_query_hashes(self):
        """
        With query hashes, row i is compared with query[i] instead of hashes[i] (still only i < j).
        """
        hashes = to_array([0, 0b1111, 1 << 40])
        query = to_array([0b1110, 1 << 40, 7])
        pairs = find_candidate_pairs(hashes, hashes, 1, 1, query_phashes=query, query_dhashes=query)
        self.assertEqual([(0, 1, 1, 1), (1, 2, 0, 0)], [(c.i, c.j, c.phash_distance, c.dhash_distance) for c in pairs])
        with self.assertRaises(ValueError):
            find_candidate_pairs(hashes, hashes, 1, 1, query_phashes=query)

    def test_high_bit_hashes(self):
        hashes = to_array([(1 << 64) - 1, (1 << 64) - 2, 0])
        pairs = find_candidate_pairs(hashes, hashes, 1, 1)
        self.assertEqual([(0, 1, 1, 1)], [(c.i, c.j, c.phash_distance, c.dhash_distance) for c in pairs])
        self.assertIsInstance(hashes, np.ndarray)


class BurstTest(unittest.TestCase):

    def test_same_camera_seconds_apart_is_a_burst(self):
        self.assertEqual("burst: same camera (OnePlus ONEPLUS A6013), 1.5 s apart", burst_note(_at(0), _at(1.5), 10))
        self.assertIsNotNone(burst_note(_at(0), _at(0.1), 10))
        self.assertEqual("burst: same camera (OnePlus ONEPLUS A6013), 3 s apart", burst_note(_at(3), _at(0), 10))

    def test_no_burst(self):
        # same capture time: a copy
        self.assertIsNone(burst_note(_at(0), _at(0), 10))
        # outside of the window
        self.assertIsNone(burst_note(_at(0), _at(9), 5))
        # different camera
        self.assertIsNone(burst_note(_at(0), _at(1, model="Pixel 7"), 10))
        # metadata missing (e.g. messenger copy)
        self.assertIsNone(burst_note(_at(0), ImageMetadata(), 10))
        # only one file has sub-seconds: compared at second precision
        self.assertIsNone(burst_note(_at(0.3), _at(0, subsec=False), 10))

    def test_burst_is_a_confidence_factor_not_a_veto(self):
        signatures = [_signature("/1.jpg", metadata=_at(0)), _signature("/2.jpg", phash=1, metadata=_at(3))]
        matches = find_matches(signatures, MatchingThresholds())
        self.assertEqual(1, len(matches))
        burst = matches[0]
        self.assertEqual(VariantKind.BURST, burst.kind)
        self.assertIn("burst: same camera (OnePlus ONEPLUS A6013), 3 s apart", burst.notes)
        plain = find_matches([signatures[0], _signature("/2.jpg", phash=1)], MatchingThresholds())[0]
        self.assertAlmostEqual(plain.confidence - 0.3, burst.confidence)

        # penalty and window are configurable
        lenient = find_matches(signatures, MatchingThresholds(burst_confidence_penalty=0.1))[0]
        self.assertAlmostEqual(plain.confidence - 0.1, lenient.confidence)
        outside = find_matches(signatures, MatchingThresholds(burst_window_seconds=2))[0]
        self.assertEqual(VariantKind.COPY, outside.kind)
        self.assertEqual(plain.confidence, outside.confidence)

        groups = cluster(signatures, matches)
        self.assertEqual(1, len(groups))
        self.assertFalse(groups[0].auto_act_allowed)
        self.assertIn("burst: same camera (OnePlus ONEPLUS A6013), 3 s apart", groups[0].review_reasons)
        self.assertEqual({"/1.jpg": VariantKind.BURST, "/2.jpg": VariantKind.BURST}, groups[0].member_kinds)

    def test_burst_shots_never_auto_act_even_with_high_confidence(self):
        signatures = [_signature("/1.jpg", metadata=_at(0)), _signature("/2.jpg", metadata=_at(1))]
        group = cluster(signatures, [Match("/1.jpg", "/2.jpg", MatchReason.NEAR_DUPLICATE, 0.95)])[0]
        self.assertFalse(group.auto_act_allowed)
        self.assertEqual("burst: same camera (OnePlus ONEPLUS A6013), 1 s apart", group.review_reason)


class MatchingTest(unittest.TestCase):

    def test_stages_and_reasons(self):
        signatures = [
            _signature("/a.jpg", phash=0b1111, sha256="same", preview="p1"),
            _signature("/a-copy.jpg", phash=0b1111, sha256="same", preview="p1"),
            _signature("/a-no-exif.jpg", phash=0b1111, sha256="other", preview="p1"),
            _signature("/a-small.jpg", phash=0b0111, dhash=0b1),
            _signature("/unrelated.jpg", phash=(1 << 64) - 1, dhash=(1 << 64) - 1),
        ]
        matches = find_matches(signatures, MatchingThresholds())
        by_pair = {(m.a, m.b): m for m in matches}
        self.assertEqual(MatchReason.EXACT, by_pair[("/a-copy.jpg", "/a.jpg")].reason)
        self.assertEqual(1.0, by_pair[("/a-copy.jpg", "/a.jpg")].confidence)
        self.assertEqual(MatchReason.PIXEL_IDENTICAL, by_pair[("/a-no-exif.jpg", "/a.jpg")].reason)
        near = by_pair[("/a-small.jpg", "/a.jpg")]
        self.assertEqual(MatchReason.NEAR_DUPLICATE, near.reason)
        self.assertEqual((1, 1), (near.phash_distance, near.dhash_distance))
        self.assertLess(near.confidence, 0.99)
        self.assertFalse(any("/unrelated.jpg" in (m.a, m.b) for m in matches))
        self.assertEqual([VariantKind.EXACT, VariantKind.PIXEL_IDENTICAL, VariantKind.COPY],
                         [by_pair[p].kind for p in (("/a-copy.jpg", "/a.jpg"), ("/a-no-exif.jpg", "/a.jpg"),
                                                    ("/a-small.jpg", "/a.jpg"))])

        groups = cluster(signatures, matches)
        self.assertEqual(1, len(groups))
        self.assertEqual(["/a-copy.jpg", "/a-no-exif.jpg", "/a-small.jpg", "/a.jpg"], groups[0].paths)
        self.assertEqual(MatchReason.NEAR_DUPLICATE, groups[0].reason)
        self.assertTrue(groups[0].auto_act_allowed)
        self.assertIsNone(groups[0].review_reason)
        self.assertEqual({"/a-copy.jpg": VariantKind.EXACT, "/a-no-exif.jpg": VariantKind.PIXEL_IDENTICAL,
                          "/a-small.jpg": VariantKind.COPY, "/a.jpg": VariantKind.EXACT}, groups[0].member_kinds)

    def test_pixel_identical_is_verified_with_resolver(self):
        signatures = [
            _signature("/a.jpg", preview="same", phash=1),
            _signature("/b.jpg", preview="same", phash=1),
        ]
        matches = find_matches(signatures, MatchingThresholds(), lambda s: s.path)
        # preview hashes collide but full pixels differ: only a near-duplicate
        self.assertEqual([MatchReason.NEAR_DUPLICATE], [m.reason for m in matches])

        matches = find_matches(signatures, MatchingThresholds(), lambda s: "identical")
        self.assertEqual([MatchReason.PIXEL_IDENTICAL], [m.reason for m in matches])

    def test_rotated_copies_are_matched(self):
        """
        b is a rotated (90° clockwise) copy of a: a's ROTATE_90 hashes are close to b's upright hashes.
        """
        a = _signature("/a.jpg", phash=_FAR, dhash=_FAR,
                       orientation_hashes=_rotated_hashes(Orientation.ROTATE_90, 0b1, 0), width=4000, height=3000)
        b = _signature("/b.jpg", width=3000, height=4000,
                       orientation_hashes=_rotated_hashes(Orientation.MIRROR, _FILL, _FILL))
        unrelated = _signature("/c.jpg", phash=_HALF, dhash=_HALF,
                               orientation_hashes=_rotated_hashes(Orientation.MIRROR, _FILL, _FILL))
        matches = find_matches([a, b, unrelated])
        self.assertEqual(1, len(matches))
        match = matches[0]
        self.assertEqual(("/a.jpg", "/b.jpg"), (match.a, match.b))
        self.assertEqual(Orientation.ROTATE_90, match.orientation)
        self.assertEqual(VariantKind.ROTATED, match.kind)
        self.assertEqual((1, 0), (match.phash_distance, match.dhash_distance))
        self.assertIn("rotated 90°", match.notes)
        # width and height are swapped by the rotation: not a crop
        self.assertNotIn("aspect ratio differs (cropped?)", match.notes)
        group = cluster([a, b, unrelated], matches)[0]
        self.assertTrue(group.auto_act_allowed)
        self.assertEqual({"/a.jpg": VariantKind.ROTATED, "/b.jpg": VariantKind.ROTATED}, group.member_kinds)

        # the orientation always turns match.a into match.b: from b's point of view it is the inverse
        renamed = find_matches([replace(a, path="/z.jpg"), b])[0]
        self.assertEqual(("/b.jpg", "/z.jpg"), (renamed.a, renamed.b))
        self.assertEqual(Orientation.ROTATE_270, renamed.orientation)
        self.assertIn("rotated 270°", renamed.notes)

        # option off, or no orientation hashes: no match
        self.assertEqual([], find_matches([a, b], MatchingThresholds(match_rotations=False)))
        self.assertEqual([], find_matches([replace(a, orientation_hashes=None), b]))

    def test_upright_comparison_wins(self):
        a = _signature("/a.jpg", orientation_hashes=_rotated_hashes(Orientation.MIRROR, 0, 0))
        b = _signature("/b.jpg", phash=0b11)
        match = find_matches([a, b])[0]
        self.assertEqual(Orientation.IDENTITY, match.orientation)
        self.assertEqual(VariantKind.COPY, match.kind)

    def test_variant_classification(self):
        thresholds = MatchingThresholds()
        colours = (100.0, 100.0, 100.0, 40.0, 40.0, 40.0)
        a = _signature("/a.jpg", colour_moments=colours)
        cases = {
            VariantKind.COPY: _signature("/b.jpg", colour_moments=(101.0, 99.0, 100.0, 41.0, 40.0, 39.0)),
            VariantKind.COLOUR_EDITED: _signature("/b.jpg", colour_moments=(120.0, 100.0, 100.0, 40.0, 40.0, 40.0)),
            VariantKind.GRAYSCALE: _signature("/b.jpg", grayscale=True, colour_moments=(95.0,) * 3 + (30.0,) * 3),
            VariantKind.CROP: _signature("/b.jpg", width=3000, height=3000, colour_moments=colours),
            VariantKind.BURST: _signature("/b.jpg", width=3000, height=3000, metadata=_at(2)),
        }
        for expected, b in cases.items():
            source = replace(a, metadata=_at(0)) if expected is VariantKind.BURST else a
            self.assertEqual(expected, classify_variant(source, b, thresholds), expected)
        # without colour moments the colours are not compared
        self.assertEqual(VariantKind.COPY, classify_variant(a, _signature("/b.jpg"), thresholds))
        self.assertEqual(VariantKind.ROTATED, classify_variant(a, cases[VariantKind.COPY], thresholds,
                                                               Orientation.MIRROR))
        self.assertEqual(VariantKind.COLOUR_EDITED,
                         classify_variant(a, cases[VariantKind.COLOUR_EDITED], thresholds, Orientation.MIRROR))
        _, notes = near_duplicate_confidence(a, cases[VariantKind.COLOUR_EDITED], 0, 0, thresholds)
        self.assertEqual((NOTE_COLOUR_EDITED,), notes)
        self.assertEqual(VariantKind.COPY, classify_variant(a, cases[VariantKind.COLOUR_EDITED],
                                                            MatchingThresholds(colour_edit_tolerance=25)))

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

    def test_chain_through_a_burst_goes_to_review(self):
        """
        A and C are burst shots, B is a messenger copy without metadata that is similar to both:
        all three are grouped, but the group is never acted on automatically.
        """
        signatures = [_signature("/a", metadata=_at(0)), _signature("/b"), _signature("/c", metadata=_at(3))]
        matches = [
            Match("/a", "/b", MatchReason.NEAR_DUPLICATE, 0.9),
            Match("/b", "/c", MatchReason.NEAR_DUPLICATE, 0.85),
        ]
        groups = cluster(signatures, matches)
        self.assertEqual([["/a", "/b", "/c"]], [g.paths for g in groups])
        self.assertFalse(groups[0].auto_act_allowed)
        self.assertEqual(["burst: same camera (OnePlus ONEPLUS A6013), 3 s apart"], groups[0].review_reasons)
        self.assertEqual({"/a": VariantKind.BURST, "/b": VariantKind.COPY, "/c": VariantKind.BURST},
                         groups[0].member_kinds)

    def test_review_reasons(self):
        signatures = [_signature("/a.jpg"), _signature("/b.jpg"), _signature("/gray.jpg", grayscale=True),
                      _signature("/edited.jpg")]
        matches = [
            Match("/a.jpg", "/b.jpg", MatchReason.NEAR_DUPLICATE, 0.9),
            Match("/a.jpg", "/gray.jpg", MatchReason.NEAR_DUPLICATE, 0.85, kind=VariantKind.GRAYSCALE),
            Match("/b.jpg", "/gray.jpg", MatchReason.NEAR_DUPLICATE, 0.85, kind=VariantKind.GRAYSCALE),
            Match("/a.jpg", "/edited.jpg", MatchReason.NEAR_DUPLICATE, 0.82, kind=VariantKind.COLOUR_EDITED),
        ]
        group = cluster(signatures, matches)[0]
        self.assertEqual({"/a.jpg": VariantKind.COPY, "/b.jpg": VariantKind.COPY, "/gray.jpg": VariantKind.GRAYSCALE,
                          "/edited.jpg": VariantKind.COLOUR_EDITED}, group.member_kinds)
        self.assertEqual(["grayscale variant: gray.jpg", "colour-edited variant: edited.jpg"], group.review_reasons)
        self.assertFalse(group.auto_act_allowed)

        # a grayscale pair: the grayscale match is the only relation of the colour file
        pair = cluster(signatures, matches[1:2])[0]
        self.assertEqual({"/a.jpg": VariantKind.GRAYSCALE, "/gray.jpg": VariantKind.GRAYSCALE}, pair.member_kinds)
        self.assertEqual(["grayscale variant: a.jpg, gray.jpg"], pair.review_reasons)

        # plain copies with a low confidence
        low = cluster(signatures, [Match("/a.jpg", "/b.jpg", MatchReason.NEAR_DUPLICATE, 0.7)])[0]
        self.assertEqual(["confidence 0.70 below 0.80"], low.review_reasons)
        self.assertTrue(cluster(signatures, [Match("/a.jpg", "/b.jpg", MatchReason.NEAR_DUPLICATE, 0.7)],
                                MatchingThresholds(auto_act_min_confidence=0.6))[0].auto_act_allowed)


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
            # grayscale and edited versions are variants to keep: never acted on automatically
            self.assertFalse(group.auto_act_allowed)
            kinds = {Path(p).stem.rsplit("-", 1)[-1] if "-" in Path(p).stem else "original": k
                     for p, k in group.member_kinds.items()}
            self.assertEqual({"original": VariantKind.COPY, "compression": VariantKind.COPY,
                              "grayscale": VariantKind.GRAYSCALE, "edited": VariantKind.COLOUR_EDITED}, kinds)
            self.assertIn("grayscale variant", group.review_reason)
            self.assertIn("colour-edited variant", group.review_reason)

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


class RotationPipelineTest(TempDirTestCase):

    def test_rotated_and_mirrored_copies_without_orientation_tag(self):
        original = self.small_copy("original.jpg", quality=95)
        with Image.open(original) as image:
            upright = ImageOps.exif_transpose(image).convert("RGB")
        for orientation in (Orientation.ROTATE_90, Orientation.MIRROR, Orientation.MIRROR_ROTATE_270):
            orientation.apply_to_image(upright).save(self.temp_dir / f"{orientation.name.lower()}.jpg", quality=90)
        files = find_image_files(self.temp_dir)

        store = SignatureStore(":memory:")
        self.addCleanup(store.close)
        pipeline = MatchingPipeline(store, workers=2)
        pipeline.analyse(files)
        groups = pipeline.match().groups
        self.assertEqual(1, len(groups))
        group = groups[0]
        self.assertEqual(4, len(group.paths))
        self.assertTrue(group.auto_act_allowed, group.review_reason)
        by_pair = {(Path(m.a).stem, Path(m.b).stem): m for m in group.matches}
        self.assertEqual(Orientation.ROTATE_90, by_pair[("original", "rotate_90")].orientation)
        self.assertIn("mirrored", by_pair[("mirror", "original")].notes)
        self.assertEqual(VariantKind.ROTATED, by_pair[("mirror", "original")].kind)

        # option off: the rotated copies are not found
        store_off = SignatureStore(":memory:")
        self.addCleanup(store_off.close)
        pipeline_off = MatchingPipeline(store_off, MatchingThresholds(match_rotations=False), workers=2)
        pipeline_off.analyse(files)
        self.assertEqual([], pipeline_off.match().groups)
        self.assertIsNone(store_off.get(str(original)).orientation_hashes)


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
        self.assertIn("rotations on", output.getvalue())

    def test_generate_variants(self):
        from py_image_dedup.matching.benchmark import ROBUSTNESS_VARIANTS, generate_variants
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "IMG.jpg"
            with Image.open(ORIGINAL) as image:
                exif = image.getexif()
                image.thumbnail((200, 200))
                image.save(source, exif=exif)
            target = Path(directory) / "variants"
            target.mkdir()
            variants = generate_variants(source, target)
            self.assertEqual(set(ROBUSTNESS_VARIANTS), set(variants))
            self.assertTrue(all(p.is_file() for p in variants.values()))
            self.assertEqual(compute_signature(source).sha256, compute_signature(variants["exact-copy"]).sha256)
            self.assertIsNone(compute_signature(variants["exif-stripped"]).metadata.capture_time)
            burst = compute_signature(variants["burst-1s"]).metadata
            self.assertIsNotNone(burst_note(compute_signature(source).metadata, burst, 10))
