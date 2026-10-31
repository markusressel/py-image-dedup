import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from py_image_dedup.config import MATCHING_BACKEND_BOTH, MATCHING_BACKEND_HASHES, MATCHING_BACKEND_IMAGE_MATCH
from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
from py_image_dedup.library.hash_matcher import HashMatcher, create_embedding_stage, member_match_info
from py_image_dedup.matching.embeddings import ModelNotAvailableError
from py_image_dedup.matching.store import SqliteSignatureStore
from py_image_dedup.state import StateStore, GroupStatus
from py_image_dedup.state.store import GroupNotActionableError, group_key
from py_image_dedup.web.app import create_app
from tests import TestBase

TEST_IMAGES = Path(__file__).parent / "images"


def _generated_image(path: Path, seed: int):
    """
    Writes a photo-like test image that does not look like any of tests/images.
    """
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:240, 0:320]
    base = np.stack([(x * 0.8 + seed * 40) % 256, (y + x * 0.3) % 256, (y * 0.9 + 60) % 256], axis=-1)
    for _ in range(12):
        cx, cy, r = rng.integers(0, 320), rng.integers(0, 240), rng.integers(10, 60)
        mask = (x - cx) ** 2 + (y - cy) ** 2 < r ** 2
        base[mask] = rng.integers(0, 256, 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(base.astype(np.uint8)).save(path, quality=92)


class _ConfigMixin:
    """
    Saves and restores the config entries changed by a test (DeduplicatorConfig is a singleton).
    """

    def _save_config(self, *entries):
        original = {entry: entry.value for entry in entries}

        def restore():
            for entry, value in original.items():
                entry.value = value

        self.addCleanup(restore)


class MatchingConfigTest(_ConfigMixin, TestBase):

    def setUp(self):
        super().setUp()
        c = self.config
        self._save_config(c.MATCHING_BACKEND, c.MATCHING_STORE_URL, c.MATCHING_MAX_PHASH_DISTANCE,
                          c.MATCHING_BURST_WINDOW_SECONDS, c.MATCHING_MATCH_ROTATIONS, c.STATE_DATABASE_PATH,
                          c.MATCHING_AUTO_ACT_MIN_CONFIDENCE, c.MATCHING_EMBEDDINGS_ENABLED,
                          c.MATCHING_EMBEDDINGS_MODEL_PATH, c.MATCHING_EMBEDDINGS_MODEL_URL,
                          c.MATCHING_EMBEDDINGS_MODEL_SHA256, c.MATCHING_EMBEDDINGS_CACHE_DIRECTORY)

    def test_defaults(self):
        self.assertEqual(MATCHING_BACKEND_IMAGE_MATCH, self.config.matching_backend)
        self.assertTrue(self.config.uses_image_match)
        self.assertFalse(self.config.uses_hashes)
        self.assertEqual("image-match", self.config.acting_matcher)
        self.assertFalse(self.config.MATCHING_EMBEDDINGS_ENABLED.value)
        # dry run stays the default
        self.assertTrue(self.config.DRY_RUN.default)

    def test_backend_values(self):
        self.config.MATCHING_BACKEND.value = "both"
        self.assertTrue(self.config.uses_image_match)
        self.assertTrue(self.config.uses_hashes)
        self.assertEqual("image-match", self.config.acting_matcher)
        self.config.MATCHING_BACKEND.value = "hashes"
        self.assertFalse(self.config.uses_image_match)
        self.assertEqual("hashes", self.config.acting_matcher)
        with self.assertRaises(ValueError):
            self.config.MATCHING_BACKEND.value = "elasticsearch"

    def test_store_url_default_is_a_separate_file_next_to_the_state_database(self):
        self.config.STATE_DATABASE_PATH.value = "/data/state/state.db"
        self.config.MATCHING_STORE_URL.value = None
        self.assertEqual("/data/state/signatures.db", self.config.signature_store_url)
        self.assertEqual(Path("/data/state/embeddings"), self.config.embeddings_cache_directory)
        self.config.MATCHING_STORE_URL.value = "postgresql://u:p@db/x"
        self.assertEqual("postgresql://u:p@db/x", self.config.signature_store_url)

    def test_thresholds(self):
        # values as they come from environment variables
        self.config.MATCHING_MAX_PHASH_DISTANCE.value = "8"
        self.config.MATCHING_BURST_WINDOW_SECONDS.value = "5.5"
        self.config.MATCHING_MATCH_ROTATIONS.value = "false"
        self.config.MATCHING_AUTO_ACT_MIN_CONFIDENCE.value = "0.9"
        thresholds = self.config.matching_thresholds()
        self.assertEqual(8, thresholds.max_phash_distance)
        self.assertEqual(12, thresholds.max_dhash_distance)
        self.assertEqual(5.5, thresholds.burst_window_seconds)
        self.assertFalse(thresholds.match_rotations)
        self.assertEqual(0.9, thresholds.auto_act_min_confidence)
        with self.assertRaises(ValueError):
            self.config.MATCHING_MAX_PHASH_DISTANCE.value = 65

    def test_embeddings_disabled_by_default(self):
        self.assertIsNone(create_embedding_stage(self.config))

    def test_embeddings_config_errors(self):
        cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cache)
        self.config.MATCHING_EMBEDDINGS_ENABLED.value = True
        self.config.MATCHING_EMBEDDINGS_CACHE_DIRECTORY.value = cache
        self.config.MATCHING_EMBEDDINGS_MODEL_URL.value = "https://example.invalid/model.onnx"
        self.config.MATCHING_EMBEDDINGS_MODEL_SHA256.value = None
        with self.assertRaises(ValueError):
            create_embedding_stage(self.config)
        with self.assertRaises(ValueError):
            self.config.MATCHING_EMBEDDINGS_MODEL_SHA256.value = "not-a-checksum"

        self.config.MATCHING_EMBEDDINGS_MODEL_URL.value = None
        self.config.MATCHING_EMBEDDINGS_MODEL_PATH.value = str(Path(cache) / "missing.onnx")
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            self.skipTest("needs the embeddings extra")
        with self.assertRaises(ModelNotAvailableError):
            create_embedding_stage(self.config)


class _HashBackendTestBase(_ConfigMixin, TestBase):
    """
    Runs the deduplicator over a copy of tests/images (never the original files).
    """

    backend = MATCHING_BACKEND_BOTH

    def setUp(self):
        super().setUp()
        c = self.config
        self._save_config(c.MATCHING_BACKEND, c.MATCHING_STORE_URL, c.SOURCE_DIRECTORIES, c.DRY_RUN,
                          c.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY, c.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS,
                          c.MAX_FILE_MODIFICATION_TIME_DELTA, c.DRY_RUN_USER_DECISIONS, c.SEARCH_ACROSS_ROOT_DIRS)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.source = self.tmp / "library"
        shutil.copytree(TEST_IMAGES, self.source)
        self.target = self.tmp / "duplicates"
        self.target.mkdir()

        # not part of tests/py_image_dedup.yaml, so creating a DeduplicatorConfig (which reloads it) keeps them
        c.MATCHING_BACKEND.value = self.backend
        c.MATCHING_STORE_URL.value = ":memory:"

        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.dedup = ImageMatchDeduplicator(interactive=False, state_store=self.store)
        if self.dedup._persistence is not None:
            # image-match (mocked Elasticsearch) finds nothing
            self.dedup._persistence.get_all.return_value = (0, [])
            self.dedup._persistence.find_similar.return_value = []
        self._apply_config()

    def _apply_config(self):
        """
        Sets the config of these tests, again after anything that creates a DeduplicatorConfig (reloads the yaml).
        """
        c = self.config
        c.SOURCE_DIRECTORIES.value = [str(self.source)]
        c.DRY_RUN.value = False
        c.DRY_RUN_USER_DECISIONS.value = None
        c.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = str(self.target)
        c.MAX_FILE_MODIFICATION_TIME_DELTA.value = None
        c.SEARCH_ACROSS_ROOT_DIRS.value = True
        # never run without dry run on anything but the temporary copy
        self.assertTrue(all(str(d).startswith(str(self.tmp)) for d in c.SOURCE_DIRECTORIES.value))

    def _files(self) -> set:
        return {p.relative_to(self.source) for p in self.source.rglob("*") if p.is_file()}

    def _moved(self) -> list:
        return [p for p in self.target.rglob("*") if p.is_file()]

    def _groups(self, matcher: str) -> list:
        return [self.store.get_group(g["id"]) for g in self.store.list_groups(matcher=matcher, limit=500)["items"]]


class BothBackendsTest(_HashBackendTestBase):
    """
    matching.backend "both": the hash pipeline only records its groups.
    """

    backend = MATCHING_BACKEND_BOTH

    def test_records_hash_groups_but_never_moves_files(self):
        files_before = self._files()
        self.dedup.deduplicate_all()

        self.assertEqual(files_before, self._files())
        self.assertEqual([], self._moved())
        groups = self._groups("hashes")
        self.assertEqual(3, len(groups))
        for group in groups:
            self.assertFalse(group["actionable"])
            self.assertEqual(4, group["candidate_count"])
            self.assertTrue(group["match_review_reasons"])
            self.assertIsNotNone(group["match_confidence"])
            self.assertEqual([], self.store.list_actions(group_id=group["id"])["items"])
            kinds = {Path(c["path"]).stem.rsplit("-", 1)[-1]: c["variant_kind"] for c in group["candidates"]}
            self.assertEqual("grayscale", kinds["grayscale"])
            self.assertEqual("colour-edited", kinds["edited"])
            self.assertEqual("copy", kinds["compression"])
            for candidate in group["candidates"]:
                self.assertIsNotNone(candidate["match_confidence"])
                self.assertIn(candidate["match_reason"], ("exact", "pixel-identical", "near-duplicate"))
                self.assertIn("criteria", candidate["metadata"])

        run = self.store.last_run("full")
        phase_names = [p["name"] for p in self.store.get_run(run["id"])["phases"]]
        self.assertEqual(8, len(phase_names))
        self.assertIn("Analyzing files (hashes)", phase_names)
        self.assertIn("Finding duplicates (hashes)", phase_names)
        self.assertEqual(3, run["summary"]["hashes_groups"])
        self.assertTrue(run["summary"]["hashes_record_only"])

    def test_user_decisions_on_record_only_groups_are_refused(self):
        self.dedup.deduplicate_all()
        group = self._groups("hashes")[0]
        duplicate = next(c for c in group["candidates"] if c["role"] != "keep")

        with self.assertRaises(GroupNotActionableError):
            self.store.set_user_keeper(group["id"], duplicate["path"])
        result = self.store.bulk_decision([group["id"]], "accept")
        self.assertEqual({"record_only": 1}, result["skipped"])

        client = TestClient(create_app(self.store, self.config, static_directory=None,
                                       thumbnail_cache_directory=self.tmp / "thumbnails"))
        response = client.post(f"/api/groups/{group['id']}/keep", json={"candidate_id": duplicate["id"]})
        self.assertEqual(409, response.status_code)
        self.assertIn("only records", response.json()["detail"])
        detail = client.get(f"/api/groups/{group['id']}").json()
        self.assertFalse(detail["actionable"])
        self.assertEqual([], detail["planned_actions"])

        # even a decision forced into the database is never executed
        self.store._execute("UPDATE groups SET decision_source = 'user', keep_path = ?, execute_requested = 1 "
                            "WHERE id = ?", (duplicate["path"], group["id"]))
        self.assertEqual([], self.store.pending_executions())
        self.dedup.execute_user_decisions()
        self.assertEqual([], self._moved())

        # the decision import ignores record-only groups
        others = [c["path"] for c in group["candidates"] if c["path"] != duplicate["path"]]
        imported = self.store.import_discarded_paths(others, apply=True)
        self.assertEqual(0, sum(imported["outcomes"].values()))

    def test_image_match_groups_are_still_acted_on(self):
        group_dir = self.source / "bottles"
        original = str(group_dir / "IMG_20190903_193151.jpg")
        telegram = str(group_dir / "IMG_20190903_193151-telegram-compression.jpg")
        reader = self.dedup._metadata_reader

        def candidate(path: str, dist: float) -> dict:
            return {"path": path, "dist": dist, "score": 64, "metadata": reader._create_metadata_dict(path)}

        self.dedup._persistence.find_similar.side_effect = lambda path: (
            [candidate(original, 0.0), candidate(telegram, 0.05)] if path in (original, telegram) else [])
        self.dedup.deduplicate_all()

        # image-match moved the telegram copy, the hash groups were only recorded
        self.assertEqual(["IMG_20190903_193151-telegram-compression.jpg"], [p.name for p in self._moved()])
        image_match_groups = self._groups("image-match")
        self.assertEqual(1, len(image_match_groups))
        self.assertEqual(GroupStatus.RESOLVED, image_match_groups[0]["status"])
        self.assertTrue(image_match_groups[0]["actionable"])
        for group in self._groups("hashes"):
            self.assertEqual([], self.store.list_actions(group_id=group["id"])["items"])

        comparison = self.store.compare_matchers("image-match", "hashes", statuses=GroupStatus.ALL)
        self.assertEqual({"same": 0, "different": 1, "only_image-match": 0, "only_hashes": 2},
                         comparison["counts"])

    def test_hash_failure_does_not_break_image_match(self):
        with mock.patch("py_image_dedup.library.hash_matcher.HashMatcher.match", side_effect=RuntimeError("boom")):
            self.dedup.deduplicate_all()
        run = self.store.last_run("full")
        self.assertEqual("finished", run["status"])
        self.assertIn("boom", run["summary"]["hashes_error"])
        self.assertEqual(8, len(self.store.get_run(run["id"])["phases"]))

    def test_incremental_runs_only_use_image_match(self):
        from py_image_dedup.library.processing_manager import ProcessingManager
        manager = ProcessingManager(self.dedup)
        self._apply_config()
        manager.add(self.source / "bottles")
        manager.latest_event_time = None
        with mock.patch.object(self.dedup, "deduplicate_all") as full_run:
            manager.process_queue()
        full_run.assert_not_called()
        self.assertEqual([], self._groups("hashes"))
        self.assertEqual("incremental", self.store.last_run()["kind"])


class HashesBackendTest(_HashBackendTestBase):
    """
    matching.backend "hashes": the hash pipeline decides, without Elasticsearch.
    """

    backend = MATCHING_BACKEND_HASHES

    def _add_plain_copies(self) -> tuple:
        """
        :return: (original, exact copy) of a new photo without variants
        """
        original = self.source / "generated" / "photo.jpg"
        _generated_image(original, seed=7)
        copy = self.source / "generated" / "copy" / "photo.jpg"
        copy.parent.mkdir(parents=True)
        shutil.copy2(original, copy)
        return original, copy

    def test_no_elasticsearch(self):
        self.assertIsNone(self.dedup._persistence)
        self.dedup.deduplicate_all()
        self.assertEqual(6, len(self.store.get_run(self.store.last_run("full")["id"])["phases"]))

    def test_variants_are_held_for_review_plain_copies_are_moved(self):
        original, copy = self._add_plain_copies()
        self.dedup.deduplicate_all()

        groups = self._groups("hashes")
        self.assertEqual(4, len(groups))
        variant_groups = [g for g in groups if g["candidate_count"] == 4]
        self.assertEqual(3, len(variant_groups))
        for group in variant_groups:
            self.assertTrue(group["actionable"])
            self.assertEqual(GroupStatus.REVIEW, group["status"])
            self.assertTrue(group["needs_review"])
            self.assertIn("grayscale variant", group["review_reason"])
            self.assertIn("colour-edited variant", group["review_reason"])
            self.assertEqual([], self.store.list_actions(group_id=group["id"])["items"])

        copies = next(g for g in groups if g["candidate_count"] == 2)
        self.assertEqual([], copies["match_review_reasons"])
        self.assertEqual(1.0, copies["match_confidence"])
        self.assertEqual(GroupStatus.RESOLVED, copies["status"])
        moved = self._moved()
        self.assertEqual(1, len(moved))
        self.assertEqual("photo.jpg", moved[0].name)
        self.assertEqual(1, sum(1 for p in (original, copy) if p.exists()))
        # every test image is still there
        for path in TEST_IMAGES.rglob("*.jpg"):
            self.assertTrue((self.source / path.relative_to(TEST_IMAGES)).exists())

    def test_user_decision_on_a_review_group_is_executed(self):
        self.dedup.deduplicate_all()
        group = self._groups("hashes")[0]
        keep = next(c for c in group["candidates"] if "telegram" not in c["path"] and c["role"] != "keep")
        self.store.set_user_keeper(group["id"], keep["path"])
        self.dedup.execute_user_decisions()
        self.assertEqual(3, len(self._moved()))
        self.assertTrue(Path(keep["path"]).exists())

    def test_without_across_dirs_only_files_of_the_same_source_directory_are_grouped(self):
        second = self.tmp / "second"
        second.mkdir()
        original = self.source / "generated" / "photo.jpg"
        _generated_image(original, seed=3)
        shutil.copy2(original, second / "photo.jpg")
        self.config.SOURCE_DIRECTORIES.value = [str(self.source), str(second)]
        self.config.SEARCH_ACROSS_ROOT_DIRS.value = False
        self.dedup.deduplicate_all()
        self.assertEqual(3, len(self._groups("hashes")))
        self.config.SEARCH_ACROSS_ROOT_DIRS.value = True
        self.dedup.deduplicate_all()
        copies = [g for g in self._groups("hashes") if g["candidate_count"] == 2]
        self.assertEqual(1, len(copies))
        self.assertEqual({str(original), str(second / "photo.jpg")},
                         {c["path"] for c in copies[0]["candidates"]})

    def test_incremental_changes_trigger_a_full_run(self):
        from py_image_dedup.library.processing_manager import ProcessingManager
        manager = ProcessingManager(self.dedup)
        self._apply_config()
        manager.add(self.source / "bottles")
        manager.add(self.source / "clouds")
        manager.latest_event_time = None
        manager.process_queue()
        run = self.store.last_run()
        self.assertEqual("full", run["kind"])
        self.assertEqual("2 changed paths", run["trigger"])
        self.assertEqual(0, len(manager.queue))
        self.assertEqual(3, len(self._groups("hashes")))

    def test_analysis_failures_are_recorded_and_forgotten(self):
        broken = self.source / "broken.jpg"
        broken.write_bytes(b"not an image")
        self.dedup.deduplicate_all()
        self.assertIn(str(broken), self.store.failure_paths(phase="analysis (hashes)"))
        broken.unlink()
        _generated_image(broken, seed=11)
        self.dedup.deduplicate_all()
        self.assertEqual([], self.store.failure_paths(phase="analysis (hashes)"))


class HashMatcherTest(_ConfigMixin, TestBase):

    def test_respects_extensions_exclusions_and_recursion(self):
        c = self.config
        self._save_config(c.EXCLUSIONS, c.RECURSIVE, c.FILE_EXTENSION_FILTER)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for name in ["a.jpg", "b.PNG", "c.txt", "sub/d.jpg", "skip/e.jpg"]:
            (tmp / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp / name).write_bytes(b"x")
        c.FILE_EXTENSION_FILTER.value = [".jpg", ".png"]
        c.EXCLUSIONS.value = ["/skip/"]
        c.RECURSIVE.value = True
        matcher = HashMatcher(c, store=SqliteSignatureStore(":memory:"))
        self.assertEqual(["a.jpg", "b.PNG", "sub/d.jpg"],
                         [str(p.relative_to(tmp)) for p in matcher.find_files([tmp])])
        c.RECURSIVE.value = False
        self.assertEqual(["a.jpg", "b.PNG"], [str(p.relative_to(tmp)) for p in matcher.find_files([tmp])])

    def test_member_match_info_orientation(self):
        from py_image_dedup.matching.models import DuplicateGroup, Match, MatchReason, VariantKind
        from py_image_dedup.matching.orientation import Orientation
        match = Match("a.jpg", "b.jpg", MatchReason.NEAR_DUPLICATE, 0.9, orientation=Orientation.ROTATE_90,
                      kind=VariantKind.ROTATED, notes=("rotated 90°",))
        group = DuplicateGroup(paths=["a.jpg", "b.jpg"], matches=[match], confidence=0.9,
                               reason=MatchReason.NEAR_DUPLICATE,
                               member_kinds={"a.jpg": VariantKind.ROTATED, "b.jpg": VariantKind.ROTATED})
        info = member_match_info(group)
        self.assertEqual("rotated 90°", info["b.jpg"]["orientation"])
        self.assertEqual("rotated 270°", info["a.jpg"]["orientation"])
        self.assertEqual("a.jpg", info["b.jpg"]["partner"])
        self.assertEqual("rotated", info["a.jpg"]["variant_kind"])
        self.assertEqual(0.9, info["a.jpg"]["match_confidence"])


class StateStoreMatcherTest(TestBase):

    def test_same_members_of_different_matchers_are_different_groups(self):
        store = StateStore(":memory:")
        self.addCleanup(store.close)
        candidates = [{"path": "/x/1.jpg"}, {"path": "/x/2.jpg"}]
        a = store.record_group(None, "image-match", candidates, ["/x/1.jpg"], None, None)
        b = store.record_group(None, "hashes", candidates, ["/x/1.jpg"], None, None, match_confidence=0.9,
                               match_review_reasons=["burst shots"])
        self.assertNotEqual(a["id"], b["id"])
        # image-match keys are unchanged, so existing groups (and user decisions) keep their identity
        self.assertEqual(group_key(["/x/1.jpg", "/x/2.jpg"]), a["key"])
        self.assertEqual(["burst shots"], b["match_review_reasons"])
        self.assertEqual(0.9, b["match_confidence"])
        self.assertEqual({"image-match": 1, "hashes": 1}, store.statistics()["groups_by_matcher"])
        self.assertEqual([b["id"]], store.list_group_ids(matcher="hashes"))

    def test_migration_of_a_version_2_database(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        path = tmp / "state.db"
        store = StateStore(path)
        group = store.record_group(None, "image-match", [{"path": "/x/1.jpg"}, {"path": "/x/2.jpg"}],
                                   ["/x/1.jpg"], "file-path", "reason")
        store.ignore_group(group["id"])
        store.close()
        connection = sqlite3.connect(path)
        for table, column in [("groups", "match_confidence"), ("groups", "match_review_reasons"),
                              ("candidates", "variant_kind"), ("candidates", "orientation"),
                              ("candidates", "match_confidence"), ("candidates", "match_reason")]:
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        connection.commit()
        connection.close()

        store = StateStore(path)
        self.addCleanup(store.close)
        columns = [row[1] for row in store._conn.execute("PRAGMA table_info(candidates)")]
        self.assertIn("variant_kind", columns)
        self.assertEqual("3", store._query_one("SELECT value FROM meta WHERE key = 'schema_version'")["value"])
        again = store.record_group(None, "image-match", [{"path": "/x/2.jpg"}, {"path": "/x/1.jpg"}],
                                   ["/x/1.jpg"], "file-path", "reason")
        self.assertEqual(group["id"], again["id"])
        self.assertEqual(GroupStatus.IGNORED, again["status"])
        self.assertEqual([], again["match_review_reasons"])


class CompareApiTest(TestBase):

    def setUp(self):
        super().setUp()
        self.store = StateStore(":memory:", actionable_matchers=["image-match"])
        self.addCleanup(self.store.close)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.client = TestClient(create_app(self.store, self.config, static_directory=None,
                                            thumbnail_cache_directory=tmp))

        def record(matcher, *paths):
            return self.store.record_group(None, matcher, [{"path": p} for p in paths], [paths[0]], None, None)["id"]

        self.same = (record("image-match", "/a/1", "/a/2"), record("hashes", "/a/1", "/a/2"))
        self.split = (record("image-match", "/b/1", "/b/2", "/b/3"), record("hashes", "/b/1", "/b/2"),
                      record("hashes", "/b/3", "/b/4"))
        self.only_image_match = record("image-match", "/c/1", "/c/2")
        self.only_hashes = record("hashes", "/d/1", "/d/2")

    def test_counts_and_categories(self):
        response = self.client.get("/api/compare")
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual({"same": 1, "different": 1, "only_image-match": 1, "only_hashes": 1}, data["counts"])
        self.assertEqual(4, data["total"])
        # differences first
        first = data["items"][0]
        self.assertEqual("different", first["category"])
        self.assertEqual([self.split[0]], [g["id"] for g in first["left"]])
        self.assertEqual(sorted(self.split[1:]), sorted(g["id"] for g in first["right"]))
        self.assertEqual([], first["only_left_paths"])
        self.assertEqual(["/b/4"], first["only_right_paths"])
        self.assertTrue(first["left"][0]["members"])
        self.assertFalse(first["right"][0]["actionable"])

        only = self.client.get("/api/compare", params={"category": "only_hashes"}).json()
        self.assertEqual(1, only["total"])
        self.assertEqual(self.only_hashes, only["items"][0]["right"][0]["id"])
        self.assertEqual(400, self.client.get("/api/compare", params={"category": "nope"}).status_code)
        self.assertEqual(400, self.client.get("/api/compare", params={"left": "hashes"}).status_code)

    def test_ignored_groups_are_not_compared_by_default(self):
        self.store.ignore_group(self.only_image_match)
        data = self.client.get("/api/compare").json()
        self.assertEqual(0, data["counts"]["only_image-match"])
        data = self.client.get("/api/compare", params=[("status", "open"), ("status", "ignored")]).json()
        self.assertEqual(1, data["counts"]["only_image-match"])

    def test_matcher_filter(self):
        data = self.client.get("/api/groups", params={"matcher": "hashes"}).json()
        self.assertEqual(4, data["total"])
        self.assertTrue(all(g["matcher"] == "hashes" and not g["actionable"] for g in data["items"]))
        status = self.client.get("/api/status").json()
        self.assertEqual("image-match", status["matching"]["backend"])
        self.assertEqual({"image-match": 3, "hashes": 4}, status["statistics"]["groups_by_matcher"])

    def test_bulk_accept_by_filter_skips_record_only_groups(self):
        c = self.config
        original = c.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value
        self.addCleanup(lambda: setattr(c.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY, "value", original))
        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target)
        c.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = target
        data = self.client.post("/api/groups/bulk", json={"action": "accept", "filter": {"matcher": "hashes"}}).json()
        self.assertEqual([], data["changed"])
        self.assertEqual({"record_only": 4}, data["skipped"])
