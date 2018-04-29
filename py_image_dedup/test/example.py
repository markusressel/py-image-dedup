import unittest


class Test(unittest.TestCase):

    def test_analyze(self):
        pass
        # runner = CliRunner()
        # result = runner.invoke(analyze, ['-d D:/test'])
        # assert result.exit_code == 0
        # print(result)

    #    analyze('-d D:/test')

    # def test_analyze(self):
    #     from py_image_dedup.library.Deduplicator import Deduplicator
    #     deduplicator = Deduplicator(['D:/test'])
    #
    #     result = deduplicator.analyze(True)
    #     print(result)

    def test_deduplicate(self):
        from py_image_dedup.library.ImageMatchDeduplicator import ImageMatchDeduplicator
        deduplicator = ImageMatchDeduplicator(directories=[r'M:\Fotos\Iris'],
                                              file_extension_filter=[".png", ".jpg", ".jpeg"],
                                              max_dist=0.15,
                                              threads=4)
        # deduplicator = ImageMatchDeduplicator(directories=[r'D:\test'], max_dist=0.15, threads=4)

        # optional
        # deduplicator.analyze(
        #     recursive=True,
        #     file_extensions=[".png", ".jpg", ".jpeg"]
        # )

        result = deduplicator.deduplicate(
            recursive=True,
            dry_run=True)

        print("Done!")
        print("")
        print("")

        for key, value in result.get_file_duplicates().items():
            if len(value) > 0:
                print("%s:" % key)
            for val in value:
                print("\t%s" % val)

        print("")
        print("File duplicate count: %s" % result.get_duplicate_count())

        # for r in result:
        #     print(r)


if __name__ == '__main__':
    unittest.main()
