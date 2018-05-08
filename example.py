from py_image_dedup.library.ImageMatchDeduplicator import ImageMatchDeduplicator
from py_image_dedup.persistence.MetadataKey import MetadataKey

deduplicator = ImageMatchDeduplicator(
    database_host="192.168.2.24",
    # directories=[r'C:\Sample'],
    directories=[r'M:\Fotos\Iris', r'M:\Fotos\Markus'],
    find_duplicatest_across_root_directories=True,
    file_extension_filter=[".png", ".jpg", ".jpeg"],  # Note: case insensitive
    max_dist=0.10,
    threads=4,
    recursive=True,
    dry_run=True
)

result = deduplicator.deduplicate()

print("Done!")
print("")
print("")

print("Duplicates for files (indented ones are duplicates):")
for reference_file, duplicates in result.get_file_duplicates().items():
    duplicate_count = len(duplicates)

    if duplicate_count > 0:
        if reference_file in result.get_removed_files():
            print("(DELE)\t\t'%s'\t\t(Count: %s)" % (reference_file, duplicate_count))
        else:
            print("(keep)\t\t'%s'\t\t(Count: %s)" % (reference_file, duplicate_count))

        for duplicate in duplicates:
            file_path = duplicate[MetadataKey.PATH.value]
            distance = duplicate[MetadataKey.DISTANCE.value]
            distance_rounded = round(distance, 3)

            if file_path in result.get_removed_files():
                print("  >(DELE)\t'%s'\t\t(Dist: %s)" % (file_path, distance_rounded))
            else:
                print("  >(keep)\t'%s'\t\t(Dist: %s)" % (file_path, distance_rounded))

        print("")

print("")
print("Removed Files (%s):" % len(result.get_removed_files()))
for duplicates in result.get_removed_files():
    print("  %s" % duplicates)

print("")
print("Removed Folders (%s):" % len(result.get_removed_empty_folders()))
for duplicates in result.get_removed_empty_folders():
    print("  %s" % duplicates)

print("")
print("Duplicate files count: %s" % result.get_duplicate_count())
print("Empty Folder count: %s" % len(result.get_removed_empty_folders()))
