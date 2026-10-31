import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from watchdog.events import FileMovedEvent, FileCreatedEvent, DirCreatedEvent, FileOpenedEvent, \
    FileClosedEvent, FileClosedNoWriteEvent

from py_image_dedup.config import FILE_OBSERVER_TYPE_INOTIFY, FILE_OBSERVER_TYPE_POLLING
from py_image_dedup.library.processing_manager import ProcessingManager
from py_image_dedup.persistence.metadata_key import MetadataKey
from py_image_dedup.state import StateStore
from tests import TestBase
from tests.test_deduplicator_state import _candidate


def _wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class _ProcessingManagerTestBase(TestBase):

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.manager = ProcessingManager(MagicMock())
        # only accept events of the temporary source directory
        self.manager.event_handler.directory_regex = re.compile(rf"^{re.escape(str(self.root))}.*$")

    def _queued(self) -> set:
        with self.manager.lock:
            return set(self.manager.queue.keys())


class WatchNewDirectoriesTest(_ProcessingManagerTestBase):
    """
    Files in directories that are created after the daemon started must be processed (issue #22).
    """

    def _start_observer(self, observer_type: str):
        observers = self.manager._setup_file_observers(observer_type, [self.root])
        self.manager.observers = observers
        self.addCleanup(self.manager.stop)

    def _assert_new_directories_are_watched(self):
        # a new directory, a file is added later
        new_dir = self.root / "new"
        new_dir.mkdir()
        time.sleep(0.2)
        new_file = new_dir / "image.jpg"
        new_file.write_bytes(b"x")

        # nested directories with a file, created at once
        nested_file = self.root / "a" / "b" / "c" / "image.png"
        nested_file.parent.mkdir(parents=True)
        nested_file.write_bytes(b"x")

        # a directory with a file moved in from outside of the source directory
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "moved").mkdir()
        (outside / "moved" / "image.jpeg").write_bytes(b"x")
        shutil.move(outside / "moved", self.root / "moved")
        moved_file = self.root / "moved" / "image.jpeg"

        expected = {new_file, nested_file, moved_file}
        self.assertTrue(_wait_for(lambda: expected <= self._queued()), f"queued: {self._queued()}")

        # a file created in the new directory after its creation is watched too
        later_file = nested_file.parent / "later.jpg"
        later_file.write_bytes(b"x")
        self.assertTrue(_wait_for(lambda: later_file in self._queued()), f"queued: {self._queued()}")

    def test_inotify_observer(self):
        self._start_observer(FILE_OBSERVER_TYPE_INOTIFY)
        self._assert_new_directories_are_watched()

    def test_polling_observer(self):
        self._start_observer(FILE_OBSERVER_TYPE_POLLING)
        self._assert_new_directories_are_watched()


class EventHandlerFilterTest(_ProcessingManagerTestBase):

    def test_open_and_close_events_are_ignored(self):
        # watchdog >= 2.1 emits these, an exception would stop the observer thread
        path = str(self.root / "image.jpg")
        for event in [FileOpenedEvent(path), FileClosedEvent(path), FileClosedNoWriteEvent(path)]:
            self.manager.event_handler.on_any_event(event)
        self.assertEqual(set(), self._queued())

    def test_directory_events_are_ignored(self):
        self.manager.event_handler.on_any_event(DirCreatedEvent(str(self.root / "new")))
        self.assertEqual(set(), self._queued())

    def test_file_events_outside_of_filter_are_ignored(self):
        self.manager.event_handler.on_any_event(FileCreatedEvent(str(self.root / "notes.txt")))
        self.manager.event_handler.on_any_event(FileCreatedEvent("/somewhere/else/image.jpg"))
        self.assertEqual(set(), self._queued())

    def test_rename_into_filter_is_processed(self):
        # f.ex. a sync tool writing a temporary file first
        self.manager.event_handler.on_any_event(
            FileMovedEvent(str(self.root / "image.jpg.part"), str(self.root / "image.jpg")))
        self.assertEqual({self.root / "image.jpg"}, self._queued())
        self.manager.deduplicator._persistence.remove.assert_not_called()

    def test_rename_out_of_filter_is_cleaned_up(self):
        self.manager.add(self.root / "image.jpg")
        self.manager.event_handler.on_any_event(
            FileMovedEvent(str(self.root / "image.jpg"), str(self.root / "image.jpg.bak")))
        self.assertEqual(set(), self._queued())
        self.manager.deduplicator._persistence.remove.assert_called_once_with(str(self.root / "image.jpg"))


class ProcessingManagerLockTest(_ProcessingManagerTestBase):

    def test_remove_waits_for_lock(self):
        path = self.root / "image.jpg"
        self.manager.add(path)

        with self.manager.lock:
            thread = threading.Thread(target=self.manager.remove, args=(path,))
            thread.start()
            thread.join(0.2)
            # the queue is not modified while processing holds the lock
            self.assertTrue(thread.is_alive())
            self.assertIn(path, self.manager.queue)

        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn(path, self.manager.queue)
        self.manager.deduplicator._persistence.remove.assert_called_once_with(str(path))

    def test_remove_unknown_path(self):
        self.manager.remove(self.root / "unknown.jpg")
        self.assertEqual(set(), self._queued())


class MissingCandidatesTest(TestBase):
    """
    Files deleted after the database cleanup phase are still returned by the backend.
    """

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
        self.dedup = ImageMatchDeduplicator(interactive=False, state_store=self.store)
        self.dedup._progress_manager.start("test", 1, "Files", False)
        self.addCleanup(self.dedup._progress_manager.clear)

    def _file(self, name: str) -> str:
        path = self.root / name
        path.write_bytes(b"x")
        return str(path)

    def _find(self, candidates):
        self.dedup._persistence.find_similar.return_value = candidates
        self.dedup.find_duplicates_of_file(root_directories=[self.root], root_directory=self.root,
                                           reference_file_path=Path(candidates[0][MetadataKey.PATH.value]))

    def test_missing_candidates_are_dropped(self):
        small = self._file("small.jpg")
        medium = self._file("medium.jpg")
        # deleted in the meantime, would have been the file to keep
        missing = str(self.root / "missing.jpg")
        self._find([
            _candidate(small, filesize=10),
            _candidate(medium, filesize=100),
            _candidate(missing, filesize=1000),
        ])

        groups = self.store.list_groups()["items"]
        self.assertEqual(1, len(groups))
        group = self.store.get_group(groups[0]["id"])
        self.assertEqual(medium, group["keep_path"])
        self.assertEqual({small, medium}, {c["path"] for c in group["candidates"]})
        self.assertEqual([Path(small)], self.dedup._deduplication_result.get_removed_or_moved_files())
        # the stale database entry is removed
        self.dedup._persistence.remove.assert_called_with(missing)

    def test_no_group_if_only_one_file_is_left(self):
        existing = self._file("existing.jpg")
        missing = str(self.root / "missing.jpg")
        self._find([_candidate(existing, filesize=10), _candidate(missing, filesize=1000)])

        self.assertEqual(0, self.store.list_groups()["total"])
        self.assertEqual([], self.dedup._deduplication_result.get_removed_or_moved_files())
        self.dedup._persistence.remove.assert_called_with(missing)

    def test_missing_candidates_outside_of_root_directories_are_kept_in_database(self):
        small = self._file("small.jpg")
        big = self._file("big.jpg")
        other_machine = "/some/other/machine/big.jpg"
        self.dedup._persistence.find_similar.return_value = [
            _candidate(small, filesize=10),
            _candidate(big, filesize=1000),
            _candidate(other_machine, filesize=10000),
        ]
        result = self.dedup._without_missing_files(self.dedup._persistence.find_similar(), [self.root])

        self.assertEqual([small, big], [c[MetadataKey.PATH.value] for c in result])
        self.dedup._persistence.remove.assert_not_called()
