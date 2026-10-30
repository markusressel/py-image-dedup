import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from py_image_dedup.state import StateStore, ActionStatus, DecisionSource
from py_image_dedup.web.app import create_app
from tests import TestBase

IMAGE = Path("images/bottles/IMG_20190903_193151.jpg")
IMAGE_2 = Path("images/bottles/IMG_20190903_193151-edited.jpg")


class WebApiTest(TestBase):

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

        original = (self.config.DRY_RUN.value, self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value)

        def restore():
            self.config.DRY_RUN.value, self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = original

        self.addCleanup(restore)
        self.config.DRY_RUN.value = True
        self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = None

        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)

        self.static = self.tmp / "dist"
        (self.static / "assets").mkdir(parents=True)
        (self.static / "index.html").write_text("<html>spa</html>")
        (self.static / "assets" / "app.js").write_text("console.log(1)")
        (self.tmp / "secret.txt").write_text("secret")

        app = create_app(self.store, self.config, static_directory=self.static,
                         thumbnail_cache_directory=self.tmp / "thumbnails")
        self.client = TestClient(app)

        group = self.store.record_group(
            run_id=None, matcher="test",
            candidates=[{"path": str(IMAGE), "file_size": 2}, {"path": str(IMAGE_2), "file_size": 1},
                        {"path": "/etc/hostname"}],
            keep_paths=[str(IMAGE)], decided_by="longer-file-name", decision_reason="test",
            needs_review=True, review_reason="weak")
        self.group_id = group["id"]
        self.candidates = self.store.get_candidates(self.group_id)

    def test_health(self):
        self.assertEqual({"status": "ok"}, self.client.get("/api/health").json())

    def test_status(self):
        run_id = self.store.start_run("full", dry_run=True)
        self.store.start_phase(run_id, "Analyzing files", 3, 6)
        self.store.live.task_started("Analyzing files in 'images'", 12)
        self.store.live.task_progress(5)

        status = self.client.get("/api/status").json()
        self.assertTrue(status["dry_run"])
        self.assertTrue(status["live"]["busy"])
        self.assertEqual("Analyzing files", status["live"]["phase"])
        self.assertEqual(5, status["live"]["done"])
        self.assertEqual(12, status["live"]["total"])
        self.assertEqual(1, status["statistics"]["needs_review"])
        self.assertEqual(run_id, status["last_run"]["id"])

    def test_runs(self):
        run_id = self.store.start_run("full", dry_run=True)
        self.store.start_phase(run_id, "Counting files", 2, 6)
        self.store.finish_run(run_id)
        self.assertEqual([run_id], [r["id"] for r in self.client.get("/api/runs").json()["items"]])
        run = self.client.get(f"/api/runs/{run_id}").json()
        self.assertEqual("Counting files", run["phases"][0]["name"])
        self.assertEqual(404, self.client.get("/api/runs/999").status_code)

    def test_groups(self):
        groups = self.client.get("/api/groups", params={"needs_review": "true"}).json()
        self.assertEqual(1, groups["total"])
        self.assertEqual(3, len(groups["items"][0]["members"]))
        self.assertEqual(0, self.client.get("/api/groups", params={"status": "resolved"}).json()["total"])
        self.assertEqual(400, self.client.get("/api/groups", params={"status": "bogus"}).status_code)

        group = self.client.get(f"/api/groups/{self.group_id}").json()
        self.assertTrue(group["dry_run"])
        self.assertEqual("longer-file-name", group["decided_by"])
        self.assertEqual([True, True], [c["exists"] for c in group["candidates"][:2]])
        # no target directory configured: duplicates would be deleted
        self.assertEqual({"delete"}, {a["action"] for a in group["planned_actions"]})
        self.assertEqual(404, self.client.get("/api/groups/999").status_code)

    def test_keep_requires_target_directory(self):
        response = self.client.post(f"/api/groups/{self.group_id}/keep",
                                    json={"candidate_id": self.candidates[1]["id"]})
        self.assertEqual(409, response.status_code)
        self.assertFalse(self.store.get_group(self.group_id)["execute_requested"])

    def test_import_discarded(self):
        body = {"discarded_paths": [str(IMAGE), "/etc/hostname"], "apply": True}
        self.assertEqual(409, self.client.post("/api/decisions/import-discarded", json=body).status_code)

        self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = self.tmp
        preview = self.client.post("/api/decisions/import-discarded", json={**body, "apply": False}).json()
        self.assertEqual([{"group_id": self.group_id, "keep_path": str(IMAGE_2)}], preview["decided"])
        self.assertEqual(str(IMAGE), self.store.get_group(self.group_id)["keep_path"])

        applied = self.client.post("/api/decisions/import-discarded", json=body).json()
        self.assertTrue(applied["applied"])
        self.assertEqual(str(IMAGE_2), self.store.get_group(self.group_id)["keep_path"])

    def test_keep_ignore_reset(self):
        self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = self.tmp
        response = self.client.post(f"/api/groups/{self.group_id}/keep",
                                    json={"candidate_id": self.candidates[1]["id"]})
        self.assertEqual(200, response.status_code)
        group = response.json()
        self.assertEqual(str(IMAGE_2), group["keep_path"])
        self.assertEqual(DecisionSource.USER, group["decision_source"])
        self.assertTrue(group["execute_requested"])
        self.assertEqual({"move"}, {a["action"] for a in group["planned_actions"]})

        self.assertEqual(400, self.client.post(f"/api/groups/{self.group_id}/keep",
                                               json={"candidate_id": 9999}).status_code)

        self.assertEqual("ignored", self.client.post(f"/api/groups/{self.group_id}/ignore").json()["status"])
        reset = self.client.post(f"/api/groups/{self.group_id}/reset").json()
        self.assertEqual("review", reset["status"])
        self.assertEqual(str(IMAGE), reset["keep_path"])

    def test_thumbnail(self):
        response = self.client.get(f"/api/candidates/{self.candidates[0]['id']}/thumbnail", params={"size": 100})
        self.assertEqual(200, response.status_code)
        self.assertEqual("image/jpeg", response.headers["content-type"])
        from PIL import Image
        import io
        with Image.open(io.BytesIO(response.content)) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), 160)
        self.assertEqual(1, len(list((self.tmp / "thumbnails").rglob("*.jpg"))))
        # served from cache the second time
        self.assertEqual(200, self.client.get(f"/api/candidates/{self.candidates[0]['id']}/thumbnail",
                                              params={"size": 100}).status_code)

    def test_image(self):
        response = self.client.get(f"/api/candidates/{self.candidates[0]['id']}/image")
        self.assertEqual(200, response.status_code)
        self.assertEqual(IMAGE.read_bytes(), response.content)

    def test_files_outside_configured_directories_are_not_served(self):
        outside = self.candidates[2]
        self.assertEqual("/etc/hostname", outside["path"])
        if Path("/etc/hostname").is_file():
            self.assertEqual(403, self.client.get(f"/api/candidates/{outside['id']}/image").status_code)
            self.assertEqual(403, self.client.get(f"/api/candidates/{outside['id']}/thumbnail").status_code)
        self.assertEqual(404, self.client.get("/api/candidates/9999/image").status_code)

    def test_moved_file_is_served_from_target(self):
        self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = self.tmp
        source = self.tmp / "source" / "moved.jpg"
        moved = self.tmp / "moved.jpg"
        shutil.copy(IMAGE, moved)
        group = self.store.record_group(run_id=None, matcher="test", candidates=[{"path": str(source)}],
                                        keep_paths=[str(source)], decided_by=None, decision_reason=None)
        candidate = self.store.get_candidates(group["id"])[0]
        self.assertEqual(404, self.client.get(f"/api/candidates/{candidate['id']}/image").status_code)

        self.store.record_action(str(source), "move", DecisionSource.RULES, dry_run=False,
                                 status=ActionStatus.DONE, target=str(moved))
        self.assertEqual(200, self.client.get(f"/api/candidates/{candidate['id']}/image").status_code)

    def test_actions_and_failures(self):
        self.store.record_action(str(IMAGE_2), "move", DecisionSource.RULES, dry_run=True,
                                 status=ActionStatus.SIMULATED, group_id=self.group_id)
        self.store.record_failure("images/broken.jpg", "OSError")
        actions = self.client.get("/api/actions", params={"group_id": self.group_id}).json()
        self.assertEqual(1, actions["total"])
        self.assertEqual(1, self.client.get("/api/failures").json()["total"])

    def test_config_and_hints(self):
        self.store.record_failure("images/broken.jpg", "OSError")
        config = self.client.get("/api/config").json()
        entries = {e["key"]: e for e in config["entries"]}
        self.assertEqual("PY_IMAGE_DEDUP_DRY_RUN", entries["py_image_dedup.dry_run"]["env"])
        self.assertTrue(entries["py_image_dedup.dry_run"]["value"])
        self.assertIn("PY_IMAGE_DEDUP_WEB_PORT", [e["env"] for e in config["entries"]])

        hint_keys = [h["key"] for h in config["hints"]]
        self.assertIn("dry_run", hint_keys)
        self.assertIn("deduplication.duplicates_target_directory", hint_keys)
        self.assertIn("analysis", hint_keys)
        messages = " ".join(h["message"] for h in config["hints"])
        # tests/py_image_dedup.yaml contains rules that do not exist
        self.assertIn("longer-path", messages)
        # the test group was decided by a weak rule
        self.assertIn("weak tie-breaker", messages)
        # the test group keeps the bigger file
        self.assertNotIn("keeps_smaller_copy", " ".join(h.get("link", "") for h in config["hints"]))

    def test_spa_fallback(self):
        self.assertEqual("<html>spa</html>", self.client.get("/").text)
        self.assertEqual("<html>spa</html>", self.client.get("/groups/1").text)
        asset = self.client.get("/assets/app.js")
        self.assertEqual("console.log(1)", asset.text)
        self.assertIn("immutable", asset.headers["cache-control"])
        # no path traversal out of the static directory
        self.assertNotIn("secret", self.client.get("/..%2Fsecret.txt").text)
        self.assertNotIn("secret", self.client.get("/assets/..%2F..%2Fsecret.txt").text)

    def test_unknown_api_path(self):
        self.assertEqual(404, self.client.get("/api/does-not-exist").status_code)
        self.assertEqual(404, self.client.post("/api/does-not-exist").status_code)

    def test_metrics(self):
        response = self.client.get("/metrics")
        self.assertEqual(200, response.status_code)
        self.assertIn("duplicate_action_total", response.text)

    def test_without_built_ui(self):
        app = create_app(self.store, self.config, static_directory=self.tmp / "missing",
                         thumbnail_cache_directory=self.tmp / "thumbnails")
        client = TestClient(app)
        self.assertIn("/api/docs", client.get("/").text)
        self.assertEqual(200, client.get("/api/health").status_code)
