from datetime import timedelta

from py_image_dedup.config.deduplicator_config import DeduplicatorConfig
from py_image_dedup.library.image_match_deduplicator import ImageMatchDeduplicator

config = DeduplicatorConfig()
# config.ELASTICSEARCH_HOST.value = "192.168.2.24"
config.SOURCE_DIRECTORIES.value = [
    # r'/home/markus/py-image-dedup/dir1',
    # r'/home/markus/py-image-dedup/dir2'
    r'/mnt/data/py-dedup-test/Syncthing',
    r'./tests/images'
]
config.SEARCH_ACROSS_ROOT_DIRS.value = True

config.ANALYSIS_THREADS.value = 8
config.ANALYSIS_USE_EXIF_DATA.value = False

config.ELASTICSEARCH_MAX_DISTANCE.value = 0.30
config.MAX_FILE_MODIFICATION_TIME_DELTA.value = timedelta(minutes=5)
config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value = "./duplicates"
config.REMOVE_EMPTY_FOLDERS.value = True

deduplicator = ImageMatchDeduplicator(config)

# max_file_modification_time_diff=1 * 1000 * 60 * 5,

result = deduplicator.deduplicate(
    skip_analyze_phase=False,
    dry_run=True
)

result.print_to_console()
