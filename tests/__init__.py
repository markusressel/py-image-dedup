import unittest

from py_image_dedup.config import DeduplicatorConfig


class TestBase(unittest.TestCase):

    def setUp(self):
        self.config = DeduplicatorConfig()
        from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
        self.under_test = ImageMatchDeduplicator(interactive=False)

    def tearDown(self):
        pass

    if __name__ == '__main__':
        unittest.main()
