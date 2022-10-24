import os
from datetime import timedelta

from container_app_conf import ConfigBase
from container_app_conf.entry.bool import BoolConfigEntry
from container_app_conf.entry.file import DirectoryConfigEntry
from container_app_conf.entry.float import FloatConfigEntry
from container_app_conf.entry.int import IntConfigEntry
from container_app_conf.entry.list import ListConfigEntry
from container_app_conf.entry.regex import RegexConfigEntry
from container_app_conf.entry.string import StringConfigEntry
from container_app_conf.entry.timedelta import TimeDeltaConfigEntry
from container_app_conf.source.env_source import EnvSource
from container_app_conf.source.yaml_source import YamlSource
from py_range_parse import Range

NODE_MAIN = "py_image_dedup"

NODE_DAEMON = "daemon"
NODE_PROCESSING_TIMEOUT = "processing_timeout"

NODE_FILE_OBSERVER_TYPE = "file_observer"
FILE_OBSERVER_TYPE_POLLING = "polling"
FILE_OBSERVER_TYPE_INOTIFY = "inotify"

NODE_DRY_RUN = "dry_run"

NODE_ELASTICSEARCH = "elasticsearch"

NODE_HOST = "host"
NODE_MAX_DISTANCE = "max_distance"
NODE_AUTO_CREATE_INDEX = "auto_create_index"
NODE_INDEX = "index"

NODE_ANALYSIS = "analysis"

NODE_EXCLUSIONS = "exclusions"
NODE_SOURCE_DIRECTORIES = "source_directories"
NODE_RECURSIVE = "recursive"
NODE_SEARCH_ACROSS_ROOT_DIRS = "across_dirs"
NODE_FILE_EXTENSIONS = "file_extensions"
NODE_USE_EXIF_DATA = "use_exif_data"
NODE_THREADS = "threads"

NODE_DEDUPLICATION = "deduplication"

NODE_PRIORITIZATION_RULES = "prioritization_rules"
NODE_MAX_FILE_MODIFICATION_TIME_DIFF = "max_file_modification_time_diff"
NODE_REMOVE_EMPTY_FOLDERS = "remove_empty_folders"
NODE_DUPLICATES_TARGET_DIRECTORY = "duplicates_target_directory"

NODE_STATS = "stats"
NODE_ENABLED = "enabled"
NODE_PORT = "port"


class DeduplicatorConfig(ConfigBase):

    def __new__(cls, *args, **kwargs):
        yaml_source = YamlSource("py_image_dedup")
        data_sources = [
            EnvSource(),
            yaml_source
        ]
        return super(DeduplicatorConfig, cls).__new__(cls, data_sources=data_sources)

    DRY_RUN = BoolConfigEntry(
        description="If enabled no source file will be touched",
        key_path=[
            NODE_MAIN,
            NODE_DRY_RUN
        ],
        default=True
    )

    ELASTICSEARCH_HOST = StringConfigEntry(
        description="Hostname of the elasticsearch backend instance to use.",
        key_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_HOST
        ],
        default="127.0.0.1"
    )

    ELASTICSEARCH_PORT = IntConfigEntry(
        description="Port of the elasticsearch backend instance to use.",
        key_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_PORT
        ],
        range=Range(1, 65535),
        default=9200
    )

    ELASTICSEARCH_MAX_DISTANCE = FloatConfigEntry(
        description="Maximum signature distance [0..1] to query from elasticsearch backend.",
        key_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_MAX_DISTANCE
        ],
        default=0.10
    )

    ELASTICSEARCH_AUTO_CREATE_INDEX = BoolConfigEntry(
        description="Whether to automatically create an index in the target database.",
        key_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_AUTO_CREATE_INDEX
        ],
        default=True
    )

    ELASTICSEARCH_INDEX = StringConfigEntry(
        description="The index name to use for storing and querying image analysis data.",
        key_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_INDEX
        ],
        default="images"
    )

    ANALYSIS_USE_EXIF_DATA = BoolConfigEntry(
        description="Whether to scan for EXIF data or not.",
        key_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_USE_EXIF_DATA
        ],
        default=True
    )

    SOURCE_DIRECTORIES = ListConfigEntry(
        description="Comma separated list of source paths to analyse and deduplicate.",
        item_type=DirectoryConfigEntry,
        item_args={
            "check_existence": True
        },
        key_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_SOURCE_DIRECTORIES
        ],
        required=True,
        example=[
            "/home/myuser/pictures/"
        ]
    )

    RECURSIVE = BoolConfigEntry(
        description="When set all directories will be recursively analyzed.",
        key_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_RECURSIVE
        ],
        default=True
    )

    SEARCH_ACROSS_ROOT_DIRS = BoolConfigEntry(
        description="When set duplicates will be found even if they are located in different root directories.",
        key_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_SEARCH_ACROSS_ROOT_DIRS
        ],
        default=False
    )

    FILE_EXTENSION_FILTER = ListConfigEntry(
        description="Comma separated list of file extensions.",
        item_type=StringConfigEntry,
        key_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_FILE_EXTENSIONS
        ],
        required=True,
        default=[
            ".png",
            ".jpg",
            ".jpeg"
        ]
    )

    EXCLUSIONS = ListConfigEntry(
        description="Comma separated list of regular expression filters.",
        item_type=RegexConfigEntry,
        key_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_EXCLUSIONS
        ],
        default=[]
    )

    ANALYSIS_THREADS = IntConfigEntry(
        description="Number of threads to use for image analysis phase.",
        key_path=[
            NODE_MAIN,
            NODE_ANALYSIS,
            NODE_THREADS
        ],
        default=os.cpu_count()
    )

    MAX_FILE_MODIFICATION_TIME_DELTA = TimeDeltaConfigEntry(
        description="Maximum file modification date difference between multiple "
                    "duplicates to be considered the same image",
        key_path=[
            NODE_MAIN,
            NODE_DEDUPLICATION,
            NODE_MAX_FILE_MODIFICATION_TIME_DIFF
        ],
        default=None,
        example=timedelta(minutes=5)
    )

    PRIORITIZATION_RULES = ListConfigEntry(
        description="Comma separated list of prioritization rules to use for ordering duplicate "
                    "images before proceeding with the deduplication process.",
        item_type=StringConfigEntry,
        key_path=[
            NODE_MAIN,
            NODE_DEDUPLICATION,
            NODE_PRIORITIZATION_RULES
        ],
        required=False,
        default=[
            "higher-pixel-count",
            "more-exif-data",
            "bigger-file-size",
            "newer-file-modification-date",
            "smaller-distance",
            "doesnt-contain-copy-in-file-name",
            "longer-file-name",
            "shorter-folder-path",
            "higher-score",
        ]
    )

    REMOVE_EMPTY_FOLDERS = BoolConfigEntry(
        description="Whether to remove empty folders or not.",
        key_path=[
            NODE_MAIN,
            NODE_REMOVE_EMPTY_FOLDERS
        ],
        default=False
    )

    DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY = DirectoryConfigEntry(
        description="Directory path to move duplicates to instead of deleting them.",
        key_path=[
            NODE_MAIN,
            NODE_DEDUPLICATION,
            NODE_DUPLICATES_TARGET_DIRECTORY
        ],
        check_existence=True,
        default=None,
        example="/home/myuser/pictures/duplicates/"
    )

    DAEMON_PROCESSING_TIMEOUT = TimeDeltaConfigEntry(
        description="Time to wait for filesystems changes to settle before analysing.",
        key_path=[
            NODE_MAIN,
            NODE_DAEMON,
            NODE_PROCESSING_TIMEOUT
        ],
        default="30s"
    )

    DAEMON_FILE_OBSERVER_TYPE = StringConfigEntry(
        description="Type of file observer to use.",
        key_path=[
            NODE_MAIN,
            NODE_DAEMON,
            NODE_FILE_OBSERVER_TYPE
        ],
        regex="|".join([FILE_OBSERVER_TYPE_POLLING, FILE_OBSERVER_TYPE_INOTIFY]),
        default=FILE_OBSERVER_TYPE_POLLING,
        required=True
    )

    STATS_ENABLED = BoolConfigEntry(
        description="Whether to enable prometheus statistics or not.",
        key_path=[
            NODE_MAIN,
            NODE_STATS,
            NODE_ENABLED
        ],
        default=True
    )

    STATS_PORT = IntConfigEntry(
        description="The port to expose statistics on.",
        key_path=[
            NODE_MAIN,
            NODE_STATS,
            NODE_PORT
        ],
        default=8000
    )
