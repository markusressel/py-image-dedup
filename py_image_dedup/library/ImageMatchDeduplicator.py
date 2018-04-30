import os
from concurrent.futures import ThreadPoolExecutor

from tqdm import tqdm

from py_image_dedup.library.DeduplicationResult import DeduplicationResult
from py_image_dedup.persistence.ImageSignatureStore import ImageSignatureStore


class ImageMatchDeduplicator:
    EXECUTOR = ThreadPoolExecutor()

    def __init__(self, directories: [str],
                 recursive: bool = True,
                 file_extension_filter: [str] = None,
                 max_dist: float = 0.03,
                 threads: int = 1,
                 dry_run: bool = True):
        """
        :param directories:
        :param recursive: also walk through subfolders recursively
        :param file_extension_filter:
        :param max_dist:
        :param threads:
        :param dry_run: if true, no files will actually be removed
        """
        self._directories: [str] = directories
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

        print("Analyzing files...")

        for directory, file_count in self._directory_map.items():
            with ThreadPoolExecutor(self._threads) as self.EXECUTOR:
                self._create_file_progressbar(file_count)
                self._walk_directory(root_directory=directory,
                                     command=lambda root_dir, file_dir, file_path: self.__analyze_file(root_dir,
                                                                                                       file_dir,
                                                                                                       file_path))

    def deduplicate(self) -> DeduplicationResult:
        """
        Removes duplicates
        :return:
        """

        self._deduplication_result = DeduplicationResult()

        self.analyze()

        for directory, file_count in self._directory_map.items():
            with ThreadPoolExecutor(self._threads) as self.EXECUTOR:
                print("Processing '%s' ..." % directory)
                self._create_file_progressbar(file_count)
                self._walk_directory(root_directory=directory,
                                     command=lambda root_dir, file_dir, file_path: self.__remove_duplicates(root_dir,
                                                                                                            file_dir,
                                                                                                            file_path))

        self._remove_files_marked_as_delete()

        self.remove_empty_folders()

        return self._deduplication_result

    def remove_empty_folders(self):
        """
        Searches for empty folders and removes them
        """

        print("Removing empty folders...")

        # remove empty folders
        self._create_folder_progressbar(len(self._directories))
        for directory in self._directories:
            self._remove_empty_folders(directory, remove_root=True)
            self._increment_progress(increase_count_by=1)

    def _count_files(self):
        """
        Counts the amount of files to analyze (used in progress) and stores them in a map
        """
        print("Counting files...")

        self._directory_map = {}

        self._create_folder_progressbar(len(self._directories))
        for directory in self._directories:
            self._progress_bar.set_postfix_str("Counting files in '%s' ..." % directory)

            file_count = self._get_files_count(directory)
            self._directory_map[directory] = file_count

            self._increment_progress(increase_count_by=1)

    def _walk_directory(self, root_directory: str, command):
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
                if not self._file_extension_matches_filter(file):
                    continue

                # skip if not existent (probably already deleted)
                if not os.path.exists(file_path):
                    continue

                self.EXECUTOR.submit(command, root_directory, root, file_path)

            if not self._recursive:
                return

    def _file_extension_matches_filter(self, file: str) -> bool:
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
        :return:
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

        # filter by files in the same root directory
        duplicate_candidates = [x for x in duplicate_candidates if x['path'].startswith(root_directory)]

        self._save_duplicates_for_result(reference_file_path, duplicate_candidates)

        self._select_images_to_delete(duplicate_candidates)

        # remember that this file has been processed in it's current state
        if not self._dry_run:
            self._persistence.update(reference_file_path, {"already_deduplicated": True})

    def _save_duplicates_for_result(self, reference_file_path, duplicate_candidates):
        duplicate_files_of_reference_file = []

        for candidate in duplicate_candidates:
            candidate_path = candidate['path']

            # skip self
            if candidate_path == reference_file_path:
                continue

            duplicate_files_of_reference_file.append(candidate_path)

        self._deduplication_result.set_file_duplicates(reference_file_path, duplicate_files_of_reference_file)

    def _select_images_to_delete(self, duplicate_candidates: [str]) -> None:
        """
        Sorts images according to the desired priorities and marks all but the first one as "to-be-deleted"
        :param duplicate_candidates: the images to analyze
        """
        # sort after all criteria
        duplicate_candidates = sorted(duplicate_candidates, key=lambda x: (
            x['path'],
            x['dist'],
            x['metadata']['modification_date'],
            x['metadata']['filesize'],
            x['score']))

        # keep first and mark others for removal
        for candidate in duplicate_candidates[1:]:
            candidate_path = candidate['path']
            candidate_dist = candidate['dist']

            # print("File '%s' is duplicate of '%s' with a dist value of '%s'" % (
            #     reference_file_path, candidate_path, candidate_dist))

            _, file_name = os.path.split(candidate_path)
            self._deduplication_result.add_removed_file(candidate_path)

    def _remove_empty_folders(self, root_path: str, remove_root: bool = True):
        """
        Function to remove empty folders
        :param root_path:
        :param remove_root:
        """
        if not os.path.isdir(root_path):
            return

        # remove empty subfolders
        files = os.listdir(root_path)
        if len(files):
            for f in files:
                fullpath = os.path.join(root_path, f)
                if os.path.isdir(fullpath):
                    self._remove_empty_folders(fullpath)

        # if folder empty, delete it
        files = os.listdir(root_path)
        if len(files) == 0 and remove_root:

            if self._dry_run:
                print("DRY RUN: Would remove empty folder '%s'" % root_path)
            else:
                print("Removing empty folder '%s'" % root_path)
                os.rmdir(root_path)

            self._deduplication_result.add_removed_empty_folder(root_path)

    def _get_files_count(self, directory: str) -> int:
        """
        :param directory: the directory to analyze
        :return: number of files in the given directory that match the currently set file filter
        """

        files_count = 0
        for r, d, files in os.walk(directory):
            for file in files:
                if self._file_extension_matches_filter(file):
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
        print("Removing duplicate files...")

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
                os.remove(file)

                # remove from persistence
                self._persistence.remove(file)

            self._deduplication_result.add_removed_file(file)

            self._increment_progress(increase_count_by=1)
