from py_image_dedup.library.DeduplicatorConfig import DeduplicatorConfig
from py_image_dedup.library.ImageMatchDeduplicator import ImageMatchDeduplicator
from py_image_dedup.persistence.ElasticSearchStoreBackend import ElasticSearchStoreBackend
from py_image_dedup.persistence.MetadataKey import MetadataKey

deduplicatorConfig = DeduplicatorConfig(
    recursive=True,
    search_across_root_directories=False,
    file_extension_filter=[
        ".png",
        ".jpg",
        ".jpeg"
    ],
    max_file_modification_time_diff=1 * 1000 * 60 * 5,
)

deduplicator = ImageMatchDeduplicator(
    image_signature_store=ElasticSearchStoreBackend(
        host="127.0.0.1",
        max_dist=0.10,
        use_exif_data=True
    ),
    directories=[
        r'/home/markus/py-image-dedup/dir1',
        r'/home/markus/py-image-dedup/dir2'
    ],
    config=deduplicatorConfig,
    threads=8,
    dry_run=True
)

result = deduplicator.deduplicate(skip_analyze_phase=False)

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
