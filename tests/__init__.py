import unittest
from unittest.mock import patch

from py_image_dedup.config import DeduplicatorConfig


class TestBase(unittest.TestCase):

    def setUp(self):
        self.config = DeduplicatorConfig()
        # unit tests must not depend on a running elasticsearch instance
        es_backend_patcher = patch("py_image_dedup.library.deduplicator.ElasticSearchStoreBackend")
        es_backend_patcher.start()
        self.addCleanup(es_backend_patcher.stop)
        from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
        self.under_test = ImageMatchDeduplicator(interactive=False)

    def tearDown(self):
        pass

    if __name__ == '__main__':
        unittest.main()
