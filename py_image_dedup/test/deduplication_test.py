import unittest


class Test(unittest.TestCase):

    def test_deduplicate(self):
        from py_image_dedup.library.ImageMatchDeduplicator import ImageMatchDeduplicator

        directories = []
        # directories = [r'D:\test']

        deduplicator = ImageMatchDeduplicator(directories=directories,
                                              search_across_root_directories=True,
                                              file_extension_filter=[".png", ".jpg", ".jpeg"],
                                              max_dist=0.10,
                                              threads=4,
                                              recursive=True,
                                              dry_run=False)

        result = deduplicator.deduplicate()

        print("Done!")
        print("")
        print("")

        print("Duplicates for files (indented ones are duplicates):")
        print("Action\t|Filename")
        for key, value in result.get_file_duplicates().items():
            if len(value) > 0:
                if key in result.get_removed_files():
                    print("(REM)\t'%s' (Count: %s)" % (key, len(value)))
                else:
                    print("(no )\t'%s' (Count: %s)" % (key, len(value)))

                for val in value:
                    file_path = val['path']
                    distance = val['dist']

                    if file_path in result.get_removed_files():
                        print("  (REM)\t'%s': %s" % (file_path, round(distance, 3)))
                    else:
                        print("  (no )\t'%s': %s" % (file_path, round(distance, 3)))

                print("")

        print("")
        print("Removed Files (%s):" % len(result.get_removed_files()))
        for value in result.get_removed_files():
            print("  %s" % value)

        print("Removed Folders (%s):" % len(result.get_removed_empty_folders()))
        for value in result.get_removed_empty_folders():
            print("  %s" % value)

        print("")
        print("File duplicate count: %s" % result.get_duplicate_count())
        print("Empty Folder count: %s" % len(result.get_removed_empty_folders()))


if __name__ == '__main__':
    unittest.main()
