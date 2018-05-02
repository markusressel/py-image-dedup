import unittest

from py_image_dedup.library.ImageMatchDeduplicator import ImageMatchDeduplicator


class Test(unittest.TestCase):
    under_test = ImageMatchDeduplicator(directories=[],
                                        search_across_root_directories=True,
                                        file_extension_filter=[".png", ".jpg", ".jpeg"],
                                        max_dist=0.10,
                                        threads=4,
                                        recursive=True,
                                        dry_run=False)

    def test_select_images_to_delete__newer_and_bigger(self):
        keep = self._create_default_candidate(path="C:/A.jpg", filesize=1, modification_date=2)
        dont_keep = self._create_default_candidate(path="C:/B.jpg", filesize=0, modification_date=1)

        self._run_test([keep], [dont_keep])

    def test_select_images_to_delete__newer(self):
        keep = self._create_default_candidate(path="C:/A.jpg", filesize=1, modification_date=2)
        dont_keep = self._create_default_candidate(path="C:/B.jpg", filesize=1, modification_date=1)

        self._run_test([keep], [dont_keep])

    def test_select_images_to_delete__bigger(self):
        keep = self._create_default_candidate(path="C:/A.jpg", filesize=1, modification_date=1)
        dont_keep = self._create_default_candidate(path="C:/B.jpg", filesize=0, modification_date=1)

        self._run_test([keep], [dont_keep])

    def _run_test(self, keep: [{}], dont_keep: [{}]):
        candidates = keep + dont_keep

        result = self.under_test._select_images_to_delete(candidates)
        self._test_result_outcome(result, keep, dont_keep)

        result = self.under_test._select_images_to_delete(reversed(candidates))
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
