import os
import time
from concurrent.futures import ThreadPoolExecutor

from tqdm import tqdm

from py_image_dedup.library.DeduplicationResult import DeduplicationResult
from py_image_dedup.persistence.ImageSignatureStore import ImageSignatureStore


class ImageMatchDeduplicator:
    EXECUTOR = ThreadPoolExecutor()

    def __init__(self, directories: [str],
                 search_across_root_directories: bool = False,
                 recursive: bool = True,
                 file_extension_filter: [str] = None,
                 max_dist: float = 0.03,
                 threads: int = 1,
                 dry_run: bool = True):
        """
        :param directories: list of directories to process
        :param search_across_root_directories: if set to true duplicates duplicates will also be searched for in
                other root directories specified in 'directories' parameter
        :param recursive: also walk through sub-folders recursively
        :param file_extension_filter: filter files for this extension
        :param max_dist: maximum "difference" allowed, ranging from [0 .. 1] where 0.2 is still a pretty similar image
        :param threads: number of threads to use for concurrent processing
        :param dry_run: if true, no files will actually be removed
        """
        self._directories: [str] = []
        for directory in directories:
            if not os.path.exists(directory):
                self._print("Missing directory will be ignored: '%s'" % directory)
                continue
            if not os.path.isdir(directory):
                self._print("Directory path is not a directory and will be ignored: '%s'" % directory)
                continue
            else:
                self._directories.append(directory)

        self._search_across_root_directories = search_across_root_directories

        self._directory_map = {}
        self._recursive = recursive

        self._file_extension_filter: [str] = file_extension_filter
        self._persistence: ImageSignatureStore = ImageSignatureStore(max_dist=max_dist)
        self._threads: int = threads

        self._dry_run = dry_run

        self._progress_bar: tqdm = None

        self._deduplication_result: DeduplicationResult = None

    def analyze(self) -> {str, str}:
        """
        Analyzes all files, generates identifiers (if necessary) and stores them for later access
        :return: file_path -> identifier
        """

        self._count_files()

        self._print("Analyzing files...")

        for directory, file_count in self._directory_map.items():
            with ThreadPoolExecutor(self._threads) as self.EXECUTOR:
                self._create_file_progressbar(file_count)
                self.__walk_directory_files(root_directory=directory,
                                            command=lambda root_dir, file_dir, file_path: self.__analyze_file(
                                                root_dir,
                                                file_dir,
                                                file_path))

    def deduplicate(self) -> DeduplicationResult:
        """
        Removes duplicates
        :return:
        """

        if self._dry_run:
            self._print("DRY RUN! No files or folders will actually be deleted!")

        self._deduplication_result = DeduplicationResult()

        self.analyze()

        for directory, file_count in self._directory_map.items():
            with ThreadPoolExecutor(self._threads) as self.EXECUTOR:
                self._print("Processing '%s' ..." % directory)
                self._create_file_progressbar(file_count)
                self.__walk_directory_files(root_directory=directory,
                                            command=lambda root_dir, file_dir, file_path: self.__remove_duplicates(
                                                root_dir,
                                                file_dir,
                                                file_path))

        self._remove_files_marked_as_delete()

        self.remove_empty_folders()

        return self._deduplication_result

    def remove_empty_folders(self):
        """
        Searches for empty folders and removes them
        """

        self._print("Removing empty folders...")

        # remove empty folders
        # self._create_folder_progressbar(len(self._directories))
        for directory in self._directories:
            empty_folders = self._find_empty_folders(directory)

            self._remove_empty_folders(directory, empty_folders)
            # self._increment_progress(increase_count_by=1)

    def _count_files(self):
        """
        Counts the amount of files to analyze (used in progress) and stores them in a map
        """
        self._print("Counting files...")

        self._directory_map = {}

        self._create_folder_progressbar(len(self._directories))
        for directory in self._directories:
            self._progress_bar.set_postfix_str("Counting files in '%s' ..." % directory)

            file_count = self._get_files_count(directory)
            self._directory_map[directory] = file_count

            self._increment_progress(increase_count_by=1)

    def __walk_directory_files(self, root_directory: str, command):
        """
        Walks through the files of the given directory

        :param root_directory: the directory to start with
        :param command: the method to execute for every file found
        :return: file_path -> identifier
        """

        # to avoid ascii char problems
        root_directory = str(root_directory)
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

                self.EXECUTOR.submit(command, root_directory, root, file_path)

            if not self._recursive:
                return

    def __file_extension_matches_filter(self, file: str) -> bool:
        """
        Checks if a file matches the filter set for this deduplicator
        :param file: the file to check
        :return: true if it matches, false otherwise
        """
        if not self._file_extension_filter:
            return True

        filename, file_extension = os.path.splitext(file)

        if file_extension.lower() not in (ext.lower() for ext in self._file_extension_filter):
            # skip file with unwanted file extension
            return False
        else:
            return True

    def __analyze_file(self, root_directory: str, file_directory: str, file_path: str):
        """
        Analyzes a single file
        :param file_path: the file path
        """
        self._progress_bar.set_postfix_str("Analyzing Image '%s' ..." % file_path)

        self._persistence.add(file_path)

        self._increment_progress(1)

    def __remove_duplicates(self, root_directory: str, file_directory: str, reference_file_path: str):
        """
        Removes all duplicates of the specified file
        :param reference_file_path: the file to check for duplicates
        """

        self._increment_progress(1)

        if self._persistence.get(reference_file_path)[0]['metadata']['already_deduplicated']:
            return
        else:
            self._progress_bar.set_postfix_str("Searching duplicates for '%s' ..." % reference_file_path)

        if not os.path.exists(reference_file_path):
            # remove from persistence
            self._persistence.remove(reference_file_path)
            return

        duplicate_candidates = self._persistence.search_similar(reference_file_path)

        if self._search_across_root_directories:
            # filter by files in at least one of the specified root directories
            # this is necessary because the database might hold items for other paths already
            # and those are not interesting to us
            duplicate_candidates = [x for x in duplicate_candidates if x['path'].startswith(tuple(self._directories))]
        else:
            # filter by files in the same root directory
            duplicate_candidates = [x for x in duplicate_candidates if x['path'].startswith(root_directory)]

        self._save_duplicates_for_result(reference_file_path, duplicate_candidates)

        candidates_to_delete = self._select_images_to_delete(duplicate_candidates)
        for candidate in candidates_to_delete:
            candidate_path = candidate['path']
            candidate_dist = candidate['dist']

            # self._print("File '%s' is duplicate of '%s' with a dist value of '%s'" % (
            #     reference_file_path, candidate_path, candidate_dist))

            _, file_name = os.path.split(candidate_path)
            self._deduplication_result.add_removed_file(candidate_path)

        # remember that this file has been processed in it's current state
        if not self._dry_run:
            self._persistence.update(reference_file_path, {"already_deduplicated": True})

    def _save_duplicates_for_result(self, reference_file_path, duplicate_candidates):
        duplicate_files_of_reference_file = []

        for candidate in duplicate_candidates:
            candidate_path = candidate['path']
            candidate_dist = candidate['dist']

            # skip self
            if candidate_path == reference_file_path:
                continue

            result_entry = {
                'path': candidate_path,
                'dist': candidate_dist
            }
            duplicate_files_of_reference_file.append(result_entry)

        self._deduplication_result.set_file_duplicates(reference_file_path, duplicate_files_of_reference_file)

    def _select_images_to_delete(self, duplicate_candidates: [{}]) -> [{}]:
        """
        Sorts images according to the desired priorities and marks all but the first one as "to-be-deleted"
        :param duplicate_candidates: the images to analyze
        """
        # sort after all criteria
        # the first item in the list will be the most preferred one of all found duplicates,
        # all other ones will be marked to remove

        duplicate_candidates = sorted(duplicate_candidates, key=lambda x: (
            # shorter file path is better
            len(x['path']),

            # reverse, bigger is better
            x['metadata']['filesize'] * -1,

            # reverse, bigger is better
            x['metadata']['modification_date'] * -1,

            # reverse, bigger is better
            x['score'] * -1,

            # smaller is better
            x['dist'],

            # just to assure the order in the result is the same
            # and recurring runs will result in the same order
            x['path'],

        ))

        # keep first and mark others for removal
        return duplicate_candidates[1:]

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

            if not self._recursive:
                break

        return result

    def _remove_empty_folders(self, root_path: str, folders: [str]):
        """
        Function to remove empty folders
        :param root_path:
        :param remove_root:
        """

        self._print("Removing empty folders in: '%s' ..." % root_path)

        if len(folders) == 0:
            return

        self._create_folder_progressbar(len(folders))
        for folder in folders:
            if not self._dry_run:
                self._progress_bar.set_postfix_str("Removing empty folder '%s'" % folder)
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
            if not self._recursive:
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
        if self._progress_bar:
            self._progress_bar.close()

        self._progress_bar = tqdm(total=total_count, unit=unit, unit_scale=True, mininterval=1)
        return self._progress_bar

    def _remove_files_marked_as_delete(self):
        """
        Removes files that were marked to be deleted in previous deduplication step
        """
        self._print("Removing duplicate files...")

        if len(self._deduplication_result.get_removed_files()) == 0:
            return

        self._create_file_progressbar(total_file_count=len(self._deduplication_result.get_removed_files()))
        self._delete_files(self._deduplication_result.get_removed_files())

    def _delete_files(self, files_to_delete: [str]):
        """
        Deletes files on disk
        :param files_to_delete: list of absolute file paths
        """

        for file in files_to_delete:
            self._progress_bar.set_postfix_str("Removing '%s' ..." % file)

            # remove the smaller/equal sized and/or older/equally old file
            if self._dry_run:
                pass
            else:
                # remove from file system
                if os.path.exists(file):
                    os.remove(file)

                # remove from persistence
                self._persistence.remove(file)

            self._deduplication_result.add_removed_file(file)

            self._increment_progress(increase_count_by=1)

    def _print(self, text: str):
        print("\n%s" % text, flush=True)
        # delay a little so it is in line
        time.sleep(0.1)
