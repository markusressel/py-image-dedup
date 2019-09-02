import unittest
from datetime import timedelta

from py_image_dedup.config.deduplicator_config import DeduplicatorConfig
from py_image_dedup.library.deduplicator import ImageMatchDeduplicator


class TestBase(unittest.TestCase):

    def setUp(self):
        config = DeduplicatorConfig()
        config.ELASTICSEARCH_AUTO_CREATE_INDEX.value = False

        config.MAX_FILE_MODIFICATION_TIME_DELTA.value = timedelta(seconds=100)
        self.under_test = ImageMatchDeduplicator(config)

    def tearDown(self):
        pass

    if __name__ == '__main__':
        unittest.main()
