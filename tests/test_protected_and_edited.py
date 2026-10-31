import shutil
import tempfile
from pathlib import Path

from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
from py_image_dedup.persistence.metadata_key import MetadataKey
from py_image_dedup.state import StateStore, ActionStatus, GroupStatus
from tests import TestBase

CAMERA = "/data/photos/Markus/Camera/IMG_20220417_200424.jpg"
EDITED = "/data/photos/Markus/Camera/IMG_20220417_200424_edit.jpg"
TELEGRAM = "/data/photos/Markus/Telegram/IMG_20220417_200424.jpg"
ALBUM = "/data/photos/Familie/Hochzeit/IMG_20220417_200424.jpg"


def _candidate(path: str, modification_date: float = 1, file_size: int = 100, software: str | None = None) -> dict:
    exif = {"Software": software} if software else {}
    return {
        MetadataKey.PATH.value: path,
        MetadataKey.DISTANCE.value: 0.0,
        MetadataKey.SCORE.value: 64,
        MetadataKey.METADATA.value: {
            MetadataKey.FILE_SIZE.value: file_size,
            MetadataKey.FILE_MODIFICATION_DATE.value: modification_date,
            MetadataKey.PIXELCOUNT.value: 10000,
            MetadataKey.EXIF_DATA.value: exif,
        },
    }


class _ConfigTestBase(TestBase):

    def setUp(self):
        super().setUp()
        for entry in [self.config.PRIORITIZATION_RULES, self.config.PROTECTED_PATHS, self.config.DRY_RUN,
                      self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY,
                      self.config.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS]:
            self.addCleanup(setattr, entry, "value", entry.value)
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.dedup = ImageMatchDeduplicator(interactive=False, state_store=self.store)


class PreferEditedTest(_ConfigTestBase):

    def _ranked(self, *candidates) -> list:
        return [c[MetadataKey.PATH.value]
                for c in ImageMatchDeduplicator._sort_by_quality_descending(list(candidates))]

    def test_edited_file_name_wins(self):
        self.config.PRIORITIZATION_RULES.value = [{"name": "prefer-edited"}, {"name": "bigger-file-size"}]
        self.assertEqual([EDITED, CAMERA], self._ranked(_candidate(CAMERA, file_size=200), _candidate(EDITED)))

    def test_editing_software_wins(self):
        self.config.PRIORITIZATION_RULES.value = [{"name": "prefer-edited"}, {"name": "bigger-file-size"}]
        lightroom = _candidate(TELEGRAM, software="Adobe Photoshop Lightroom Classic 13.5.1 (Macintosh)")
        self.assertEqual([TELEGRAM, CAMERA],
                         self._ranked(_candidate(CAMERA, file_size=200, software="F936BXXS7FXEA"), lightroom))
        criteria = ImageMatchDeduplicator._quality_criteria(lightroom)
        self.assertEqual("edited: edited with 'Adobe Photoshop Lightroom Classic 13.5.1 (Macintosh)'",
                         criteria[0][2])

    def test_custom_patterns(self):
        self.config.PRIORITIZATION_RULES.value = [{"name": "prefer-edited", "file_name_patterns": ["-mine$"]}]
        mine = "/data/photos/a/IMG_1-mine.jpg"
        self.assertEqual([mine, EDITED], self._ranked(_candidate(EDITED), _candidate(mine)))

    def test_edited_copy_and_original_need_review(self):
        self.config.PRIORITIZATION_RULES.value = [{"name": "prefer-edited"}, {"name": "bigger-file-size"}]
        self.config.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS.value = True
        edited, original = _candidate(EDITED), _candidate(CAMERA)

        self.assertIsNone(self.dedup._record_group([edited], [original]))

        group = self.store.list_groups()["items"][0]
        self.assertEqual((GroupStatus.REVIEW, EDITED), (group["status"], group["keep_path"]))
        self.assertIn("edited copy", group["review_reason"])

    def test_copies_of_an_edit_need_no_review(self):
        self.config.PRIORITIZATION_RULES.value = [{"name": "prefer-edited"}, {"name": "bigger-file-size"}]
        self.config.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS.value = True
        other_copy = "/data/photos/Markus/Telegram/IMG_20220417_200424_edit.jpg"

        decision = self.dedup._record_group([_candidate(EDITED, file_size=200)], [_candidate(other_copy)])

        self.assertEqual([other_copy], [c[MetadataKey.PATH.value] for c in decision[1]])


class ProtectedPathsTest(_ConfigTestBase):

    def test_protected_files_are_kept(self):
        self.config.PRIORITIZATION_RULES.value = [{"name": "bigger-file-size"}]
        self.config.PROTECTED_PATHS.value = ["/Familie/"]

        keep, dont_keep = self.dedup._select_images_to_delete(
            [_candidate(ALBUM), _candidate(CAMERA, file_size=200), _candidate(TELEGRAM)])

        self.assertEqual([CAMERA, ALBUM], [c[MetadataKey.PATH.value] for c in keep])
        self.assertEqual([TELEGRAM], [c[MetadataKey.PATH.value] for c in dont_keep])

        self.dedup._record_group(keep, dont_keep)
        candidates = self.store.get_candidates(self.store.list_groups()["items"][0]["id"])
        album = next(c for c in candidates if c["path"] == ALBUM)
        self.assertEqual(("keep", "in a protected path ('/Familie/')"), (album["role"], album["keep_reason"]))

    def test_user_decision_does_not_move_protected_files(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "Familie").mkdir()
        (tmp / "Camera").mkdir()
        (tmp / "duplicates").mkdir()
        album, camera = tmp / "Familie" / "a.jpg", tmp / "Camera" / "a.jpg"
        for path in (album, camera):
            shutil.copy("images/bottles/IMG_20190903_193151.jpg", path)
        self.config.DRY_RUN.value = False
        self.config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = tmp / "duplicates"
        self.config.PROTECTED_PATHS.value = ["/Familie/"]

        group = self.store.record_group(run_id=None, matcher="test", candidates=[{"path": str(album)},
                                                                                 {"path": str(camera)}],
                                        keep_paths=[str(album)], decided_by="x", decision_reason="test")
        self.store.set_user_keeper(group["id"], str(camera))
        self.dedup.execute_user_decisions()

        self.assertTrue(album.exists())
        actions = self.store.list_actions()["items"]
        self.assertEqual([(str(album), ActionStatus.SKIPPED)], [(a["path"], a["status"]) for a in actions])
