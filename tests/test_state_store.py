import tempfile
import unittest
from pathlib import Path

from py_image_dedup.state import StateStore, GroupStatus, DecisionSource, ActionStatus, BulkAction, group_key


def _candidates(*paths):
    return [{"path": p, "file_size": 100 - i, "pixel_count": 10, "distance": 0.0, "metadata": {"x": i}}
            for i, p in enumerate(paths)]


class StateStoreTest(unittest.TestCase):

    def setUp(self):
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)

    def _record(self, paths=("/a/1.jpg", "/a/2.jpg", "/a/3.jpg"), keep=None, needs_review=False, decided_by="x"):
        keep = keep or [paths[0]]
        return self.store.record_group(run_id=None, matcher="test", candidates=_candidates(*paths), keep_paths=keep,
                                       decided_by=decided_by, decision_reason="because",
                                       needs_review=needs_review, review_reason="weak" if needs_review else None)

    def test_group_key_is_order_independent(self):
        self.assertEqual(group_key(["/b", "/a"]), group_key(["/a", "/b", "/a"]))
        self.assertNotEqual(group_key(["/a"]), group_key(["/a", "/b"]))

    def test_run_lifecycle(self):
        run_id = self.store.start_run("full", dry_run=True, trigger="test")
        self.assertTrue(self.store.live.snapshot()["busy"])
        self.store.start_phase(run_id, "Counting files", 1, 2)
        self.store.update_phase_progress(run_id, 3, 4)
        self.store.start_phase(run_id, "Analyzing files", 2, 2)
        self.store.finish_run(run_id, summary={"moved": 1})

        run = self.store.get_run(run_id)
        self.assertEqual("finished", run["status"])
        self.assertTrue(run["dry_run"])
        self.assertEqual({"moved": 1}, run["summary"])
        self.assertEqual(["Counting files", "Analyzing files"], [p["name"] for p in run["phases"]])
        self.assertEqual(3, run["phases"][0]["done"])
        self.assertTrue(all(p["finished_at"] is not None for p in run["phases"]))
        self.assertFalse(self.store.live.snapshot()["busy"])
        self.assertEqual(run_id, self.store.last_run("full")["id"])
        self.assertIsNone(self.store.last_run("incremental"))

    def test_failed_run(self):
        run_id = self.store.start_run("incremental", dry_run=False)
        self.store.finish_run(run_id, error="boom")
        self.assertEqual("failed", self.store.get_run(run_id)["status"])

    def test_running_runs_are_marked_interrupted_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "sub", "state.db")
            store = StateStore(path)
            run_id = store.start_run("full", dry_run=True)
            store.close()

            store = StateStore(path)
            self.assertEqual("interrupted", store.get_run(run_id)["status"])
            store.close()

    def test_record_new_group(self):
        group = self._record()
        self.assertEqual(GroupStatus.OPEN, group["status"])
        self.assertEqual("/a/1.jpg", group["keep_path"])
        self.assertEqual(DecisionSource.RULES, group["decision_source"])
        self.assertEqual(3, group["candidate_count"])

        candidates = self.store.get_group(group["id"])["candidates"]
        self.assertEqual(["keep", "duplicate", "duplicate"], [c["role"] for c in candidates])
        self.assertEqual({"x": 1}, candidates[1]["metadata"])

    def test_record_weak_group_needs_review(self):
        group = self._record(needs_review=True)
        self.assertEqual(GroupStatus.REVIEW, group["status"])
        self.assertTrue(group["needs_review"])

    def test_rerecord_keeps_identity_and_candidate_ids(self):
        first = self._record()
        first_ids = [c["id"] for c in self.store.get_candidates(first["id"])]
        second = self._record(paths=("/a/3.jpg", "/a/2.jpg", "/a/1.jpg"), keep=["/a/1.jpg"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(sorted(first_ids), sorted(c["id"] for c in self.store.get_candidates(second["id"])))
        self.assertEqual(1, self.store.list_groups()["total"])

    def test_user_keeper_survives_rerecord(self):
        group = self._record()
        updated = self.store.set_user_keeper(group["id"], "/a/2.jpg")
        self.assertEqual(DecisionSource.USER, updated["decision_source"])
        self.assertTrue(updated["execute_requested"])
        self.assertEqual("keep", next(c for c in updated["candidates"] if c["path"] == "/a/2.jpg")["role"])

        again = self._record()
        self.assertEqual(DecisionSource.USER, again["decision_source"])
        self.assertEqual("/a/2.jpg", again["keep_path"])
        self.assertEqual("/a/1.jpg", again["rule_keep_path"])
        roles = {c["path"]: c["role"] for c in self.store.get_candidates(group["id"])}
        self.assertEqual("keep", roles["/a/2.jpg"])
        self.assertEqual("duplicate", roles["/a/1.jpg"])

    def test_user_keeper_must_be_member(self):
        group = self._record()
        with self.assertRaises(ValueError):
            self.store.set_user_keeper(group["id"], "/elsewhere.jpg")
        with self.assertRaises(KeyError):
            self.store.set_user_keeper(12345, "/a/1.jpg")

    def test_ignore_survives_rerecord_and_reset(self):
        group = self._record(needs_review=True)
        self.assertEqual(GroupStatus.IGNORED, self.store.ignore_group(group["id"])["status"])
        self.assertEqual(GroupStatus.IGNORED, self._record(needs_review=True)["status"])

        reset = self.store.reset_group(group["id"])
        self.assertEqual(GroupStatus.REVIEW, reset["status"])
        self.assertEqual(DecisionSource.RULES, reset["decision_source"])

    def test_reset_restores_rule_keeper(self):
        group = self._record()
        self.store.set_user_keeper(group["id"], "/a/3.jpg")
        reset = self.store.reset_group(group["id"])
        self.assertEqual("/a/1.jpg", reset["keep_path"])
        self.assertFalse(reset["execute_requested"])
        self.assertEqual("keep", reset["candidates"][0]["role"])

    def test_pending_executions(self):
        group = self._record()
        self.assertEqual([], self.store.pending_executions())
        self.store.set_user_keeper(group["id"], "/a/2.jpg")
        pending = self.store.pending_executions()
        self.assertEqual([group["id"]], [g["id"] for g in pending])
        self.assertEqual(3, len(pending[0]["candidates"]))

        # dry run: executed but not resolved
        self.store.mark_executed(group["id"], resolved=False)
        self.assertEqual([], self.store.pending_executions())
        self.assertEqual(GroupStatus.OPEN, self.store.get_group(group["id"])["status"])

        self.store.set_user_keeper(group["id"], "/a/2.jpg")
        self.store.mark_executed(group["id"], resolved=True)
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(group["id"])["status"])

    def test_resolved_group_reopens_when_found_again(self):
        group = self._record()
        self.store.mark_resolved([group["id"]])
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(group["id"])["status"])
        self.assertEqual(GroupStatus.OPEN, self._record()["status"])

    def test_resolve_unseen_groups(self):
        old_run = self.store.start_run("full", dry_run=True)
        gone = self.store.record_group(run_id=old_run, matcher="test", candidates=_candidates("/a/1.jpg", "/a/2.jpg"),
                                       keep_paths=["/a/1.jpg"], decided_by="x", decision_reason="because")
        ignored = self.store.record_group(run_id=old_run, matcher="test",
                                          candidates=_candidates("/b/1.jpg", "/b/2.jpg"),
                                          keep_paths=["/b/1.jpg"], decided_by="x", decision_reason="because")
        self.store.ignore_group(ignored["id"])

        new_run = self.store.start_run("full", dry_run=True)
        seen = self.store.record_group(run_id=new_run, matcher="test", candidates=_candidates("/c/1.jpg", "/c/2.jpg"),
                                       keep_paths=["/c/1.jpg"], decided_by="x", decision_reason="because")

        self.assertEqual(1, self.store.resolve_unseen_groups(new_run))
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(gone["id"])["status"])
        self.assertEqual(GroupStatus.IGNORED, self.store.get_group(ignored["id"])["status"])
        self.assertEqual(GroupStatus.OPEN, self.store.get_group(seen["id"])["status"])

    def test_import_discarded_paths(self):
        decided = self._record(paths=("/a/1.jpg", "/a/2.jpg", "/a/3.jpg"))
        several = self._record(paths=("/b/1.jpg", "/b/2.jpg", "/b/3.jpg"))
        all_discarded = self._record(paths=("/c/1.jpg", "/c/2.jpg"))
        untouched = self._record(paths=("/d/1.jpg", "/d/2.jpg"))
        by_user = self._record(paths=("/e/1.jpg", "/e/2.jpg"))
        self.store.set_user_keeper(by_user["id"], "/e/1.jpg")
        discarded = ["/a/1.jpg", "/a/3.jpg", "/b/1.jpg", "/c/1.jpg", "/c/2.jpg", "/e/1.jpg"]

        preview = self.store.import_discarded_paths(discarded)
        self.assertEqual({"decided": 1, "no_discarded_member": 1, "all_discarded": 1, "several_remaining": 1,
                          "already_decided_by_user": 1}, preview["outcomes"])
        self.assertEqual([{"group_id": decided["id"], "keep_path": "/a/2.jpg"}], preview["decided"])
        self.assertEqual(DecisionSource.RULES, self.store.get_group(decided["id"])["decision_source"])

        self.store.import_discarded_paths(discarded, apply=True)
        group = self.store.get_group(decided["id"])
        self.assertEqual((DecisionSource.USER, "/a/2.jpg"), (group["decision_source"], group["keep_path"]))
        self.assertTrue(group["execute_requested"])
        for other in (several, all_discarded, untouched):
            self.assertEqual(DecisionSource.RULES, self.store.get_group(other["id"])["decision_source"])
        self.assertEqual("/e/1.jpg", self.store.get_group(by_user["id"])["keep_path"])

    def test_list_groups_review_first_and_filters(self):
        open_group = self._record(paths=("/a/1.jpg", "/a/2.jpg"))
        review_group = self._record(paths=("/b/1.jpg", "/b/2.jpg"), needs_review=True, decided_by="longer-file-name")
        resolved_group = self._record(paths=("/c/1_x.jpg", "/c/2.jpg"))
        self.store.mark_resolved([resolved_group["id"]])

        items = self.store.list_groups()["items"]
        self.assertEqual([review_group["id"], open_group["id"], resolved_group["id"]], [g["id"] for g in items])
        self.assertEqual(["/b/1.jpg", "/b/2.jpg"], [m["path"] for m in items[0]["members"]])

        self.assertEqual(1, self.store.list_groups(status=GroupStatus.OPEN)["total"])
        self.assertEqual(1, self.store.list_groups(needs_review=True)["total"])
        self.assertEqual(1, self.store.list_groups(decided_by="longer-file-name")["total"])
        self.assertEqual([resolved_group["id"]], [g["id"] for g in self.store.list_groups(search="1_x")["items"]])
        # LIKE wildcards are escaped
        self.assertEqual(0, self.store.list_groups(search="1%x")["total"])

    def test_keeps_smaller_copy(self):
        # candidates have decreasing file sizes, so keeping the first one is fine
        fine = self._record(paths=("/a/1.jpg", "/a/2.jpg"))
        smaller = self._record(paths=("/b/1.jpg", "/b/2.jpg"), keep=["/b/2.jpg"])
        self.assertEqual(1, self.store.statistics()["keeps_smaller_copy"])
        self.assertEqual([smaller["id"]], [g["id"] for g in self.store.list_groups(keeps_smaller_copy=True)["items"]])

        # the user decided: still reported, it is about the kept copy
        self.store.set_user_keeper(fine["id"], "/a/2.jpg")
        self.assertEqual(2, self.store.statistics()["keeps_smaller_copy"])

    def test_actions(self):
        group = self._record()
        self.store.record_action("/a/2.jpg", "move", DecisionSource.RULES, dry_run=True,
                                 status=ActionStatus.SIMULATED, target="/dup/a/2.jpg", group_id=group["id"])
        self.store.record_action("/a/3.jpg", "move", DecisionSource.USER, dry_run=False,
                                 status=ActionStatus.DONE, target="/dup/a/3.jpg", group_id=group["id"])
        actions = self.store.list_actions(group_id=group["id"])
        self.assertEqual(2, actions["total"])
        self.assertEqual("/a/3.jpg", actions["items"][0]["path"])
        self.assertTrue(actions["items"][1]["dry_run"])
        self.assertEqual(1, self.store.list_actions(status=ActionStatus.SIMULATED)["total"])
        self.assertEqual(1, self.store.list_actions(search="3.jpg")["total"])

        # only real, successful moves count
        self.assertIsNone(self.store.last_move_target("/a/2.jpg"))
        self.assertEqual("/dup/a/3.jpg", self.store.last_move_target("/a/3.jpg"))

    def test_failures(self):
        self.store.record_failure("/a/broken.jpg", "OSError: truncated", phase="analysis")
        self.store.record_failure("/a/broken.jpg", "OSError: truncated", phase="analysis")
        failures = self.store.list_failures()
        self.assertEqual(1, failures["total"])
        self.assertEqual(2, failures["items"][0]["count"])

        self.store.clear_failure("/a/never-failed.jpg")
        self.store.clear_failure("/a/broken.jpg")
        self.assertEqual(0, self.store.list_failures()["total"])

    def test_statistics(self):
        group = self._record(needs_review=True, decided_by="longer-file-name")
        self._record(paths=("/b/1.jpg", "/b/2.jpg"), decided_by="bigger-file-size")
        self.store.record_action("/a/2.jpg", "move", DecisionSource.RULES, dry_run=True,
                                 status=ActionStatus.SIMULATED, group_id=group["id"])
        self.store.record_failure("/x.jpg", "err")

        stats = self.store.statistics()
        self.assertEqual(2, stats["groups_total"])
        self.assertEqual(1, stats["groups"][GroupStatus.REVIEW])
        self.assertEqual(1, stats["needs_review"])
        self.assertEqual(3, stats["duplicate_files_open"])
        self.assertEqual({"simulated": 1}, stats["actions"]["move"])
        self.assertEqual(1, stats["failures"])
        self.assertEqual({"longer-file-name": 1, "bigger-file-size": 1}, stats["decided_by"])

    def test_bulk_accept(self):
        rules = self._record()
        user = self._record(paths=("/b/1.jpg", "/b/2.jpg"))
        self.store.set_user_keeper(user["id"], "/b/2.jpg")
        self.store.mark_executed(user["id"], resolved=False)
        ignored = self._record(paths=("/c/1.jpg", "/c/2.jpg"))
        self.store.ignore_group(ignored["id"])
        resolved = self._record(paths=("/d/1.jpg", "/d/2.jpg"))
        self.store.mark_resolved([resolved["id"]])

        result = self.store.bulk_decision([rules["id"], user["id"], ignored["id"], resolved["id"], 999, rules["id"]],
                                          BulkAction.ACCEPT)

        self.assertEqual([rules["id"]], result["changed"])
        self.assertEqual({"decided_by_user": 1, "ignored": 1, "resolved": 1, "unknown": 1}, result["skipped"])
        group = self.store.get_group(rules["id"])
        self.assertEqual((DecisionSource.USER, "/a/1.jpg", True),
                         (group["decision_source"], group["keep_path"], group["execute_requested"]))
        # the user's own choice is not replaced by the rule decision
        self.assertEqual("/b/2.jpg", self.store.get_group(user["id"])["keep_path"])
        self.assertFalse(self.store.get_group(user["id"])["execute_requested"])
        self.assertEqual(GroupStatus.IGNORED, self.store.get_group(ignored["id"])["status"])
        self.assertEqual(GroupStatus.RESOLVED, self.store.get_group(resolved["id"])["status"])

    def test_bulk_ignore_and_reset(self):
        review = self._record(needs_review=True)
        open_group = self._record(paths=("/b/1.jpg", "/b/2.jpg"))
        resolved = self._record(paths=("/d/1.jpg", "/d/2.jpg"))
        self.store.mark_resolved([resolved["id"]])
        ids = [review["id"], open_group["id"], resolved["id"]]

        result = self.store.bulk_decision(ids, BulkAction.IGNORE)
        self.assertEqual([review["id"], open_group["id"]], result["changed"])
        self.assertEqual({"resolved": 1}, result["skipped"])
        self.assertEqual(GroupStatus.IGNORED, self.store.get_group(review["id"])["status"])

        self.assertEqual({"already_ignored": 2, "resolved": 1},
                         self.store.bulk_decision(ids, BulkAction.IGNORE)["skipped"])

        result = self.store.bulk_decision(ids, BulkAction.RESET)
        self.assertEqual([review["id"], open_group["id"]], result["changed"])
        self.assertEqual(GroupStatus.REVIEW, self.store.get_group(review["id"])["status"])
        self.assertEqual(GroupStatus.OPEN, self.store.get_group(open_group["id"])["status"])
        self.assertEqual({"nothing_to_reset": 2, "resolved": 1},
                         self.store.bulk_decision(ids, BulkAction.RESET)["skipped"])

        with self.assertRaises(ValueError):
            self.store.bulk_decision(ids, "delete")

    def test_list_group_ids(self):
        review = self._record(needs_review=True)
        self._record(paths=("/b/1.jpg", "/b/2.jpg"))
        self.assertEqual([review["id"]], self.store.list_group_ids(status=GroupStatus.REVIEW))
        self.assertEqual(2, len(self.store.list_group_ids()))
        self.assertEqual(1, len(self.store.list_group_ids(search="/b/")))

    def test_undo_move(self):
        group = self._record()
        simulated = self.store.record_action("/a/2.jpg", "move", DecisionSource.RULES, dry_run=True,
                                             status=ActionStatus.SIMULATED, target="/t/a/2.jpg",
                                             group_id=group["id"])
        first = self.store.record_action("/a/3.jpg", "move", DecisionSource.RULES, dry_run=False,
                                         status=ActionStatus.DONE, target="/t/a/3.jpg", group_id=group["id"])
        move = self.store.record_action("/a/2.jpg", "move", DecisionSource.RULES, dry_run=False,
                                        status=ActionStatus.DONE, target="/t/a/2.jpg", group_id=group["id"])

        actions = {a["id"]: a for a in self.store.list_actions()["items"]}
        self.assertFalse(actions[simulated]["undoable"])
        self.assertTrue(actions[move]["undoable"])
        self.assertEqual({first, move}, {a["id"] for a in self.store.undoable_moves_of_group(group["id"])})
        with self.assertRaises(ValueError):
            self.store.request_restore(simulated)
        with self.assertRaises(KeyError):
            self.store.request_restore(999)

        self.assertTrue(self.store.request_restore(move)["restore_requested"])
        self.store.request_restore(move)  # requesting twice is harmless
        self.assertEqual([move], [a["id"] for a in self.store.pending_restores()])
        self.assertEqual(1, self.store.statistics()["restore_requested"])
        self.store.finish_restore(move)
        self.assertEqual([], self.store.pending_restores())

        # once the file is restored, the move can not be undone again and the file is not "moved" anymore
        self.assertEqual("/t/a/2.jpg", self.store.last_move_target("/a/2.jpg"))
        self.store.record_action("/a/2.jpg", "restore", DecisionSource.USER, dry_run=False,
                                 status=ActionStatus.DONE, target="/t/a/2.jpg", group_id=group["id"])
        self.assertIsNone(self.store.last_move_target("/a/2.jpg"))
        self.assertFalse(self.store.get_action(move)["undoable"])
        self.assertEqual([first], [a["id"] for a in self.store.undoable_moves_of_group(group["id"])])

    def test_mark_restored_ignores_group(self):
        group = self._record()
        self.store.set_user_keeper(group["id"], "/a/2.jpg")
        self.store.mark_resolved([group["id"]])
        self.store.mark_restored(group["id"])
        group = self.store.get_group(group["id"])
        self.assertEqual((GroupStatus.IGNORED, False, None),
                         (group["status"], group["execute_requested"], group["resolved_at"]))
        self.assertEqual([group["id"]], self.store.group_ids_of_path("/a/2.jpg"))


if __name__ == '__main__':
    unittest.main()
