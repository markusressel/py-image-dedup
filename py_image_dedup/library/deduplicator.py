import datetime
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import click
from ordered_set import OrderedSet
from tqdm import tqdm

from py_image_dedup import util
from py_image_dedup.config.deduplicator_config import DeduplicatorConfig
from py_image_dedup.library import ActionEnum
from py_image_dedup.library.deduplication_result import DeduplicationResult
from py_image_dedup.persistence import ImageSignatureStore
from py_image_dedup.persistence.elasticsearchstorebackend import ElasticSearchStoreBackend
from py_image_dedup.persistence.metadata_key import MetadataKey
from py_image_dedup.util import file, echo
from py_image_dedup.util.file import file_has_extension, get_files_count


class ImageMatchDeduplicator:
    EXECUTOR = ThreadPoolExecutor()

    _config: DeduplicatorConfig = DeduplicatorConfig()
    _progress_bar: tqdm = None
    _processed_files: dict = {}
    _deduplication_result: DeduplicationResult = None

    def __init__(self, config: DeduplicatorConfig):
        """
        :param config: configuration
        """
        self._config = config
        self._persistence: ImageSignatureStore = ElasticSearchStoreBackend(
            host=config.ELASTICSEARCH_HOST.value,
            use_exif_data=config.ANALYSIS_USE_EXIF_DATA.value,
            max_dist=config.ELASTICSEARCH_MAX_DISTANCE.value,
            setup_database=config.ELASTICSEARCH_AUTO_CREATE_INDEX.value
        )

    def reset_result(self):
        self._deduplication_result = DeduplicationResult()
        self._processed_files = {}

    def analyse_all(self):
        """
        Runs the analysis phase independently.
        """
        directories = self._config.SOURCE_DIRECTORIES.value

        echo("Phase 1/2: Counting files ...", color='cyan')
        directory_map = self._count_files(directories)

        echo("Phase 2/2: Analyzing files ...", color='cyan')
        self.analyze_directories(directory_map)

    def deduplicate_all(self, skip_analyze_phase: bool = False) -> DeduplicationResult:
        """
        Runs the full 6 deduplication phases.
        :param skip_analyze_phase: useful if you already did a dry run and want to do a real run afterwards
        :return: result of the operation
        """
        # see: https://stackoverflow.com/questions/14861891/runtimewarning-invalid-value-encountered-in-divide
        # and: https://stackoverflow.com/questions/29347987/why-cant-i-suppress-numpy-warnings
        import numpy
        numpy.warnings.filterwarnings('ignore')

        directories = self._config.SOURCE_DIRECTORIES.value
        if len(directories) <= 0:
            raise ValueError("No root directories to scan")

        if self._config.DRY_RUN.value:
            echo("==> DRY RUN! No files or folders will actually be deleted! <==", color='yellow')

        echo("Phase 1/6: Cleaning up database ...", color='cyan')
        self._cleanup_database(directories)

        echo("Phase 2/6: Counting files ...", color='cyan')
        directory_map = self._count_files(directories)

        phase_3_text = "Phase 3/6: Analyzing files"
        if skip_analyze_phase:
            echo(phase_3_text + " - Skipping", color='yellow')
        else:
            echo(phase_3_text, color='cyan')
            self.analyze_directories(directory_map)

        echo("Phase 4/6: Finding duplicate files ...", color='cyan')
        self.find_duplicates_in_directories(directory_map)

        # Phase 5/6: Move or Delete duplicate files
        self.process_duplicates()

        self.remove_empty_folders()

        return self._deduplication_result

    def analyze_directories(self, directory_map: dict):
        """
        Analyzes all files, generates identifiers (if necessary) and stores them for later access
        """
        threads = self._config.ANALYSIS_THREADS.value

        # load truncated images too
        # TODO: this causes an infinite loop on some (truncated) images
        # ImageFile.LOAD_TRUNCATED_IMAGES = True

        for directory, file_count in directory_map.items():
            echo("Analyzing files in '%s' ..." % directory)
            with self._create_file_progressbar(file_count):
                self.__walk_directory_files(
                    root_directory=directory,
                    threads=threads,
                    command=lambda root_dir, file_dir, file_path: self.analyze_file(file_path))

    def find_duplicates_in_directories(self, directory_map: dict):
        """
        Finds duplicates in the given directories
        :param directory_map: map of directory path -> file count
        """
        self.reset_result()

        for directory, file_count in directory_map.items():
            echo("Finding duplicates in '%s' ..." % directory)
            with self._create_file_progressbar(file_count):
                self.__walk_directory_files(
                    root_directory=directory,
                    threads=1,  # there seems to be no performance advantage in using multiple threads here
                    command=lambda root_dir, _, file_path: self.find_duplicates_of_file(
                        self._config.SOURCE_DIRECTORIES.value,
                        root_dir,
                        file_path))

    def _cleanup_database(self, directories: []):
        """
        Removes database entries of files that don't exist on disk.
        Note that this cleanup will only consider files within one
        of the root directories specified in constructor, as other file paths
        might have been added on other machines.
        :param directories: directories in this run
        """
        # TODO: This iterates through all db entries - even the ones we are ignoring.
        # The db query should be improved to speed this up

        count, entries = self._persistence.get_all()
        if count <= 0:
            return

        with self._create_progressbar(count, unit="entries"):
            for entry in entries:
                try:
                    image_entry = entry['_source']
                    metadata = image_entry[MetadataKey.METADATA.value]

                    file_path = image_entry[MetadataKey.PATH.value]

                    self._progress_bar.set_postfix_str(self._truncate_middle(file_path))

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
                    if not file_path.startswith(tuple(directories)):
                        continue

                    if not os.path.exists(file_path):
                        echo("Removing db entry for missing file: %s" % file_path)
                        self._persistence.remove(file_path)

                finally:
                    self._increment_progress()

    def _remove_empty_folders(self, directories: [], recursive: bool):
        """
        Searches for empty folders and removes them
        :param directories: directories to scan
        """
        dry_run = self._config.DRY_RUN.value

        # remove empty folders
        for directory in directories:
            empty_folders = self._find_empty_folders(directory, recursive)
            self._remove_folders(directory, empty_folders, dry_run)

    def _count_files(self, directories: []) -> dict:
        """
        Counts the amount of files to analyze (used in progress) and stores them in a map
        :return map "directory path" -> "directory file count"
        """
        directory_map = {}

        with self._create_folder_progressbar(len(directories)):
            for directory in directories:
                self._progress_bar.set_postfix_str(self._truncate_middle(directory))

                file_count = get_files_count(
                    directory,
                    self._config.RECURSIVE.value,
                    self._config.FILE_EXTENSION_FILTER.value
                )
                directory_map[directory] = file_count

                self._increment_progress()

        return directory_map

    def __walk_directory_files(self, root_directory: str, command, threads: int):
        """
        Walks through the files of the given directory
        :param root_directory: the directory to start with
        :param command: the method to execute for every file found
        :return: file_path -> identifier
        """
        # to avoid ascii char problems
        root_directory = str(root_directory)

        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="py-image-dedup-walker") as self.EXECUTOR:
            for (root, dirs, files) in os.walk(root_directory):
                # root is the place you're listing
                # dirs is a list of directories directly under root
                # files is a list of files directly under root

                for file in files:
                    file_path = os.path.abspath(os.path.join(root, file))

                    # skip file with unwanted file extension
                    if not file_has_extension(file, self._config.FILE_EXTENSION_FILTER.value):
                        continue

                    # skip if not existent (probably already deleted)
                    if not os.path.exists(file_path):
                        continue

                    try:
                        self.EXECUTOR.submit(util.reraise_with_stack(command), root_directory, root, file_path)
                    except Exception as e:
                        click.echo(e, err=True)
                        sys.exit(1)

                if not self._config.RECURSIVE.value:
                    return

    def analyze_file(self, file_path: str):
        """
        Analyzes a single file
        :param file_path: the file path
        """
        self._progress_bar.set_postfix_str(self._truncate_middle(file_path))

        try:
            self._persistence.add(file_path)
        except Exception as e:
            logging.exception(e)
            echo("Error analyzing file '%s': %s" % (file_path, e))
        finally:
            self._increment_progress()

    def find_duplicates_of_file(self, root_directories: [str], root_directory: str, reference_file_path: str):
        """
        Finds duplicates and marks all but the best copy as "to-be-deleted".
        :param root_directories: valid root directories
        :param root_directory: root directory of reference_file_path
        :param reference_file_path: the file to check for duplicates
        """
        self._increment_progress()
        self._progress_bar.set_postfix_str(self._truncate_middle(reference_file_path))

        # remember processed files to prevent processing files in multiple directions
        if reference_file_path in self._processed_files:
            # already found a better candidate for this file
            return

        duplicate_candidates = self._persistence.find_similar(reference_file_path)

        if self._config.SEARCH_ACROSS_ROOT_DIRS.value:
            # filter by files in at least one of the specified root directories
            # this is necessary because the database might hold items for other paths already
            # and those are not interesting to us
            duplicate_candidates = [x for x in duplicate_candidates if
                                    x[MetadataKey.PATH.value].startswith(tuple(root_directories))]
        else:
            # filter by files in the same root directory
            duplicate_candidates = [x for x in duplicate_candidates if
                                    x[MetadataKey.PATH.value].startswith(root_directory)]

        if len(duplicate_candidates) <= 0:
            echo("No duplication candidates found in database for '%s'. "
                 "This is an indication that the file has not been analysed yet or "
                 "there was an issue analysing it." % reference_file_path,
                 color='yellow')

        if len(duplicate_candidates) <= 1:
            for candidate in duplicate_candidates:
                candidate_path = candidate[MetadataKey.PATH.value]

                if candidate_path != reference_file_path:
                    echo("Unexpected unique duplication candidate '%s' for "
                         "reference file '%s'" % (candidate_path, reference_file_path), color='yellow')

                self._processed_files[candidate_path] = True

            # nothing to do here since the result is unique
            return

        # sort by quality criteria and redo the search to use the best candidate as the reference image
        sorted_duplicate_candidates = self._sort_by_quality_descending(duplicate_candidates)
        new_reference_file_path = sorted_duplicate_candidates[0][MetadataKey.PATH.value]
        duplicate_candidates = self._persistence.find_similar(new_reference_file_path)

        candidates_to_keep, candidates_to_delete = self._select_images_to_delete(duplicate_candidates)
        self._save_duplicates_for_result(candidates_to_keep, candidates_to_delete)

    def _save_duplicates_for_result(self, files_to_keep: dict, duplicates: dict) -> None:
        """
        Saves the comparison result for the final summary

        :param files_to_keep: list of image that shall be kept
        :param duplicates: less good duplicates
        """
        self._deduplication_result.set_file_duplicates(files_to_keep, duplicates)

        for file_to_keep in files_to_keep:
            self._deduplication_result.add_file_action(file_to_keep[MetadataKey.PATH.value], ActionEnum.NONE)

        if self._config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value is None:
            action = ActionEnum.DELETE
        else:
            action = ActionEnum.MOVE
        for duplicate in duplicates:
            self._deduplication_result.add_file_action(duplicate[MetadataKey.PATH.value], action)

    def _select_images_to_delete(self, duplicate_candidates: [{}]) -> tuple:
        """
        Selects which image to keep and wich to remove
        :return: tuple (image to keep, list of images to remove)
        """
        duplicate_candidates = self._sort_by_quality_descending(duplicate_candidates)

        # keep first and mark others for removal
        keep = [duplicate_candidates[0]]
        dont_keep = duplicate_candidates[1:]

        # move files that don't fit criteria to "keep" list
        max_mod_time_diff = self._config.MAX_FILE_MODIFICATION_TIME_DELTA.value
        if max_mod_time_diff is not None:
            # filter files that don't match max mod time diff criteria
            best_candidate = keep[0]
            best_match_mod_timestamp = best_candidate[MetadataKey.METADATA.value][
                MetadataKey.FILE_MODIFICATION_DATE.value]

            for c in duplicate_candidates:
                c_timestamp = c[MetadataKey.METADATA.value][MetadataKey.FILE_MODIFICATION_DATE.value]
                timestamp_diff = abs(c_timestamp - best_match_mod_timestamp)
                timedelta = datetime.timedelta(seconds=timestamp_diff)
                if not timedelta <= max_mod_time_diff:
                    keep.append(c)
                    dont_keep.remove(c)

        # remember that we have processed these files
        for candidate in duplicate_candidates:
            self._processed_files[candidate[MetadataKey.PATH.value]] = True

        return keep, dont_keep

    @staticmethod
    def _sort_by_quality_descending(duplicate_candidates) -> []:
        """
        Sorts images according to the desired priorities.
        The first item in the list will be the most preferred one of all found duplicates.

        :param duplicate_candidates: the images to analyze
        :return: duplicate candidates sorted by given criteria
        """

        def sort_criteria(candidate: dict) -> ():
            criteria = []

            # higher pixel count is better
            criteria.append(candidate[MetadataKey.METADATA.value][MetadataKey.PIXELCOUNT.value] * -1)

            if MetadataKey.EXIF_DATA.value in candidate[MetadataKey.METADATA.value]:
                # more exif data is better
                criteria.append(len(candidate[MetadataKey.METADATA.value][MetadataKey.EXIF_DATA.value]) * -1)

            # reverse, bigger is better
            criteria.append(candidate[MetadataKey.METADATA.value][MetadataKey.FILE_SIZE.value] * -1)

            # reverse, bigger (later time) is better
            criteria.append(candidate[MetadataKey.METADATA.value][MetadataKey.FILE_MODIFICATION_DATE.value] * -1)

            # smaller distance is better
            criteria.append(candidate[MetadataKey.DISTANCE.value])

            # if the filename contains "copy" it is less good
            criteria.append("copy" in file.get_file_name(candidate[MetadataKey.PATH.value]).lower())

            # longer filename is better (for "edited" versions)
            criteria.append(len(file.get_file_name(candidate[MetadataKey.PATH.value])) * -1)

            # shorter folder path is better
            criteria.append(len(file.get_containing_folder(candidate[MetadataKey.PATH.value])))

            # reverse, bigger is better
            criteria.append(candidate[MetadataKey.SCORE.value] * -1)

            # just to assure the order in the result is the same
            # if all other criteria (above) are equal
            # and recurring runs will result in the same order
            # (although they shouldn't be compared twice to begin with)
            criteria.append(candidate[MetadataKey.PATH.value])

            return tuple(criteria)

        duplicate_candidates = sorted(duplicate_candidates, key=sort_criteria)

        return duplicate_candidates

    def process_duplicates(self):
        """
        Moves or removes duplicates based on the configuration
        """
        dry_run = self._config.DRY_RUN.value
        duplicate_target_directory = self._config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value
        if duplicate_target_directory:
            echo("Phase 5/6: Moving duplicates ...", color='cyan')
            self._move_files_marked_as_delete(duplicate_target_directory, dry_run)
        else:
            echo("Phase 5/6: Removing duplicates ...", color='cyan')
            self._remove_files_marked_as_delete(dry_run)

    def _find_empty_folders(self, root_path: str, recursive: bool) -> [str]:
        """
        Finds empty folders within the given root_path
        :param root_path: folder to search in
        """
        result = OrderedSet()

        # traverse bottom-up to remove folders that are empty due to file removal
        for root, directories, files in os.walk(root_path, topdown=False):
            abs_file_paths = list(map(lambda x: os.path.abspath(os.path.join(root, x)), files))
            abs_folder_paths = list(map(lambda x: os.path.abspath(os.path.join(root, x)), directories))

            files_deleted = list(
                filter(lambda x: x in self._deduplication_result.get_removed_or_moved_files(), abs_file_paths))
            filtered_files = list(
                filter(lambda x: x not in files_deleted, abs_file_paths))

            folders_deleted = list(filter(lambda x: x in result, abs_folder_paths))
            filtered_directories = list(filter(lambda x: x not in folders_deleted, abs_folder_paths))
            if (len(filtered_files) == 0 and (len(filtered_directories) == 0) and (
                    len(folders_deleted) > 0 or len(files_deleted) > 0)):
                # check if a parent directory is already added
                if len([directory for directory in filtered_directories if directory.startswith(root)]) == 0:
                    result.append(root)

            if not recursive:
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
        :param dry_run: set to true to simulate this action
        """
        items_to_remove = self._deduplication_result.get_file_with_action(ActionEnum.DELETE)
        marked_files_count = len(items_to_remove)
        if marked_files_count == 0:
            return

        with self._create_file_progressbar(total_file_count=marked_files_count):
            self._delete_files(items_to_remove, dry_run)

    def _move_files_marked_as_delete(self, target_dir: str, dry_run: bool):
        """
        Moves files that were marked to be deleted in previous deduplication step to the target directory
        :param target_dir: the directory to move duplicates to
        :param dry_run: set to true to simulate this action
        """
        items_to_move = self._deduplication_result.get_file_with_action(ActionEnum.MOVE)
        marked_files_count = len(items_to_move)
        if marked_files_count == 0:
            return

        with self._create_file_progressbar(total_file_count=marked_files_count):
            self._move_files(items_to_move, target_dir, dry_run)

    def _delete_files(self, files_to_delete: [str], dry_run: bool):
        """
        Deletes files on disk
        :param files_to_delete: list of absolute file paths
        :param dry_run: set to true to simulate this action
        """
        for file_path in files_to_delete:
            self._progress_bar.set_postfix_str(self._truncate_middle(file_path))

            if dry_run:
                pass
            else:
                # remove from file system
                if os.path.exists(file_path):
                    os.remove(file_path)

                # remove from persistence
                self._persistence.remove(file_path)

            self._increment_progress()

    def _move_files(self, files_to_move: [str], target_dir: str, dry_run: bool):
        """
        Moves files on disk
        :param files_to_move: list of absolute file paths
        :param target_dir: directory to move files to
        """

        for file_path in files_to_move:
            self._progress_bar.set_postfix_str(self._truncate_middle(file_path))

            try:
                if dry_run:
                    continue

                # move file
                if os.path.exists(file_path):
                    target_subdir = os.path.join(target_dir + file.get_containing_folder(file_path))
                    target_file_path = os.path.join(target_subdir, file.get_file_name(file_path))

                    if os.path.exists(target_file_path):
                        raise ValueError("Cant move duplicate file because the target already exists: {}".format(
                            target_file_path))

                    os.makedirs(target_subdir, exist_ok=True)
                    shutil.move(file_path, target_file_path)

                # remove from persistence
                self._persistence.remove(file_path)
            except Exception as ex:
                logging.exception(ex)
                # LOGGER.log(ex)
            finally:
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

    def remove_empty_folders(self):
        phase_6_text = "Phase 6/6: Removing empty folders"
        if not self._config.REMOVE_EMPTY_FOLDERS.value:
            echo(phase_6_text + " - Skipping", color='yellow')
        else:
            echo(phase_6_text, color='cyan')
            self._remove_empty_folders(self._config.SOURCE_DIRECTORIES.value, self._config.RECURSIVE.value)
