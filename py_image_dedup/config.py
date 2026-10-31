import os
from datetime import timedelta
from pathlib import Path

from container_app_conf import ConfigBase
from container_app_conf.entry.bool import BoolConfigEntry
from container_app_conf.entry.dict import DictConfigEntry
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

from py_image_dedup.matching.embeddings import DEFAULT_MIN_SIMILARITY
from py_image_dedup.matching.models import MatchingThresholds

NODE_MAIN = "py_image_dedup"

NODE_DAEMON = "daemon"
NODE_PROCESSING_TIMEOUT = "processing_timeout"

NODE_FILE_OBSERVER_TYPE = "file_observer"
FILE_OBSERVER_TYPE_POLLING = "polling"
FILE_OBSERVER_TYPE_INOTIFY = "inotify"

NODE_DRY_RUN = "dry_run"
NODE_DRY_RUN_USER_DECISIONS = "dry_run_user_decisions"

NODE_ELASTICSEARCH = "elasticsearch"

NODE_HOST = "host"
NODE_MAX_DISTANCE = "max_distance"
NODE_AUTO_CREATE_INDEX = "auto_create_index"
NODE_INDEX = "index"
NODE_CONNECTION_TIMEOUT = "connection_timeout"

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
NODE_PROTECTED_PATHS = "protected_paths"

NODE_HOLD_AMBIGUOUS_GROUPS = "hold_ambiguous_groups"

NODE_STATS = "stats"
NODE_ENABLED = "enabled"
NODE_PORT = "port"

NODE_STATE = "state"
NODE_DATABASE_PATH = "database_path"

NODE_WEB = "web"

NODE_IMMICH = "immich"
NODE_URL = "url"
NODE_API_KEY = "api_key"
NODE_PATH_MAPPINGS = "path_mappings"
NODE_PUBLIC_URL = "public_url"
NODE_RESCAN_AFTER_CHANGES = "rescan_after_changes"
NODE_STATIC_DIRECTORY = "static_directory"

NODE_MATCHING = "matching"
NODE_BACKEND = "backend"
NODE_STORE_URL = "store_url"
NODE_MAX_PHASH_DISTANCE = "max_phash_distance"
NODE_MAX_DHASH_DISTANCE = "max_dhash_distance"
NODE_BURST_WINDOW_SECONDS = "burst_window_seconds"
NODE_BURST_CONFIDENCE_PENALTY = "burst_confidence_penalty"
NODE_AUTO_ACT_MIN_CONFIDENCE = "auto_act_min_confidence"
NODE_COLOUR_EDIT_TOLERANCE = "colour_edit_tolerance"
NODE_ASPECT_RATIO_TOLERANCE = "aspect_ratio_tolerance"
NODE_MATCH_ROTATIONS = "match_rotations"
NODE_EMBEDDINGS = "embeddings"
NODE_MODEL_PATH = "model_path"
NODE_MODEL_URL = "model_url"
NODE_MODEL_SHA256 = "model_sha256"
NODE_CACHE_DIRECTORY = "cache_directory"
NODE_SIMILARITY_THRESHOLD = "similarity_threshold"

# matching.backend: which matcher finds the duplicate groups
MATCHING_BACKEND_IMAGE_MATCH = "image-match"  # image-match + Elasticsearch (default)
MATCHING_BACKEND_HASHES = "hashes"  # the hash pipeline (py_image_dedup/matching), no Elasticsearch
MATCHING_BACKEND_BOTH = "both"  # image-match decides, the hash pipeline only records its groups for comparison
MATCHING_BACKENDS = [MATCHING_BACKEND_IMAGE_MATCH, MATCHING_BACKEND_HASHES, MATCHING_BACKEND_BOTH]

# names of the matchers in the state store (groups.matcher)
MATCHER_IMAGE_MATCH = "image-match"
MATCHER_HASHES = "hashes"

DEFAULT_STATE_DATABASE_PATH = "~/.local/share/py-image-dedup/state.db"

_DEFAULT_THRESHOLDS = MatchingThresholds()


class DeduplicatorConfig(ConfigBase):

    def __new__(cls, *args, **kwargs):
        yaml_source = YamlSource("py_image_dedup")
        data_sources = [
            EnvSource(),
            yaml_source
        ]
        return super(DeduplicatorConfig, cls).__new__(cls, *args, data_sources=data_sources, **kwargs)

    DRY_RUN = BoolConfigEntry(
        description="If enabled no source file will be touched",
        key_path=[
            NODE_MAIN,
            NODE_DRY_RUN
        ],
        default=True
    )

    DRY_RUN_USER_DECISIONS = BoolConfigEntry(
        description="Overrides dry_run for decisions made by the user (web UI, imported decisions). "
                    "F.ex. set dry_run to true and this to false to only move the duplicates you decided on. "
                    "If not set, dry_run applies.",
        key_path=[
            NODE_MAIN,
            NODE_DRY_RUN_USER_DECISIONS
        ],
        required=False,
        default=None
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

    ELASTICSEARCH_CONNECTION_TIMEOUT = TimeDeltaConfigEntry(
        description="Maximum time to wait for the elasticsearch backend to become reachable on startup.",
        key_path=[
            NODE_MAIN,
            NODE_ELASTICSEARCH,
            NODE_CONNECTION_TIMEOUT
        ],
        default="2m"
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

    PROTECTED_PATHS = ListConfigEntry(
        description="Regular expressions (matched anywhere in the file path) of files that are never moved or "
                    "deleted, f.ex. curated albums. If the best copy of a group is somewhere else, both are kept.",
        item_type=RegexConfigEntry,
        key_path=[
            NODE_MAIN,
            NODE_DEDUPLICATION,
            NODE_PROTECTED_PATHS
        ],
        required=False,
        default=[],
        example=["/Familie/"]
    )

    PRIORITIZATION_RULES = ListConfigEntry(
        description="Comma separated list of prioritization rules to use for ordering duplicate "
                    "images before proceeding with the deduplication process.",
        item_type=DictConfigEntry,
        key_path=[
            NODE_MAIN,
            NODE_DEDUPLICATION,
            NODE_PRIORITIZATION_RULES
        ],
        required=False,
        default=[
            {"name": "higher-pixel-count"},
            {"name": "more-exif-data"},
            {"name": "bigger-file-size"},
            {"name": "newer-file-modification-date"},
            {"name": "smaller-distance"},
            {"name": "doesnt-contain-copy-in-file-name"},
            {"name": "longer-file-name"},
            {"name": "shorter-folder-path"},
            {"name": "higher-score"},
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

    DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS = BoolConfigEntry(
        description="If enabled, duplicate groups whose best copy was only chosen by a weak tie-breaker rule "
                    "(e.g. file name or folder path length) are not moved/deleted automatically, "
                    "but wait for a decision in the web UI.",
        key_path=[
            NODE_MAIN,
            NODE_DEDUPLICATION,
            NODE_HOLD_AMBIGUOUS_GROUPS
        ],
        default=False
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

    STATE_DATABASE_PATH = StringConfigEntry(
        description="Path of the SQLite database holding the daemon state shown in the web UI "
                    "(runs, duplicate groups, decisions, action log). Thumbnails are cached next to it.",
        key_path=[
            NODE_MAIN,
            NODE_STATE,
            NODE_DATABASE_PATH
        ],
        default=DEFAULT_STATE_DATABASE_PATH,
        example="/data/py-image-dedup/state.db"
    )

    WEB_ENABLED = BoolConfigEntry(
        description="Whether to start the web UI (and its JSON API) when running the daemon.",
        key_path=[
            NODE_MAIN,
            NODE_WEB,
            NODE_ENABLED
        ],
        default=True
    )

    WEB_HOST = StringConfigEntry(
        description="The address the web UI listens on. The web UI has no authentication, "
                    "only expose it to trusted networks.",
        key_path=[
            NODE_MAIN,
            NODE_WEB,
            NODE_HOST
        ],
        default="127.0.0.1"
    )

    WEB_PORT = IntConfigEntry(
        description="The port the web UI listens on.",
        key_path=[
            NODE_MAIN,
            NODE_WEB,
            NODE_PORT
        ],
        range=Range(1, 65535),
        default=8080
    )

    WEB_STATIC_DIRECTORY = DirectoryConfigEntry(
        description="Directory containing the built web UI (web/dist). "
                    "If unset, web/dist next to the py_image_dedup package is used, if it exists.",
        key_path=[
            NODE_MAIN,
            NODE_WEB,
            NODE_STATIC_DIRECTORY
        ],
        check_existence=False,
        default=None,
        example="/app/web/dist"
    )

    IMMICH_URL = StringConfigEntry(
        description="Base URL of an Immich server. Enables importing the files trashed in Immich "
                    "(f.ex. by its duplicate review) as decisions for the duplicate groups.",
        key_path=[
            NODE_MAIN,
            NODE_IMMICH,
            NODE_URL
        ],
        required=False,
        default=None,
        example="http://immich-server:2283"
    )

    IMMICH_API_KEY = StringConfigEntry(
        description="Immich API key (needs the asset.read permission) of the user who owns the trashed assets.",
        key_path=[
            NODE_MAIN,
            NODE_IMMICH,
            NODE_API_KEY
        ],
        required=False,
        default=None,
        secret=True
    )

    IMMICH_PATH_MAPPINGS = ListConfigEntry(
        description="Maps the file paths Immich reports (immich) to the paths this application sees (local), "
                    "f.ex. when Immich mounts the photo library at a different location. "
                    "The first matching mapping is used, unmapped paths are used as they are.",
        item_type=DictConfigEntry,
        key_path=[
            NODE_MAIN,
            NODE_IMMICH,
            NODE_PATH_MAPPINGS
        ],
        required=False,
        default=[],
        example=[{"immich": "/mnt/photos", "local": "/data/photos"}]
    )

    IMMICH_PUBLIC_URL = StringConfigEntry(
        description="URL of the Immich server as reachable from the browser, used for the \"open in Immich\" "
                    "links in the web UI. Falls back to immich.url.",
        key_path=[
            NODE_MAIN,
            NODE_IMMICH,
            NODE_PUBLIC_URL
        ],
        required=False,
        default=None,
        example="https://immich.example.com"
    )

    IMMICH_RESCAN_AFTER_CHANGES = BoolConfigEntry(
        description="Queue a scan of the Immich external libraries after files were really moved or deleted "
                    "(at most once per run / batch of executed decisions), so Immich notices the change. "
                    "Needs an API key of an Immich admin with the library.read and library.update permissions.",
        key_path=[
            NODE_MAIN,
            NODE_IMMICH,
            NODE_RESCAN_AFTER_CHANGES
        ],
        required=False,
        default=False
    )

    MATCHING_BACKEND = StringConfigEntry(
        description="Which matcher finds duplicates: 'image-match' (image-match + Elasticsearch, default), "
                    "'hashes' (the hash pipeline: SHA-256, pixel hash, pHash/dHash, no Elasticsearch needed) "
                    "or 'both': image-match decides as before, the hash pipeline additionally runs on full runs "
                    "and only records its groups (never acted on) to compare both in the web UI (/compare).",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_BACKEND
        ],
        regex=f"^({'|'.join(MATCHING_BACKENDS)})$",
        default=MATCHING_BACKEND_IMAGE_MATCH,
        required=True
    )

    MATCHING_STORE_URL = StringConfigEntry(
        description="Where the hash pipeline stores its signatures: a SQLite file path, sqlite:////absolute/path.db "
                    "or postgresql://user:password@host:5432/db (needs the 'postgres' extra). "
                    "If not set, signatures.db next to the state database is used (a separate file).",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_STORE_URL
        ],
        required=False,
        default=None,
        # may contain a database password
        secret=True,
        example="sqlite:////data/state/signatures.db"
    )

    MATCHING_MAX_PHASH_DISTANCE = IntConfigEntry(
        description="Hash pipeline: maximum Hamming distance (of 64 bits) of the pHash of two near-duplicates.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_MAX_PHASH_DISTANCE
        ],
        range=Range(0, 64),
        default=_DEFAULT_THRESHOLDS.max_phash_distance
    )

    MATCHING_MAX_DHASH_DISTANCE = IntConfigEntry(
        description="Hash pipeline: maximum Hamming distance (of 64 bits) of the dHash of two near-duplicates "
                    "(both the pHash and the dHash distance must be within their limit).",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_MAX_DHASH_DISTANCE
        ],
        range=Range(0, 64),
        default=_DEFAULT_THRESHOLDS.max_dhash_distance
    )

    MATCHING_BURST_WINDOW_SECONDS = FloatConfigEntry(
        description="Hash pipeline: files of the same camera whose capture times are at most this many seconds "
                    "apart (but not equal) are burst/series shots: they still match, with a lower confidence, "
                    "and their group always goes to review.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_BURST_WINDOW_SECONDS
        ],
        default=_DEFAULT_THRESHOLDS.burst_window_seconds
    )

    MATCHING_BURST_CONFIDENCE_PENALTY = FloatConfigEntry(
        description="Hash pipeline: confidence subtracted from a match between burst shots.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_BURST_CONFIDENCE_PENALTY
        ],
        default=_DEFAULT_THRESHOLDS.burst_confidence_penalty
    )

    MATCHING_AUTO_ACT_MIN_CONFIDENCE = FloatConfigEntry(
        description="Hash pipeline: groups with a lower confidence (of their weakest match) are never acted on "
                    "automatically, they wait for a decision in the web UI.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_AUTO_ACT_MIN_CONFIDENCE
        ],
        default=_DEFAULT_THRESHOLDS.auto_act_min_confidence
    )

    MATCHING_COLOUR_EDIT_TOLERANCE = FloatConfigEntry(
        description="Hash pipeline: largest difference of a colour channel mean or standard deviation (0..255) "
                    "that still counts as the same colours. Above it a match is a colour-edited variant (review).",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_COLOUR_EDIT_TOLERANCE
        ],
        default=_DEFAULT_THRESHOLDS.colour_edit_tolerance
    )

    MATCHING_ASPECT_RATIO_TOLERANCE = FloatConfigEntry(
        description="Hash pipeline: relative aspect ratio difference above which a match is flagged as cropped.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_ASPECT_RATIO_TOLERANCE
        ],
        default=_DEFAULT_THRESHOLDS.aspect_ratio_tolerance
    )

    MATCHING_MATCH_ROTATIONS = BoolConfigEntry(
        description="Hash pipeline: also match copies that were rotated/mirrored without an orientation tag "
                    "(compares all 8 orientations, about 7x search time).",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_MATCH_ROTATIONS
        ],
        default=_DEFAULT_THRESHOLDS.match_rotations
    )

    MATCHING_EMBEDDINGS_ENABLED = BoolConfigEntry(
        description="Hash pipeline: opt-in crop detection with SSCD embeddings (needs the 'embeddings' extra, "
                    "onnxruntime). Crops and their source always go to review.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_EMBEDDINGS,
            NODE_ENABLED
        ],
        default=False
    )

    MATCHING_EMBEDDINGS_MODEL_PATH = StringConfigEntry(
        description="ONNX model file of the crop detection (f.ex. exported with tools/export_sscd_onnx.py). "
                    "If not set, the model is downloaded from model_url (or the default SSCD model) into "
                    "cache_directory.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_EMBEDDINGS,
            NODE_MODEL_PATH
        ],
        required=False,
        default=None,
        example="/data/models/sscd_disc_mixup.onnx"
    )

    MATCHING_EMBEDDINGS_MODEL_URL = StringConfigEntry(
        description="Where to download the ONNX model from if model_path is not set (requires model_sha256). "
                    "If neither is set, a pinned ONNX export of SSCD disc_mixup is downloaded.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_EMBEDDINGS,
            NODE_MODEL_URL
        ],
        required=False,
        default=None
    )

    MATCHING_EMBEDDINGS_MODEL_SHA256 = StringConfigEntry(
        description="SHA-256 of the model file, verified before loading (required with model_url, optional "
                    "with model_path).",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_EMBEDDINGS,
            NODE_MODEL_SHA256
        ],
        regex="^[0-9a-fA-F]{64}$",
        required=False,
        default=None
    )

    MATCHING_EMBEDDINGS_CACHE_DIRECTORY = StringConfigEntry(
        description="Directory for the downloaded model and the embedding cache (embeddings.db). "
                    "If not set, an 'embeddings' directory next to the state database is used.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_EMBEDDINGS,
            NODE_CACHE_DIRECTORY
        ],
        required=False,
        default=None,
        example="/data/state/embeddings"
    )

    MATCHING_EMBEDDINGS_SIMILARITY_THRESHOLD = FloatConfigEntry(
        description="Minimum cosine similarity [-1..1] of two embeddings to be considered a crop/copy.",
        key_path=[
            NODE_MAIN,
            NODE_MATCHING,
            NODE_EMBEDDINGS,
            NODE_SIMILARITY_THRESHOLD
        ],
        default=DEFAULT_MIN_SIMILARITY
    )

    @property
    def matching_backend(self) -> str:
        """
        :return: one of MATCHING_BACKENDS
        """
        return self.MATCHING_BACKEND.value or MATCHING_BACKEND_IMAGE_MATCH

    @property
    def uses_image_match(self) -> bool:
        """
        :return: True if image-match + Elasticsearch runs (backend image-match or both)
        """
        return self.matching_backend in (MATCHING_BACKEND_IMAGE_MATCH, MATCHING_BACKEND_BOTH)

    @property
    def uses_hashes(self) -> bool:
        """
        :return: True if the hash pipeline runs (backend hashes or both)
        """
        return self.matching_backend in (MATCHING_BACKEND_HASHES, MATCHING_BACKEND_BOTH)

    @property
    def acting_matcher(self) -> str:
        """
        :return: the matcher whose groups may be acted on (by the rules and by user decisions).
                 Groups of any other matcher are record-only.
        """
        return MATCHER_HASHES if self.matching_backend == MATCHING_BACKEND_HASHES else MATCHER_IMAGE_MATCH

    @property
    def state_directory(self) -> Path:
        """
        :return: the directory of the state database
        """
        return Path(self.STATE_DATABASE_PATH.value).expanduser().parent

    @property
    def signature_store_url(self) -> str:
        """
        :return: matching.store_url, default: signatures.db next to the state database
        """
        return self.MATCHING_STORE_URL.value or str(self.state_directory / "signatures.db")

    @property
    def embeddings_cache_directory(self) -> Path:
        """
        :return: matching.embeddings.cache_directory, default: "embeddings" next to the state database
        """
        configured = self.MATCHING_EMBEDDINGS_CACHE_DIRECTORY.value
        return Path(configured).expanduser() if configured else self.state_directory / "embeddings"

    def matching_thresholds(self) -> MatchingThresholds:
        """
        :return: thresholds of the hash pipeline from the matching.* configuration
        """
        return MatchingThresholds(
            max_phash_distance=self.MATCHING_MAX_PHASH_DISTANCE.value,
            max_dhash_distance=self.MATCHING_MAX_DHASH_DISTANCE.value,
            burst_window_seconds=self.MATCHING_BURST_WINDOW_SECONDS.value,
            burst_confidence_penalty=self.MATCHING_BURST_CONFIDENCE_PENALTY.value,
            aspect_ratio_tolerance=self.MATCHING_ASPECT_RATIO_TOLERANCE.value,
            colour_edit_tolerance=self.MATCHING_COLOUR_EDIT_TOLERANCE.value,
            match_rotations=self.MATCHING_MATCH_ROTATIONS.value,
            auto_act_min_confidence=self.MATCHING_AUTO_ACT_MIN_CONFIDENCE.value,
        )

    @property
    def user_decisions_dry_run(self) -> bool:
        """
        :return: whether decisions made by the user are only simulated
        """
        value = self.DRY_RUN_USER_DECISIONS.value
        return self.DRY_RUN.value if value is None else value
