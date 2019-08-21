from enum import Enum


class ConfigParam(Enum):
    RECURSIVE = "RECURSIVE"
    SEARCH_ACROSS_ROOT_DIRS = "SEARCH_ACROSS_ROOT_DIRS"
    FILE_EXTENSION_FILTER = "FILE_EXTENSION_FILTER"
    MAX_FILE_MODIFICATION_TIME_DIFF = "MAX_FILE_MODIFICATION_TIME_DIFF"


class DeduplicatorConfig:

    def __init__(self, recursive: bool = True,
                 search_across_root_directories: bool = False,
                 file_extension_filter: [str] or None = None,
                 max_file_modification_time_diff: int or None = None):
        """
        Create a config

        :param recursive: also walk through sub-folders recursively
        :param search_across_root_directories: if set to true duplicates duplicates will also be searched for
               in other root directories specified in 'directories' parameter, otherwise root directories
               will be processed separately
        :param file_extension_filter: filter files for this extension (case insensitive)
        :param max_file_modification_time_diff:
        """

        self._entries = {
            ConfigParam.RECURSIVE: recursive,
            ConfigParam.SEARCH_ACROSS_ROOT_DIRS: search_across_root_directories,
            ConfigParam.FILE_EXTENSION_FILTER: file_extension_filter,
            ConfigParam.MAX_FILE_MODIFICATION_TIME_DIFF: max_file_modification_time_diff,
        }

    def __getitem__(self, key: ConfigParam):
        return self._entries[key]
