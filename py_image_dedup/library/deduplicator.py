import datetime
import filecmp
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import click
from ordered_set import OrderedSet

from py_image_dedup import util
from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.library import ActionEnum
from py_image_dedup.library.deduplication_result import DeduplicationResult
from py_image_dedup.library.progress_manager import ProgressManager
from py_image_dedup.persistence import ImageSignatureStore
from py_image_dedup.persistence.elasticsearchstorebackend import ElasticSearchStoreBackend
from py_image_dedup.persistence.metadata_key import MetadataKey
from py_image_dedup.stats import DUPLICATE_ACTION_MOVE_COUNT, DUPLICATE_ACTION_DELETE_COUNT, ANALYSIS_TIME, \
    FIND_DUPLICATES_TIME
from py_image_dedup.util import file, echo
from py_image_dedup.util.file import get_files_count, file_has_extension

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)


class ImageMatchDeduplicator:
    EXECUTOR = ThreadPoolExecutor()

    _config: DeduplicatorConfig
    _progress_manager: ProgressManager

    _processed_files: dict = {}
    _deduplication_result: DeduplicationResult = None

    def __init__(self, interactive: bool):
        """

        :param interactive: whether cli output should be interactive or not
        """
        self.interactive = interactive

        self._progress_manager = ProgressManager()
        self._config = DeduplicatorConfig()
        self._persistence: ImageSignatureStore = ElasticSearchStoreBackend(
            host=self._config.ELASTICSEARCH_HOST.value,
            port=self._config.ELASTICSEARCH_PORT.value,
            el_index=self._config.ELASTICSEARCH_INDEX.value,
            use_exif_data=self._config.ANALYSIS_USE_EXIF_DATA.value,
            max_dist=self._config.ELASTICSEARCH_MAX_DISTANCE.value,
            setup_database=self._config.ELASTICSEARCH_AUTO_CREATE_INDEX.value
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
        import warnings
        warnings.filterwarnings('ignore')

        directories = self._config.SOURCE_DIRECTORIES.value
        if len(directories) <= 0:
            raise ValueError("No root directories to scan")

        if self._config.DRY_RUN.value:
            echo("==> DRY RUN! No files or folders will actually be deleted! <==", color='yellow')

        echo("Phase 1/6: Cleaning up database ...", color='cyan')
        self.cleanup_database(directories)

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
            self._progress_manager.start(f"Analyzing files in '{directory}'", file_count, "Files", self.interactive)
            self.__walk_directory_files(
                root_directory=directory,
                threads=threads,
                command=lambda root_dir, file_dir, file_path: self.analyze_file(file_path))
            self._progress_manager.clear()

    def find_duplicates_in_directories(self, directory_map: dict):
        """
        Finds duplicates in the given directories
        :param directory_map: map of directory path -> file count
        """
        self.reset_result()

        for directory, file_count in directory_map.items():
            self._progress_manager.start(f"Finding duplicates in '{directory}' ...", file_count, "Files",
                                         self.interactive)
            self.__walk_directory_files(
                root_directory=directory,
                threads=1,  # there seems to be no performance advantage in using multiple threads here
                command=lambda root_dir, _, file_path: self.find_duplicates_of_file(
                    self._config.SOURCE_DIRECTORIES.value,
                    root_dir,
                    file_path))
            self._progress_manager.clear()

    def cleanup_database(self, directories: List[Path]):
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

        self._progress_manager.start(f"Cleanup database", count, "entries", self.interactive)
        for entry in entries:
            try:
                image_entry = entry['_source']
                metadata = image_entry[MetadataKey.METADATA.value]

                file_path = Path(image_entry[MetadataKey.PATH.value])
                self._progress_manager.set_postfix(self._truncate_middle(str(file_path)))

                if MetadataKey.DATAMODEL_VERSION.value not in metadata:
                    echo(f"Removing db entry with missing db model version number: {file_path}")
                    self._persistence.remove(str(file_path))
                    continue

                data_version = metadata[MetadataKey.DATAMODEL_VERSION.value]
                if data_version != self._persistence.DATAMODEL_VERSION:
                    echo(f"Removing db entry with old db model version: {file_path}")
                    self._persistence.remove(str(file_path))
                    continue

                # filter by files in at least one of the specified root directories
                # this is necessary because the database might hold items for other paths already
                # and those are not interesting to us
                if not any(root_dir in file_path.parents for root_dir in directories):
                    continue

                if not file_path.exists():
                    echo(f"Removing db entry for missing file: {file_path}")
                    self._persistence.remove(str(file_path))

            finally:
                self._progress_manager.inc()
        self._progress_manager.clear()

    def _remove_empty_folders(self, directories: List[Path], recursive: bool):
        """
        Searches for empty folders and removes them
        :param directories: directories to scan
        """
        dry_run = self._config.DRY_RUN.value

        # remove empty folders
        for directory in directories:
            empty_folders = self._find_empty_folders(directory, recursive, dry_run)
            self._remove_folders(directory, empty_folders, dry_run)

    def _count_files(self, directories: List[Path]) -> dict:
        """
        Counts the amount of files to analyze (used in progress) and stores them in a map
        :return map "directory path" -> "directory file count"
        """
        directory_map = {}

        self._progress_manager.start(f"Counting files", len(directories), "Dirs", self.interactive)
        for directory in directories:
            self._progress_manager.set_postfix(self._truncate_middle(directory))

            file_count = get_files_count(
                directory,
                self._config.RECURSIVE.value,
                self._config.FILE_EXTENSION_FILTER.value,
                self._config.EXCLUSIONS.value
            )
            directory_map[directory] = file_count

            self._progress_manager.inc()
        self._progress_manager.clear()

        return directory_map

    def __walk_directory_files(self, root_directory: Path, command, threads: int):
        """
        Walks through the files of the given directory
        :param root_directory: the directory to start with
        :param command: the method to execute for every file found
        :return: file_path -> identifier
        """
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="py-image-dedup-walker") as self.EXECUTOR:
            for (root, dirs, files) in os.walk(str(root_directory)):
                # root is the place you're listing
                # dirs is a list of directories directly under root
                # files is a list of files directly under root
                root = Path(root)

                for file in files:
                    file_path = Path(root, file)

                    # skip file in exclusion
                    if any(list(map(lambda x: x.search(str(file_path.absolute())), self._config.EXCLUSIONS.value))):
                        continue

                    # skip file with unwanted file extension
                    if not file_has_extension(file_path, self._config.FILE_EXTENSION_FILTER.value):
                        continue

                    # skip if not existent (probably already deleted)
                    if not file_path.exists():
                        self._progress_manager.inc()
                        continue

                    try:
                        self.EXECUTOR.submit(util.reraise_with_stack(command), root_directory, root, file_path)
                    except Exception as e:
                        click.echo(e, err=True)
                        sys.exit(1)

                if not self._config.RECURSIVE.value:
                    return

    @ANALYSIS_TIME.time()
    def analyze_file(self, file_path: Path):
        """
        Analyzes a single file
        :param file_path: the file path
        """
        self._progress_manager.set_postfix(self._truncate_middle(file_path))

        try:
            self._persistence.add(str(file_path))
        except Exception as e:
            logging.exception(e)
            echo(f"Error analyzing file '{file_path}': {e}")
        finally:
            self._progress_manager.inc()

    @FIND_DUPLICATES_TIME.time()
    def find_duplicates_of_file(self, root_directories: List[Path], root_directory: Path, reference_file_path: Path):
        """
        Finds duplicates and marks all but the best copy as "to-be-deleted".
        :param root_directories: valid root directories
        :param root_directory: root directory of reference_file_path
        :param reference_file_path: the file to check for duplicates
        """
        self._progress_manager.inc()
        self._progress_manager.set_postfix(self._truncate_middle(reference_file_path))

        # remember processed files to prevent processing files in multiple directions
        if reference_file_path in self._processed_files:
            # already found a better candidate for this file
            return

        duplicate_candidates = self._persistence.find_similar(str(reference_file_path))

        if self._config.SEARCH_ACROSS_ROOT_DIRS.value:
            # filter by files in at least one of the specified root directories
            # this is necessary because the database might hold items for other paths already
            # and those are not interesting to us
            duplicate_candidates = [
                candidate for candidate in duplicate_candidates if
                any(root_dir in Path(candidate[MetadataKey.PATH.value]).parents for root_dir in root_directories)
            ]
        else:
            # filter by files in the same root directory
            duplicate_candidates = [
                candidate for candidate in duplicate_candidates if
                root_directory in Path(candidate[MetadataKey.PATH.value]).parents
            ]

        if len(duplicate_candidates) <= 0:
            echo(f"No duplication candidates found in database for '{reference_file_path}'. "
                 "This is an indication that the file has not been analysed yet or "
                 "there was an issue analysing it.",
                 color='yellow')

        if len(duplicate_candidates) <= 1:
            for candidate in duplicate_candidates:
                candidate_path = Path(candidate[MetadataKey.PATH.value])

                if candidate_path != reference_file_path:
                    echo(f"Unexpected unique duplication candidate '{candidate_path}' for "
                         f"reference file '{reference_file_path}'", color='yellow')

                self._processed_files[candidate_path] = True

            # nothing to do here since the result is unique
            return

        # sort by quality criteria and redo the search to use the best candidate as the reference image
        sorted_duplicate_candidates = self._sort_by_quality_descending(duplicate_candidates)
        new_reference_file_path = sorted_duplicate_candidates[0][MetadataKey.PATH.value]
        duplicate_candidates = self._persistence.find_similar(new_reference_file_path)

        candidates_to_keep, candidates_to_delete = self._select_images_to_delete(duplicate_candidates)
        self._save_duplicates_for_result(candidates_to_keep, candidates_to_delete)

    def _save_duplicates_for_result(self, files_to_keep: List[dict], duplicates: List[dict]) -> None:
        """
        Saves the comparison result for the final summary

        :param files_to_keep: list of image that shall be kept
        :param duplicates: less good duplicates
        """
        self._deduplication_result.set_file_duplicates(files_to_keep, duplicates)

        for file_to_keep in files_to_keep:
            file_path = Path(file_to_keep[MetadataKey.PATH.value])
            self._deduplication_result.add_file_action(file_path, ActionEnum.NONE)

        if self._config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value is None:
            action = ActionEnum.DELETE
        else:
            action = ActionEnum.MOVE
        for duplicate in duplicates:
            file_path = Path(duplicate[MetadataKey.PATH.value])
            self._deduplication_result.add_file_action(file_path, action)

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

            for c in dont_keep:
                c_timestamp = c[MetadataKey.METADATA.value][MetadataKey.FILE_MODIFICATION_DATE.value]
                timestamp_diff = abs(c_timestamp - best_match_mod_timestamp)
                difference = datetime.timedelta(seconds=timestamp_diff)
                if difference > max_mod_time_diff:
                    keep.append(c)
            dont_keep = list(filter(lambda x: x not in keep, dont_keep))

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

            for rule in DeduplicatorConfig.PRIORITIZATION_RULES.value:
                if rule == "more-exif-data":
                    if MetadataKey.EXIF_DATA.value in candidate[MetadataKey.METADATA.value]:
                        # more exif data is better
                        criteria.append(len(candidate[MetadataKey.METADATA.value][MetadataKey.EXIF_DATA.value]) * -1)
                elif rule == "less-exif-data":
                    if MetadataKey.EXIF_DATA.value in candidate[MetadataKey.METADATA.value]:
                        # more exif data is better
                        criteria.append(len(candidate[MetadataKey.METADATA.value][MetadataKey.EXIF_DATA.value]) * 1)
                elif rule == "bigger-file-size":
                    # reverse, bigger is better
                    criteria.append(candidate[MetadataKey.METADATA.value][MetadataKey.FILE_SIZE.value] * -1)
                elif rule == "smaller-file-size":
                    # smaller is better
                    criteria.append(candidate[MetadataKey.METADATA.value][MetadataKey.FILE_SIZE.value] * 1)
                elif rule == "newer-file-modification-date":
                    # reverse, bigger (later time) is better
                    criteria.append(
                        candidate[MetadataKey.METADATA.value][MetadataKey.FILE_MODIFICATION_DATE.value] * -1)
                elif rule == "older-file-modification-date":
                    # smaller (earlier time) is better
                    criteria.append(
                        candidate[MetadataKey.METADATA.value][MetadataKey.FILE_MODIFICATION_DATE.value] * 1)
                elif rule == "smaller-distance":
                    # smaller distance is better
                    criteria.append(candidate[MetadataKey.DISTANCE.value])
                elif rule == "bigger-distance":
                    # bigger distance is better
                    criteria.append(candidate[MetadataKey.DISTANCE.value] * -1)
                # elif rule == "longer-path":
                # elif rule == "shorter-path":
                elif rule == "contains-copy-in-file-name":
                    # if the filename contains "copy" it is less good
                    criteria.append("copy" in file.get_file_name(candidate[MetadataKey.PATH.value]).lower())
                elif rule == "doesnt-contain-copy-in-file-name":
                    # if the filename contains "copy" it is better
                    criteria.append("copy" not in file.get_file_name(candidate[MetadataKey.PATH.value]).lower())
                elif rule == "longer-file-name":
                    # longer filename is better (for "edited" versions)
                    criteria.append(len(file.get_file_name(candidate[MetadataKey.PATH.value])) * -1)

                elif rule == "shorter-file-name":
                    # shorter filename is better (for "edited" versions)
                    criteria.append(len(file.get_file_name(candidate[MetadataKey.PATH.value])) * 1)

                elif rule == "longer-folder-path":
                    # shorter folder path is better
                    criteria.append(len(file.get_containing_folder(candidate[MetadataKey.PATH.value])) * -1)
                elif rule == "shorter-folder-path":
                    # shorter folder path is better
                    criteria.append(len(file.get_containing_folder(candidate[MetadataKey.PATH.value])))
                elif rule == "higher-score":
                    # reverse, bigger is better
                    criteria.append(candidate[MetadataKey.SCORE.value] * -1)
                elif rule == "lower-score":
                    # lower is better
                    criteria.append(candidate[MetadataKey.SCORE.value] * 1)
                elif rule == "higher-pixel-count":
                    # higher pixel count is better
                    criteria.append(candidate[MetadataKey.METADATA.value][MetadataKey.PIXELCOUNT.value] * -1)
                elif rule == "lower-pixel-count":
                    # lower pixel count is better
                    criteria.append(candidate[MetadataKey.METADATA.value][MetadataKey.PIXELCOUNT.value] * 1)

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

    def _find_empty_folders(self, root_path: Path, recursive: bool, dry_run: bool) -> [str]:
        """
        Finds empty folders within the given root_path
        :param root_path: folder to search in
        """
        result = OrderedSet()

        # traverse bottom-up to remove folders that are empty due to file removal
        for root, directories, files in os.walk(str(root_path), topdown=False):
            # get absolute paths of all files and folders in the current root directory
            abs_file_paths = list(map(lambda x: os.path.abspath(os.path.join(root, x)), files))
            abs_folder_paths = list(map(lambda x: os.path.abspath(os.path.join(root, x)), directories))

            # find out which of those files were deleted by the deduplication process
            files_deleted = list(
                map(lambda x: Path(x), filter(
                    lambda x: Path(x) in self._deduplication_result.get_removed_or_moved_files(),
                    abs_file_paths)))
            files_deleted = list(set(files_deleted + list(
                filter(lambda x: x.parent == Path(root), self._deduplication_result.get_removed_or_moved_files()))))

            folders_deleted = list(filter(lambda x: x in result, abs_folder_paths))
            filtered_directories = list(filter(lambda x: x not in folders_deleted, abs_folder_paths))

            if dry_run:
                if len(files_deleted) > 0 and len(files_deleted) == len(files) and len(folders_deleted) == len(
                        directories):
                    result.append(root)
            else:
                if len(files_deleted) > 0 and len(files) <= 0 and len(directories) <= 0:
                    result.append(root)

            if not recursive:
                break

        return result

    def _remove_folders(self, root_path: Path, folders: [str], dry_run: bool):
        """
        Function to remove empty folders
        :param root_path:
        """
        echo(f"Removing empty folders ({len(folders)}) in: '{root_path}' ...")

        if len(folders) == 0:
            return

        self._progress_manager.start("Removing empty folders", len(folders), "Folder", self.interactive)
        for folder in folders:
            self._progress_manager.set_postfix(self._truncate_middle(folder))

            if not dry_run:
                os.rmdir(folder)

            self._deduplication_result.add_removed_empty_folder(folder)
            self._progress_manager.inc()
        self._progress_manager.clear()

    def _remove_files_marked_as_delete(self, dry_run: bool):
        """
        Removes files that were marked to be deleted in previous deduplication step
        :param dry_run: set to true to simulate this action
        """
        items_to_remove = self._deduplication_result.get_file_with_action(ActionEnum.DELETE)
        marked_files_count = len(items_to_remove)
        if marked_files_count == 0:
            return

        self._progress_manager.start("Removing files", marked_files_count, "File", self.interactive)
        self._delete_files(items_to_remove, dry_run)
        self._progress_manager.clear()

    def _move_files_marked_as_delete(self, target_dir: Path, dry_run: bool):
        """
        Moves files that were marked to be deleted in previous deduplication step to the target directory
        :param target_dir: the directory to move duplicates to
        :param dry_run: set to true to simulate this action
        """
        items_to_move = self._deduplication_result.get_file_with_action(ActionEnum.MOVE)
        marked_files_count = len(items_to_move)
        if marked_files_count == 0:
            return

        self._progress_manager.start("Moving files", marked_files_count, "File", self.interactive)
        self._move_files(items_to_move, target_dir, dry_run)
        self._progress_manager.clear()

    def _delete_files(self, files_to_delete: [str], dry_run: bool):
        """
        Deletes files on disk
        :param files_to_delete: list of absolute file paths
        :param dry_run: set to true to simulate this action
        """
        for file_path in files_to_delete:
            self._progress_manager.set_postfix(self._truncate_middle(file_path))

            if dry_run:
                pass
            else:
                # remove from file system
                if os.path.exists(file_path):
                    os.remove(file_path)

                # remove from persistence
                self._persistence.remove(file_path)

                DUPLICATE_ACTION_DELETE_COUNT.inc()

            self._progress_manager.inc()

    def _move_files(self, files_to_move: List[Path], target_dir: Path, dry_run: bool):
        """
        Moves files on disk
        :param files_to_move: list of absolute file paths
        :param target_dir: directory to move files to
        """
        for file_path in files_to_move:
            self._progress_manager.set_postfix(self._truncate_middle(file_path))

            try:
                if dry_run:
                    continue

                # move file
                if not file_path.exists():
                    continue

                target_file = Path(str(target_dir), *file_path.parts[1:])
                if target_file.exists():
                    if filecmp.cmp(file_path, target_file, shallow=False):
                        os.remove(file_path)
                    else:
                        raise ValueError(f"Cant move duplicate file because the target already exists: {target_file}")
                else:
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(file_path, target_file)

                # remove from persistence
                self._persistence.remove(str(file_path))

                DUPLICATE_ACTION_MOVE_COUNT.inc()
            except Exception as ex:
                logging.exception(ex)
                # LOGGER.log(ex)
            finally:
                self._progress_manager.inc()

    @staticmethod
    def _truncate_middle(text: any, max_length: int = 50):
        text = str(text)
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
