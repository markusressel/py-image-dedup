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
        deduplicator = ImageMatchDeduplicator(directories=[r'M:\Fotos\Markus'],
                                              file_extension_filter=[".png", ".jpg", ".jpeg"],
                                              max_dist=0.05,
                                              threads=4,
                                              recursive=True,
                                              dry_run=False)
        # deduplicator = ImageMatchDeduplicator(directories=[r'D:\test'],
        #                                       file_extension_filter=[".png", ".jpg", ".jpeg"],
        #                                       max_dist=0.05,
        #                                       threads=4,
        #                                       recursive=True,
        #                                       dry_run=False)

        # deduplicator = ImageMatchDeduplicator(directories=[r'D:\test'], max_dist=0.15, threads=4)

        # optional
        # deduplicator.analyze(
        #     recursive=True,
        #     file_extensions=[".png", ".jpg", ".jpeg"]
        # )

        result = deduplicator.deduplicate()

        print("Done!")
        print("")
        print("")

        print("Duplicates for files (indented ones are duplicates):")
        print("Removed\tDuplicates\tfilename")
        for key, value in result.get_file_duplicates().items():
            if len(value) > 0:
                if key in result.get_removed_files():
                    print("(YES)\t(Duplicates: %s) '%s':" % (len(value), key))
                else:
                    print("(no )\t(Duplicates: %s) '%s':" % (len(value), key))
            for val in value:
                if val in result.get_removed_files():
                    print("\t(YES)\t'%s'" % val)
                else:
                    print("\t(no )\t'%s'" % val)

        print("")
        print("Removed Files (%s):" % len(result.get_removed_files()))
        for value in result.get_removed_files():
            print("%s" % value)

        print("")
        print("File duplicate count: %s" % result.get_duplicate_count())

        # for r in result:
        #     print(r)


if __name__ == '__main__':
    unittest.main()
