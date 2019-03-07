import unittest
from random import shuffle
from random import uniform

from py_image_dedup.library.ImageMatchDeduplicator import ImageMatchDeduplicator
from py_image_dedup.persistence.MetadataKey import MetadataKey


class SelectImagesToDeleteTest(unittest.TestCase):
    under_test = ImageMatchDeduplicator(
        image_signature_store=None,
        directories=[],
        search_across_root_directories=True,
        max_file_modification_time_diff=100,
        file_extension_filter=[".png", ".jpg", ".jpeg"],
        threads=4,
        recursive=True,
        dry_run=True)

    def test_select_images_to_delete__filter_max_mod_time_diff(self):
        keep = [
            self._create_default_candidate(modification_date=100),
            self._create_default_candidate(modification_date=-1000)  # file modification time is too far apart
        ]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(modification_date=0)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__contains_copy(self):
        keep = [self._create_default_candidate(path="C:/1.jpg")]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/1%s-Copy.jpg" % i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__newer_and_bigger(self):
        keep = [self._create_default_candidate(filesize=100, modification_date=100)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(filesize=i, modification_date=i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__newer(self):
        keep = [self._create_default_candidate(modification_date=100)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(modification_date=i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__bigger(self):
        keep = [self._create_default_candidate(filesize=100)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(filesize=i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__all_the_same(self):
        keep = [self._create_default_candidate(path="C:/00000.jpg")]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/1%s.jpg" % i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__all_the_same_2(self):
        keep = [self._create_default_candidate(path="C:/50-edited.jpg")]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(path="C:/%s.jpg" % i)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__higher_score(self):
        keep = [self._create_default_candidate(score=100)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate()
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__lower_dist(self):
        keep = [self._create_default_candidate(dist=0)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(dist=uniform(0.1, 1.0))
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def test_select_images_to_delete__real_example(self):
        keep = [self._create_default_candidate(
            path=r"M:\Fotos\Markus\Google Photos Archiv\Takeout\Google Photos\2017-06-17\20170617_153437.jpg",
            filesize=10000000, modification_date=1)]

        dont_keep = []
        for i in range(50):
            c = self._create_default_candidate(
                path=r"M:\Fotos\Iris\Syncthing\Telegram Empfangen\223023133_644761%i.jpg" % i,
                filesize=270000, modification_date=2)
            dont_keep.append(c)

        self._run_test(keep, dont_keep)

    def _run_test(self, keep: [{}], dont_keep: [{}], test_reversed_order: bool = True,
                  test_random_input_order: bool = True):
        candidates = keep + dont_keep

        result = self.under_test._select_images_to_delete(candidates)
        self._test_result_outcome(result, keep, dont_keep)

        if test_reversed_order:
            result = self.under_test._select_images_to_delete(reversed(candidates))
            self._test_result_outcome(result, keep, dont_keep)

        if test_random_input_order:
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
            MetadataKey.PATH.value: path,
            MetadataKey.DISTANCE.value: dist,
            MetadataKey.METADATA.value: {
                MetadataKey.FILE_SIZE.value: filesize,
                MetadataKey.FILE_MODIFICATION_DATE.value: modification_date
            },
            MetadataKey.SCORE.value: score
        }


if __name__ == '__main__':
    unittest.main()
