import unittest


class TestBase(unittest.TestCase):
    def setUp(self):
        from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
        self.under_test = ImageMatchDeduplicator()

    def tearDown(self):
        pass

    if __name__ == '__main__':
        unittest.main()
