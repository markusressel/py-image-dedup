from pathlib import Path

from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
from py_image_dedup.library.processing_manager import ProcessingManager
from tests import TestBase


class InstanceStateTest(TestBase):
    """
    Mutable state must belong to an instance, not be shared across instances via class attributes.
    """

    def test_deduplicators_do_not_share_state(self):
        other = ImageMatchDeduplicator(interactive=False)

        self.under_test._processed_files["/some/file.jpg"] = True

        self.assertEqual({}, other._processed_files)
        self.assertIsNotNone(self.under_test._deduplication_result)
        self.assertIsNot(self.under_test._deduplication_result, other._deduplication_result)
        self.assertNotIn("_processed_files", ImageMatchDeduplicator.__dict__)
        self.assertNotIn("_deduplication_result", ImageMatchDeduplicator.__dict__)

    def test_reset_result_creates_fresh_state(self):
        self.under_test._processed_files["/some/file.jpg"] = True
        old_result = self.under_test._deduplication_result

        self.under_test.reset_result()

        self.assertEqual({}, self.under_test._processed_files)
        self.assertIsNot(old_result, self.under_test._deduplication_result)

    def test_processing_managers_do_not_share_state(self):
        first = ProcessingManager(self.under_test)
        second = ProcessingManager(self.under_test)

        first.add(Path("/some/file.jpg"))

        self.assertIn(Path("/some/file.jpg"), first.queue)
        self.assertEqual(0, len(second.queue))
        self.assertIsNotNone(first.latest_event_time)
        self.assertIsNone(second.latest_event_time)
        self.assertIsNot(first.lock, second.lock)
