from datetime import timedelta

from container_app_conf import Config
from container_app_conf.entry.bool import BoolConfigEntry
from container_app_conf.entry.float import FloatConfigEntry
from container_app_conf.entry.int import IntConfigEntry
from container_app_conf.entry.list import ListConfigEntry
from container_app_conf.entry.string import StringConfigEntry
from container_app_conf.entry.timedelta import TimeDeltaConfigEntry

from py_image_dedup.config import NODE_MAIN, NODE_RECURSIVE, NODE_SEARCH_ACROSS_ROOT_DIRS, \
    NODE_FILE_EXTENSIONS, NODE_MAX_FILE_MODIFICATION_TIME_DIFF, NODE_SOURCE_DIRECTORIES, \
    NODE_ELASTICSEARCH, NODE_HOST, NODE_MAX_DISTANCE, NODE_ANALYSIS, \
    NODE_USE_EXIF_DATA, NODE_DEDUPLICATION, NODE_REMOVE_EMPTY_FOLDERS, NODE_DUPLICATES_TARGET_DIRECTORY, \
    NODE_AUTO_CREATE_INDEX, NODE_THREADS


class DeduplicatorConfig(Config):

    @property
    def config_file_names(self) -> [str]:
        return ["py_image_dedup"]

    ELASTICSEARCH_HOST = StringConfigEntry(
        description="Hostname of the elasticsearch backend instance to use",
        yaml_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_HOST
        ],
        default="127.0.0.1"
    )

    ELASTICSEARCH_MAX_DISTANCE = FloatConfigEntry(
        description="Maximum signature distance [0..1] to query from elasticsearch backend.",
        yaml_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_MAX_DISTANCE
        ],
        default=0.10
    )

    ELASTICSEARCH_AUTO_CREATE_INDEX = BoolConfigEntry(
        description="Whether to automatically create an index in the target database.",
        yaml_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_AUTO_CREATE_INDEX
        ],
        default=True
    )

    ANALYSIS_USE_EXIF_DATA = BoolConfigEntry(
        description="Whether to scan for EXIF data or not.",
        yaml_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_USE_EXIF_DATA
        ],
        default=True
    )

    SOURCE_DIRECTORIES = ListConfigEntry(
        description="Comma separated list of source paths to analyse and deduplicate.",
        item_type=StringConfigEntry,
        yaml_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_SOURCE_DIRECTORIES
        ],
        example=[
            "/home/myuser/pictures"
        ]
    )

    RECURSIVE = BoolConfigEntry(
        description="When set all directories will be recursively analyzed.",
        yaml_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_RECURSIVE
        ],
        default=True
    )

    SEARCH_ACROSS_ROOT_DIRS = BoolConfigEntry(
        description="When set duplicates will be found even if they are located in different root directories.",
        yaml_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_SEARCH_ACROSS_ROOT_DIRS
        ],
        default=False
    )

    FILE_EXTENSION_FILTER = ListConfigEntry(
        description="Comma separated list of file extensions.",
        item_type=StringConfigEntry,
        yaml_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_FILE_EXTENSIONS
        ],
        default=[
            ".png",
            ".jpg",
            ".jpeg"
        ]
    )

    ANALYSIS_THREADS = IntConfigEntry(
        description="Number of threads to use for image analysis phase.",
        yaml_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_THREADS
        ],
        default=1
    )

    MAX_FILE_MODIFICATION_TIME_DELTA = TimeDeltaConfigEntry(
        description="Maximum file modification date difference between multiple "
                    "duplicates to be considered the same image",
        yaml_path=[
            NODE_MAIN,
            NODE_DEDUPLICATION,
            NODE_MAX_FILE_MODIFICATION_TIME_DIFF
        ],
        default=None,
        example=timedelta(minutes=5)
    )

    REMOVE_EMPTY_FOLDERS = BoolConfigEntry(
        description="Whether to remove empty folders or not.",
        yaml_path=[
            NODE_MAIN,
            NODE_REMOVE_EMPTY_FOLDERS
        ],
        default=False
    )

    DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY = StringConfigEntry(
        description="Directory path to move duplicates to instead of deleting them.",
        yaml_path=[
            NODE_MAIN,
            NODE_DEDUPLICATION,
            NODE_DUPLICATES_TARGET_DIRECTORY
        ],
        default=None,
        example="/home/myuser/pictures/duplicates"
    )
