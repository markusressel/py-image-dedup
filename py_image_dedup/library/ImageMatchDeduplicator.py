import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import click
from tqdm import tqdm

from py_image_dedup.library.DeduplicationResult import DeduplicationResult
from py_image_dedup.library.DeduplicatorConfig import DeduplicatorConfig, ConfigParam
from py_image_dedup.persistence import ImageSignatureStore
from py_image_dedup.persistence.MetadataKey import MetadataKey
from py_image_dedup.util import FileUtils, echo


class ImageMatchDeduplicator:
    EXECUTOR = ThreadPoolExecutor()

    _config: DeduplicatorConfig = DeduplicatorConfig()
    _directories: [str] = []
    _directory_map = {}
    _progress_bar: tqdm = None
    _processed_files: dict = {}
    _deduplication_result: DeduplicationResult = None

    def __init__(self, image_signature_store: ImageSignatureStore):
        """
        :param image_signature_store: persistent storage for image signatures and other metadata
        """
        self._persistence: ImageSignatureStore = image_signature_store

    def deduplicate(self, directories: [str],
                    config: DeduplicatorConfig = None,
                    threads: int = 1,
                    dry_run: bool = True,
                    skip_analyze_phase: bool = False) -> DeduplicationResult:
        """
        Removes duplicates

        :param directories: list of directories to process
        :param config: deduplication configuration
        :param threads: number of threads to use for concurrent processing
        :param dry_run: If true, no files will actually be removed.
                        Note though that the signature store will be written even when using this option
                        as the search wouldn't work without it.
        :param skip_analyze_phase: useful if you already did a dry run and want to do a real run afterwards
        :return: result of the operation
        """

        # see: https://stackoverflow.com/questions/14861891/runtimewarning-invalid-value-encountered-in-divide
        # and: https://stackoverflow.com/questions/29347987/why-cant-i-suppress-numpy-warnings
        import numpy
        numpy.warnings.filterwarnings('ignore')

        self._deduplication_result = DeduplicationResult()
        if config:
            self._config: DeduplicatorConfig = config

        for directory in directories:
            if not os.path.exists(directory):
                echo("Missing directory will be ignored: '%s'" % directory)
                continue
            if not os.path.isdir(directory):
                echo("Directory path is not a directory and will be ignored: '%s'" % directory)
                continue
            else:
                self._directories.append(directory)

        if len(self._directories) <= 0:
            echo("No root directories to scan", color='yellow')
            return self._deduplication_result

        if dry_run:
            echo("DRY RUN! No files or folders will actually be deleted!", color='yellow')

        echo("Phase 1/6: Cleaning up database ...", color='cyan')
        self._cleanup_database()

        echo("Phase 2/6: Counting files ...", color='cyan')
        self._count_files()

        phase_3_text = "Phase 3/6: Analyzing files"
        if skip_analyze_phase:
            echo(phase_3_text + " - Skipping", color='yellow')
        else:
            echo(phase_3_text, color='cyan')
            self._analyze(threads)

        echo("Phase 4/6: Finding duplicate files ...", color='cyan')
        self._processed_files = {}
        for directory, file_count in self._directory_map.items():
            echo("Finding duplicates in '%s' ..." % directory)
            with self._create_file_progressbar(file_count):
                self.__walk_directory_files(
                    root_directory=directory,
                    threads=1,
                    command=lambda root_dir, file_dir, file_path: self.__find_duplicates(
                        root_dir,
                        file_dir,
                        file_path))

        echo("Phase 5/6: Removing duplicates ...", color='cyan')
        self._remove_files_marked_as_delete(dry_run)

        echo("Phase 6/6: Removing empty folders ...", color='cyan')
        self._remove_empty_folders(dry_run)

        return self._deduplication_result

    def _analyze(self, threads: int = 1) -> {str, str}:
        """
        Analyzes all files, generates identifiers (if necessary) and stores them for later access

        :return: file_path -> identifier
        """

        for directory, file_count in self._directory_map.items():
            echo("Analyzing files in '%s' ..." % directory)
            with self._create_file_progressbar(file_count):
                self.__walk_directory_files(
                    root_directory=directory,
                    threads=threads,
                    command=lambda root_dir, file_dir, file_path: self.__analyze_file(file_path))

    def _cleanup_database(self):
        """
        Removes database entries of files that don't exist on disk
        Note that this cleanup will only consider files within one
        of the root directories specified in constructor, as other file paths
        might have been added on other machines.
        """

        # TODO: This iterates through all db entries - even the ones we are ignoring.
        # The db query should be improved to speed this up

        entries = self._persistence.get_all()
        for entry in entries:
            image_entry = entry['_source']
            metadata = image_entry[MetadataKey.METADATA.value]

            file_path = image_entry[MetadataKey.PATH.value]

            if MetadataKey.DATAMODEL_VERSION.value not in metadata:
                echo("Removing db entry with missing db model version number: %s" % file_path)
                self._persistence.remove(file_path)
                continue

            data_version = metadata[MetadataKey.DATAMODEL_VERSION.value]
            if data_version != self._persistence.DATAMODEL_VERSION:
                echo("Removing db entry with old db model version: %s" % file_path)
                self._persistence.remove(file_path)
                continue

            # filter by files in at least one of the specified root directories
            # this is necessary because the database might hold items for other paths already
            # and those are not interesting to us
            if not file_path.startswith(tuple(self._directories)):
                continue

            if not os.path.exists(file_path):
                echo("Removing db entry for missing file: %s" % file_path)
                self._persistence.remove(file_path)

    def _remove_empty_folders(self, dry_run: bool):
        """
        Searches for empty folders and removes them
        """

        # remove empty folders
        for directory in self._directories:
            empty_folders = self._find_empty_folders(directory)

            self._remove_folders(directory, empty_folders, dry_run)
            # self._increment_progress(increase_count_by=1)

    def _count_files(self):
        """
        Counts the amount of files to analyze (used in progress) and stores them in a map
        """

        self._directory_map = {}

        with self._create_folder_progressbar(len(self._directories)):
            for directory in self._directories:
                self._progress_bar.set_postfix_str(self._truncate_middle(directory))

                file_count = self._get_files_count(directory)
                self._directory_map[directory] = file_count

                self._increment_progress()

    def __walk_directory_files(self, root_directory: str, command, threads: int):
        """
        Walks through the files of the given directory

        :param root_directory: the directory to start with
        :param command: the method to execute for every file found
        :return: file_path -> identifier
        """

        # to avoid ascii char problems
        root_directory = str(root_directory)

        with ThreadPoolExecutor(threads) as self.EXECUTOR:
            for (root, dirs, files) in os.walk(root_directory):
                # root is the place you're listing
                # dirs is a list of directories directly under root
                # files is a list of files directly under root

                for file in files:
                    file_path = os.path.abspath(os.path.join(root, file))

                    # skip file with unwanted file extension
                    if not self.__file_extension_matches_filter(file):
                        continue

                    # skip if not existent (probably already deleted)
                    if not os.path.exists(file_path):
                        continue

                    try:
                        self.EXECUTOR.submit(command, root_directory, root, file_path)
                    except Exception as e:
                        click.echo(str(e), err=True)
                        sys.exit(-1)

                if not self._config[ConfigParam.RECURSIVE]:
                    return

    def __file_extension_matches_filter(self, file: str) -> bool:
        """
        Checks if a file matches the filter set for this deduplicator
        :param file: the file to check
        :return: true if it matches, false otherwise
        """
        if not self._config[ConfigParam.FILE_EXTENSION_FILTER]:
            return True

        filename, file_extension = os.path.splitext(file)

        if file_extension.lower() not in (ext.lower() for ext in self._config[ConfigParam.FILE_EXTENSION_FILTER]):
            # skip file with unwanted file extension
            return False
        else:
            return True

    def __analyze_file(self, file_path: str):
        """
        Analyzes a single file
        :param file_path: the file path
        """
        self._progress_bar.set_postfix_str(self._truncate_middle(file_path))

        self._persistence.add(file_path)

        self._increment_progress()

    def __find_duplicates(self, root_directory: str, file_directory: str, reference_file_path: str):
        """
        Removes all duplicates of the specified file
        :param reference_file_path: the file to check for duplicates
        """

        self._increment_progress()

        self._progress_bar.set_postfix_str(self._truncate_middle(reference_file_path))

        # remember processed files to prevent processing files in multiple directions
        if reference_file_path in self._processed_files:
            # already found a better candidate for this file
            return

        duplicate_candidates = self._persistence.find_similar(reference_file_path)

        if self._config[ConfigParam.SEARCH_ACROSS_ROOT_DIRS]:
            # filter by files in at least one of the specified root directories
            # this is necessary because the database might hold items for other paths already
            # and those are not interesting to us
            duplicate_candidates = [x for x in duplicate_candidates if
                                    x[MetadataKey.PATH.value].startswith(tuple(self._directories))]
        else:
            # filter by files in the same root directory
            duplicate_candidates = [x for x in duplicate_candidates if
                                    x[MetadataKey.PATH.value].startswith(root_directory)]

        if len(duplicate_candidates) <= 1:
            for candidate in duplicate_candidates:
                candidate_path = candidate[MetadataKey.PATH.value]

                if candidate_path != reference_file_path:
                    echo("Unexpected single candidate!", color='yellow')

                self._processed_files[candidate_path] = True

                # nothing to do here since the result is unique
                return

        # sort by quality criteria and redo the search to use the best candidate as the reference image
        sorted_duplicate_candidates = self._sort_by_quality_descending(duplicate_candidates)
        new_reference_file_path = sorted_duplicate_candidates[0][MetadataKey.PATH.value]
        duplicate_candidates = self._persistence.find_similar(new_reference_file_path)

        candidates_to_keep, candidates_to_delete = self._select_images_to_delete(duplicate_candidates)
        self._save_duplicates_for_result(candidates_to_keep[0], candidates_to_delete)
        for candidate in candidates_to_delete:
            candidate_path = candidate[MetadataKey.PATH.value]
            self._deduplication_result.add_removed_file(candidate_path)

    def _save_duplicates_for_result(self, file_to_keep: dict, duplicates_to_remove: dict) -> None:
        """
        Saves the comparison result for the final summary

        :param file_to_keep: the best image that shall be kept
        :param duplicates_to_remove: less good duplicates
        """
        self._deduplication_result.set_file_duplicates(file_to_keep, duplicates_to_remove)

    def _select_images_to_delete(self, duplicate_candidates: [{}]) -> tuple:
        """
        Selects which image to keep and wich to remove
        :return: tuple (image to keep, list of images to remove)
        """

        duplicate_candidates = self._sort_by_quality_descending(duplicate_candidates)

        keep_unfitting = []
        max_mod_time_diff = self._config[ConfigParam.MAX_FILE_MODIFICATION_TIME_DIFF]
        if max_mod_time_diff is not None:
            # filter files that don't match max mod time diff criteria
            best_candidate = duplicate_candidates[0]
            best_match_mod_time = best_candidate[MetadataKey.METADATA.value][MetadataKey.FILE_MODIFICATION_DATE.value]

            keep_unfitting = [c for c in duplicate_candidates if not math.fabs(
                c[MetadataKey.METADATA.value][
                    MetadataKey.FILE_MODIFICATION_DATE.value] - best_match_mod_time) <= max_mod_time_diff]

        # remember that we have processed these files
        for candidate in duplicate_candidates:
            self._processed_files[candidate[MetadataKey.PATH.value]] = True

        # keep first and mark others for removal
        return [duplicate_candidates[0]] + keep_unfitting, duplicate_candidates[1:]

    def _sort_by_quality_descending(self, duplicate_candidates) -> []:
        """
        Sorts images according to the desired priorities.
        The first item in the list will be the most preferred one of all found duplicates.

        :param duplicate_candidates: the images to analyze
        :return: duplicate candidates sorted by given criteria
        """

        duplicate_candidates = sorted(duplicate_candidates, key=lambda candidate: (

            # reverse, bigger is better
            candidate[MetadataKey.METADATA.value][MetadataKey.FILE_SIZE.value] * -1,

            # reverse, bigger (later time) is better
            candidate[MetadataKey.METADATA.value][MetadataKey.FILE_MODIFICATION_DATE.value] * -1,

            # more exif data is better
            len(candidate[MetadataKey.METADATA.value][MetadataKey.EXIF_DATA.value]) * -1,

            # higher pixel count is better
            candidate[MetadataKey.METADATA.value][MetadataKey.PIXELCOUNT.value] * -1,

            # smaller distance is better
            candidate[MetadataKey.DISTANCE.value],

            # if the filename contains "copy" it is less good
            "copy" in FileUtils.get_file_name(candidate[MetadataKey.PATH.value]).lower(),

            # longer filename is better (for "edited" versions)
            len(FileUtils.get_file_name(candidate[MetadataKey.PATH.value])) * -1,

            # shorter folder path is better
            len(FileUtils.get_containing_folder(candidate[MetadataKey.PATH.value])),

            # reverse, bigger is better
            candidate[MetadataKey.SCORE.value] * -1,

            # just to assure the order in the result is the same
            # if all other criteria (above) are equal
            # and recurring runs will result in the same order
            # (although they shouldn't be compared twice to begin with)
            candidate[MetadataKey.PATH.value],

        ))

        return duplicate_candidates

    def _find_empty_folders(self, root_path: str) -> [str]:
        """
        Function to remove empty folders
        :param root_path: folder to search in
        """

        result = []

        for root, directories, files in os.walk(root_path):
            if len(files) == 0 and len(directories) == 0:
                # check if a parent directory is already added
                if len([directory for directory in result if directory.startswith(root)]) == 0:
                    result.append(root)

            if not self._config[ConfigParam.RECURSIVE]:
                break

        return result

    def _remove_folders(self, root_path: str, folders: [str], dry_run: bool):
        """
        Function to remove empty folders
        :param root_path:
        """

        echo("Removing empty folders in: '%s' ..." % root_path)

        if len(folders) == 0:
            return

        with self._create_folder_progressbar(len(folders)):
            for folder in folders:
                self._progress_bar.set_postfix_str(self._truncate_middle(folder))

                if not dry_run:
                    os.rmdir(folder)

                self._deduplication_result.add_removed_empty_folder(folder)
                self._increment_progress()

    def _get_files_count(self, directory: str) -> int:
        """
        :param directory: the directory to analyze
        :return: number of files in the given directory that match the currently set file filter
        """

        files_count = 0
        for r, d, files in os.walk(directory):
            for file in files:
                if self.__file_extension_matches_filter(file):
                    files_count += 1
            if not self._config[ConfigParam.RECURSIVE]:
                break

        return files_count

    def _increment_progress(self, increase_count_by: int = 1):
        """
        Increases the current progress bar
        :param increase_count_by: amount to increase
        """
        self._progress_bar.update(n=increase_count_by)

    def _create_file_progressbar(self, total_file_count: int) -> tqdm:
        self._create_progressbar(total_file_count, "Files")
        return self._progress_bar

    def _create_folder_progressbar(self, total_folder_count: int) -> tqdm:
        self._create_progressbar(total_folder_count, "Folder")
        return self._progress_bar

    def _create_progressbar(self, total_count: int, unit: str) -> tqdm:
        """
        Creates a new progress bar
        :param total_count: target for 100%
        :param unit: "Things" that are counted
        :return: progress bar
        """
        self._progress_bar = tqdm(total=total_count, unit=unit, unit_scale=True, mininterval=1)
        return self._progress_bar

    def _remove_files_marked_as_delete(self, dry_run: bool):
        """
        Removes files that were marked to be deleted in previous deduplication step
        """

        marked_files_count = len(self._deduplication_result.get_removed_files())
        if marked_files_count == 0:
            return

        with self._create_file_progressbar(total_file_count=marked_files_count):
            self._delete_files(self._deduplication_result.get_removed_files(), dry_run)

    def _delete_files(self, files_to_delete: [str], dry_run: bool):
        """
        Deletes files on disk
        :param files_to_delete: list of absolute file paths
        """

        for file in files_to_delete:
            self._progress_bar.set_postfix_str(self._truncate_middle(file))

            # remove the smaller/equal sized and/or older/equally old file
            if dry_run:
                pass
            else:
                # remove from file system
                if os.path.exists(file):
                    os.remove(file)

                # remove from persistence
                self._persistence.remove(file)

            self._deduplication_result.add_removed_file(file)

            self._increment_progress()

    @staticmethod
    def _truncate_middle(text: str, max_length: int = 50):
        if len(text) <= max_length:
            # string is already short-enough, fill up with spaces
            return text + ((max_length - len(text)) * " ")
        # half of the size, minus the 3 .'s
        n_2 = int(max_length / 2) - 3
        # whatever's left
        n_1 = max_length - n_2 - 3
        return '{0}...{1}'.format(text[:n_1], text[-n_2:])
