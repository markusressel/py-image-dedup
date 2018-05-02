import unittest
from random import shuffle
from random import uniform

from py_image_dedup.library.ImageMatchDeduplicator import ImageMatchDeduplicator


class Test(unittest.TestCase):
    under_test = ImageMatchDeduplicator(directories=[],
                                        find_duplicatest_across_root_directories=True,
                                        file_extension_filter=[".png", ".jpg", ".jpeg"],
                                        max_dist=0.10,
                                        threads=4,
                                        recursive=True,
                                        dry_run=False)

    def test_select_images_to_delete__newer_and_bigger(self):
        keep = [self._create_default_candidate(path="C:/A.jpg", filesize=10, modification_date=10)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/1%s.jpg" % i, filesize=i, modification_date=i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__newer(self):
        keep = [self._create_default_candidate(path="C:/A.jpg", filesize=1, modification_date=10)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/1%s.jpg" % i, filesize=1, modification_date=i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__bigger(self):
        keep = [self._create_default_candidate(path="C:/A.jpg", filesize=10, modification_date=1)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/1%s.jpg" % i, filesize=i, modification_date=1)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__all_the_same(self):
        keep = [self._create_default_candidate(path="C:/A.jpg")]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/1%s.jpg" % i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__higher_score(self):
        keep = [self._create_default_candidate(path="C:/A.jpg", score=100)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/1%s.jpg" % i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__lower_dist(self):
        keep = [self._create_default_candidate(path="C:/0.jpg", dist=0)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/1%s.jpg" % i, dist=uniform(0.1, 1.0))
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__real_example(self):
        keep = [self._create_default_candidate(
            path=r"M:\Fotos\Markus\Google Photos Archiv\Takeout\Google Photos\2017-06-17\20170617_153437.jpg",
            filesize=10000000)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path=r"M:\Fotos\Iris\Syncthing\Telegram Empfangen\223023133_644761.jpg",
                                               filesize=270000)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def _run_test(self, keep: [{}], dont_keep: [{}]):
        candidates = keep + dont_keep

        result = self.under_test._select_images_to_delete(candidates)
        self._test_result_outcome(result, keep, dont_keep)

        result = self.under_test._select_images_to_delete(reversed(candidates))
        self._test_result_outcome(result, keep, dont_keep)

        # test random sort orders of input just to be sure
        for i in range(50):
            shuffle(candidates)
            result = self.under_test._select_images_to_delete(candidates)
            self._test_result_outcome(result, keep, dont_keep)

    def _test_result_outcome(self, result: [{}], keep: [{}], dont_keep: [{}]):
        for c in keep:
            self.assertNotIn(c, result)
        for c in dont_keep:
            self.assertIn(c, result)

    def _create_default_candidate(self, path: str = "C:/test", dist: float = 0.05, filesize: int = 100,
                                  modification_date: int = 1, score: int = 64) -> {}:
        return {
            'path': path,
            'dist': dist,
            'metadata': {
                'filesize': filesize,
                'modification_date': modification_date
            },
            'score': score
        }


if __name__ == '__main__':
    unittest.main()
