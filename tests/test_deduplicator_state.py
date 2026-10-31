import shutil
import tempfile
from pathlib import Path
from unittest import mock

from py_image_dedup.integrations.immich import ImmichError
from py_image_dedup.library import ActionEnum
from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
from py_image_dedup.persistence.metadata_key import MetadataKey
from py_image_dedup.state import StateStore, GroupStatus, ActionStatus, DecisionSource
from tests import TestBase

ROOT = Path("images")


def _candidate(path: str, filesize: int = 100, modification_date: int = 1, dist: float = 0.05,
               pixel_count: int = 10000, score: int = 64) -> dict:
    return {
        MetadataKey.PATH.value: path,
        MetadataKey.DISTANCE.value: dist,
        MetadataKey.METADATA.value: {
            MetadataKey.FILE_SIZE.value: filesize,
            MetadataKey.FILE_MODIFICATION_DATE.value: modification_date,
            MetadataKey.PIXELCOUNT.value: pixel_count,
            MetadataKey.EXIF_DATA.value: {"Make": "Test", "MakerNote": "x" * 1000},
        },
        MetadataKey.SCORE.value: score,
    }


class DeduplicatorStateTest(TestBase):
    """
    Recording of runs, groups, decisions and actions in the StateStore, and execution of user decisions.
    """

    def setUp(self):
        super().setUp()
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.dedup = ImageMatchDeduplicator(interactive=False, state_store=self.store)
        self._original_values = {
            entry: entry.value for entry in [
                self.config.DRY_RUN,
                self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY,
                self.config.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS,
            ]
        }
        self.addCleanup(self._restore_config)

    def _restore_config(self):
        for entry, value in self._original_values.items():
            entry.value = value

    def _find(self, candidates):
        self.dedup._persistence.find_similar.return_value = candidates
        self.dedup._progress_manager.start("test", 1, "Files", False)
        self.addCleanup(self.dedup._progress_manager.clear)
        # the candidate files don't exist on disk, see MissingCandidatesTest for that filter
        with mock.patch.object(self.dedup, "_without_missing_files", side_effect=lambda c, _: c):
            self.dedup.find_duplicates_of_file(root_directories=[ROOT], root_directory=ROOT,
                                               reference_file_path=Path(candidates[0][MetadataKey.PATH.value]))
        groups = self.store.list_groups()["items"]
        return self.store.get_group(groups[0]["id"]) if groups else None

    def test_records_group_with_deciding_rule(self):
        group = self._find([
            _candidate("images/a/small.jpg", filesize=10),
            _candidate("images/a/big.jpg", filesize=1000),
        ])

        self.assertEqual("images/a/big.jpg", group["keep_path"])
        self.assertEqual("bigger-file-size", group["decided_by"])
        self.assertIn("1000 bytes", group["decision_reason"])
        self.assertFalse(group["needs_review"])
        self.assertEqual(GroupStatus.OPEN, group["status"])
        self.assertEqual("image-match", group["matcher"])
        keeper, duplicate = group["candidates"]
        self.assertEqual(("keep", "duplicate"), (keeper["role"], duplicate["role"]))
        self.assertEqual(1000, keeper["file_size"])
        self.assertEqual(2, keeper["exif_tag_count"])
        # long EXIF values are not stored
        self.assertEqual({"Make": "Test"}, keeper["metadata"]["exif"])
        self.assertIn("bigger-file-size", [c["rule"] for c in keeper["metadata"]["criteria"]])

        # unchanged default behaviour: the duplicate is still processed
        self.assertEqual([Path("images/a/small.jpg")],
                         self.dedup._deduplication_result.get_file_with_action(ActionEnum.DELETE))

    def test_weak_rule_needs_review(self):
        group = self._find([
            _candidate("images/a/1.jpg"),
            _candidate("images/a/2.jpg"),
        ])
        self.assertTrue(group["needs_review"])
        self.assertEqual(GroupStatus.REVIEW, group["status"])
        self.assertEqual("file-path", group["decided_by"])
        # hold_ambiguous_groups is disabled by default: the decision is still executed
        self.assertEqual(1, len(self.dedup._deduplication_result.get_removed_or_moved_files()))

    def test_duplicate_backend_entries(self):
        # the backend may return several entries for the same file
        group = self._find([
            _candidate("images/a/small.jpg", filesize=10),
            _candidate("images/a/big.jpg", filesize=1000),
            _candidate("images/a/big.jpg", filesize=1000),
        ])
        self.assertEqual(2, len(group["candidates"]))
        self.assertEqual("bigger-file-size", group["decided_by"])
        self.assertFalse(group["needs_review"])

    def test_hold_ambiguous_groups(self):
        self.config.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS.value = True
        group = self._find([
            _candidate("images/a/1.jpg"),
            _candidate("images/a/2.jpg"),
        ])
        self.assertEqual(GroupStatus.REVIEW, group["status"])
        self.assertEqual([], self.dedup._deduplication_result.get_removed_or_moved_files())

    def test_ignored_group_is_not_touched(self):
        candidates = [_candidate("images/a/small.jpg", filesize=10), _candidate("images/a/big.jpg", filesize=1000)]
        group = self._find(candidates)
        self.store.ignore_group(group["id"])

        self.dedup.reset_result()
        group = self._find(candidates)
        self.assertEqual(GroupStatus.IGNORED, group["status"])
        self.assertEqual([], self.dedup._deduplication_result.get_removed_or_moved_files())

    def test_user_keeper_is_applied_in_later_runs(self):
        candidates = [_candidate("images/a/small.jpg", filesize=10), _candidate("images/a/big.jpg", filesize=1000)]
        group = self._find(candidates)
        self.store.set_user_keeper(group["id"], "images/a/small.jpg")

        self.dedup.reset_result()
        group = self._find(candidates)
        self.assertEqual(DecisionSource.USER, group["decision_source"])
        self.assertEqual([Path("images/a/big.jpg")],
                         self.dedup._deduplication_result.get_file_with_action(ActionEnum.DELETE))
        self.assertEqual([Path("images/a/small.jpg")],
                         self.dedup._deduplication_result.get_file_with_action(ActionEnum.NONE))

    def test_group_with_disagreeing_suggestion_is_held(self):
        candidates = [_candidate("images/a/small.jpg", filesize=10), _candidate("images/a/big.jpg", filesize=1000)]
        self._find(candidates)
        # f.ex. Immich kept the smaller copy
        self.store.import_discarded_paths(["images/a/big.jpg"], apply=True, source="immich")

        self.dedup.reset_result()
        group = self._find(candidates)
        self.assertEqual(GroupStatus.REVIEW, group["status"])
        self.assertTrue(group["needs_review"])
        self.assertEqual("images/a/small.jpg", group["suggested_keep_path"])
        self.assertEqual("Immich kept images/a/small.jpg, the rules keep images/a/big.jpg", group["review_reason"])
        self.assertEqual([], self.dedup._deduplication_result.get_removed_or_moved_files())

    def test_dry_run_actions_are_simulated(self):
        self.config.DRY_RUN.value = True
        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target)
        self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = Path(target)
        self._find([_candidate("images/a/small.jpg", filesize=10), _candidate("images/a/big.jpg", filesize=1000)])
        self.dedup.process_duplicates()

        actions = self.store.list_actions()["items"]
        self.assertEqual(1, len(actions))
        self.assertEqual(("images/a/small.jpg", "move", ActionStatus.SIMULATED, True),
                         (actions[0]["path"], actions[0]["action"], actions[0]["status"], actions[0]["dry_run"]))
        self.assertEqual(GroupStatus.OPEN, self.store.get_group(actions[0]["group_id"])["status"])

    def test_run_and_phases_are_recorded(self):
        self.dedup._persistence.get_all.return_value = (0, [])
        self.dedup.deduplicate_all(skip_analyze_phase=True)

        run = self.store.get_run(self.store.last_run()["id"])
        self.assertEqual("full", run["kind"])
        self.assertEqual("finished", run["status"])
        self.assertEqual(6, len(run["phases"]))
        self.assertIsNotNone(run["summary"])
        self.assertFalse(self.store.live.snapshot()["busy"])

    def test_full_run_resolves_groups_that_are_gone(self):
        self.store.record_group(run_id=None, matcher="image-match", candidates=[{"path": "/gone/1.jpg"}, {"path": "/gone/2.jpg"}],
                                keep_paths=["/gone/1.jpg"], decided_by="x", decision_reason="because")
        self.dedup._persistence.get_all.return_value = (0, [])
        self.dedup.deduplicate_all(skip_analyze_phase=True)
        self.assertEqual(1, self.store.statistics()["groups"]["resolved"])

    def test_full_run_forgets_failures_of_missing_files(self):
        existing = next(ROOT.rglob("*.jpg"))
        self.store.record_failure("/gone/broken.jpg", "OSError: truncated", phase="analysis")
        self.store.record_failure(str(existing), "OSError: truncated", phase="analysis")
        self.dedup._persistence.get_all.return_value = (0, [])
        self.dedup.deduplicate_all(skip_analyze_phase=True)
        self.assertEqual([str(existing)], [f["path"] for f in self.store.list_failures()["items"]])

    def test_failed_run_is_recorded(self):
        self.dedup._persistence.get_all.side_effect = RuntimeError("es down")
        with self.assertRaises(RuntimeError):
            self.dedup.deduplicate_all()
        run = self.store.last_run()
        self.assertEqual("failed", run["status"])
        self.assertIn("es down", run["error"])

    def test_analysis_failures_are_recorded(self):
        self.dedup._progress_manager.start("test", 2, "Files", False)
        self.dedup._persistence.add.side_effect = OSError("truncated")
        self.dedup.analyze_file(Path("images/broken.jpg"))
        self.assertEqual(1, self.store.list_failures()["total"])

        self.dedup._persistence.add.side_effect = None
        self.dedup.analyze_file(Path("images/broken.jpg"))
        self.assertEqual(0, self.store.list_failures()["total"])


class ExecuteUserDecisionTest(TestBase):
    """
    Executing decisions made in the web UI (works on copies of the test images in a temporary directory).
    """

    def setUp(self):
        super().setUp()
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.dedup = ImageMatchDeduplicator(interactive=False, state_store=self.store)

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.source = self.tmp / "source"
        self.target = self.tmp / "duplicates"
        self.source.mkdir()
        self.target.mkdir()
        images = sorted(Path("images/bottles").glob("*.jpg"))[:3]
        self.files = []
        for image in images:
            copy = self.source / image.name
            shutil.copy(image, copy)
            self.files.append(copy)

        original = (self.config.DRY_RUN.value, self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value)

        def restore():
            self.config.DRY_RUN.value, self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = original

        self.addCleanup(restore)
        self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = self.target

        group = self.store.record_group(
            run_id=None, matcher="image-match", candidates=[{"path": str(f)} for f in self.files],
            keep_paths=[str(self.files[0])], decided_by="file-path", decision_reason="test", needs_review=True)
        self.group_id = group["id"]

    def _target_of(self, path: Path) -> Path:
        return Path(str(self.target), *path.parts[1:])

    def test_dry_run_does_not_touch_files(self):
        self.config.DRY_RUN.value = True
        self.store.set_user_keeper(self.group_id, str(self.files[1]))

        self.assertEqual(1, self.dedup.execute_user_decisions())

        self.assertTrue(all(f.exists() for f in self.files))
        self.assertEqual([], list(self.target.iterdir()))
        actions = self.store.list_actions()["items"]
        self.assertEqual({ActionStatus.SIMULATED}, {a["status"] for a in actions})
        self.assertEqual({str(self.files[0]), str(self.files[2])}, {a["path"] for a in actions})
        self.assertEqual({DecisionSource.USER}, {a["source"] for a in actions})
        group = self.store.get_group(self.group_id)
        self.assertFalse(group["execute_requested"])
        self.assertEqual(GroupStatus.OPEN, group["status"])
        # nothing left to do
        self.assertEqual(0, self.dedup.execute_user_decisions())

    def test_moves_duplicates(self):
        self.config.DRY_RUN.value = False
        self.store.set_user_keeper(self.group_id, str(self.files[1]))

        self.dedup.execute_user_decisions()

        self.assertTrue(self.files[1].exists())
        self.assertFalse(self.files[0].exists())
        self.assertFalse(self.files[2].exists())
        self.assertTrue(self._target_of(self.files[0]).exists())
        self.assertTrue(self._target_of(self.files[2]).exists())
        self.assertEqual({ActionStatus.DONE}, {a["status"] for a in self.store.list_actions()["items"]})
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(self.group_id)["status"])

    def test_user_decisions_can_override_dry_run(self):
        self.config.DRY_RUN.value = True
        original = self.config.DRY_RUN_USER_DECISIONS.value
        self.addCleanup(setattr, self.config.DRY_RUN_USER_DECISIONS, "value", original)
        self.config.DRY_RUN_USER_DECISIONS.value = False
        self.store.set_user_keeper(self.group_id, str(self.files[1]))

        self.dedup.execute_user_decisions()

        self.assertFalse(self.files[0].exists())
        self.assertTrue(self._target_of(self.files[0]).exists())
        self.assertEqual({ActionStatus.DONE}, {a["status"] for a in self.store.list_actions()["items"]})
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(self.group_id)["status"])

    def test_missing_keeper_aborts(self):
        self.config.DRY_RUN.value = False
        self.store.set_user_keeper(self.group_id, str(self.files[1]))
        self.files[1].unlink()

        self.dedup.execute_user_decisions()

        self.assertTrue(self.files[0].exists())
        self.assertTrue(self.files[2].exists())
        self.assertEqual({ActionStatus.SKIPPED}, {a["status"] for a in self.store.list_actions()["items"]})
        self.assertEqual(GroupStatus.OPEN, self.store.get_group(self.group_id)["status"])

    def test_never_deletes_without_target_directory(self):
        self.config.DRY_RUN.value = False
        self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = None
        self.store.set_user_keeper(self.group_id, str(self.files[1]))

        self.dedup.execute_user_decisions()

        self.assertTrue(all(f.exists() for f in self.files))
        self.assertEqual({ActionStatus.SKIPPED}, {a["status"] for a in self.store.list_actions()["items"]})

    def _enable_immich_rescan(self):
        entries = [self.config.IMMICH_URL, self.config.IMMICH_API_KEY, self.config.IMMICH_PATH_MAPPINGS,
                   self.config.IMMICH_RESCAN_AFTER_CHANGES]
        for entry, value in [(e, e.value) for e in entries]:
            self.addCleanup(setattr, entry, "value", value)
        self.config.IMMICH_URL.value = "http://immich:2283"
        self.config.IMMICH_API_KEY.value = "key"
        self.config.IMMICH_PATH_MAPPINGS.value = [{"immich": "/immich", "local": str(self.source)}]
        self.config.IMMICH_RESCAN_AFTER_CHANGES.value = True
        # run the rescan synchronously
        self.dedup._run_in_background = lambda function: function()
        rescan = mock.patch("py_image_dedup.integrations.immich.ImmichClient.rescan_libraries",
                            return_value=["lib"]).start()
        self.addCleanup(mock.patch.stopall)
        return rescan

    def test_immich_rescan_once_after_moving_files(self):
        rescan = self._enable_immich_rescan()
        self.config.DRY_RUN.value = False
        self.store.set_user_keeper(self.group_id, str(self.files[1]))

        self.dedup.execute_user_decisions()

        rescan.assert_called_once()
        self.assertEqual({f"/immich/{self.files[0].name}", f"/immich/{self.files[2].name}"},
                         set(rescan.call_args.args[0]))

        # nothing changed since
        self.dedup.execute_user_decisions()
        rescan.assert_called_once()

    def test_no_immich_rescan_in_dry_run_or_when_disabled(self):
        rescan = self._enable_immich_rescan()
        self.config.DRY_RUN.value = True
        self.store.set_user_keeper(self.group_id, str(self.files[1]))
        self.dedup.execute_user_decisions()
        rescan.assert_not_called()

        self.config.IMMICH_RESCAN_AFTER_CHANGES.value = False
        self.config.DRY_RUN.value = False
        self.store.set_user_keeper(self.group_id, str(self.files[1]))
        self.dedup.execute_user_decisions()
        self.assertFalse(self.files[0].exists())
        rescan.assert_not_called()

    def test_unreachable_immich_does_not_break_decisions(self):
        rescan = self._enable_immich_rescan()
        rescan.side_effect = ImmichError("down")
        self.config.DRY_RUN.value = False
        self.store.set_user_keeper(self.group_id, str(self.files[1]))

        with self.assertLogs("py_image_dedup.library.deduplicator", level="WARNING") as logs:
            self.dedup.execute_user_decisions()

        rescan.assert_called_once()
        self.assertIn("down", "".join(logs.output))
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(self.group_id)["status"])

    def _move_and_request_restore(self) -> int:
        self.config.DRY_RUN.value = False
        self.store.set_user_keeper(self.group_id, str(self.files[1]))
        self.dedup.execute_user_decisions()
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(self.group_id)["status"])
        move = next(a for a in self.store.list_actions()["items"] if a["path"] == str(self.files[0]))
        self.store.request_restore(move["id"])
        return move["id"]

    def test_restore_moved_file(self):
        move_id = self._move_and_request_restore()

        self.assertEqual(1, self.dedup.execute_restore_requests())

        self.assertTrue(self.files[0].exists())
        self.assertFalse(self._target_of(self.files[0]).exists())
        # the other moved file is untouched
        self.assertFalse(self.files[2].exists())
        restore = self.store.list_actions()["items"][0]
        self.assertEqual(("restore", ActionStatus.DONE, str(self.files[0]), str(self._target_of(self.files[0]))),
                         (restore["action"], restore["status"], restore["path"], restore["target"]))
        self.assertEqual(self.group_id, restore["group_id"])
        self.assertEqual(DecisionSource.USER, restore["source"])
        group = self.store.get_group(self.group_id)
        # ignored, so the daemon does not move the file again
        self.assertEqual(GroupStatus.IGNORED, group["status"])
        self.assertFalse(group["execute_requested"])
        self.assertFalse(self.store.get_action(move_id)["undoable"])
        self.assertEqual(0, self.dedup.execute_restore_requests())

    def test_restore_never_overwrites(self):
        self._move_and_request_restore()
        self.files[0].write_bytes(b"new file at the original location")

        self.dedup.execute_restore_requests()

        self.assertEqual(b"new file at the original location", self.files[0].read_bytes())
        self.assertTrue(self._target_of(self.files[0]).exists())
        restore = self.store.list_actions()["items"][0]
        self.assertEqual(("restore", ActionStatus.SKIPPED), (restore["action"], restore["status"]))
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(self.group_id)["status"])
        self.assertEqual([], self.store.pending_restores())

    def test_restore_of_missing_moved_file_is_skipped(self):
        self._move_and_request_restore()
        self._target_of(self.files[0]).unlink()

        self.dedup.execute_restore_requests()

        self.assertFalse(self.files[0].exists())
        self.assertEqual(ActionStatus.SKIPPED, self.store.list_actions()["items"][0]["status"])

    def test_restore_respects_dry_run(self):
        move_id = self._move_and_request_restore()
        self.config.DRY_RUN.value = True

        self.dedup.execute_restore_requests()

        self.assertFalse(self.files[0].exists())
        self.assertTrue(self._target_of(self.files[0]).exists())
        self.assertEqual(ActionStatus.SIMULATED, self.store.list_actions()["items"][0]["status"])
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(self.group_id)["status"])
        # a simulated restore does not count, the move can still be undone
        self.assertTrue(self.store.get_action(move_id)["undoable"])

    def test_restore_copies_if_hard_links_are_not_possible(self):
        self._move_and_request_restore()
        content = self._target_of(self.files[0]).read_bytes()

        with mock.patch("os.link", side_effect=OSError(18, "Invalid cross-device link")):
            self.dedup.execute_restore_requests()

        self.assertEqual(content, self.files[0].read_bytes())
        self.assertFalse(self._target_of(self.files[0]).exists())

    def test_immich_rescan_after_restore(self):
        self._move_and_request_restore()
        rescan = self._enable_immich_rescan()

        self.dedup.execute_restore_requests()

        rescan.assert_called_once()
        self.assertEqual([f"/immich/{self.files[0].name}"], list(rescan.call_args.args[0]))
