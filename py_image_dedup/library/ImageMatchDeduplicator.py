import os
from concurrent.futures import ThreadPoolExecutor

from py_image_dedup.persistence.ImageSignatureStore import ImageSignatureStore


class ImageMatchDeduplicator:
    EXECUTOR = ThreadPoolExecutor()

    def __init__(self, directories: [str], max_dist: float, threads: int = 1):
        self._directories = directories
        self._persistence = ImageSignatureStore(max_dist=max_dist)
        self._threads = threads

        self._changed_files_dict = {}

    def analyze(self, recursive: bool, file_extensions: [str] = None) -> {str, str}:
        """
        Analyzes all files, generates identifiers (if necessary) and stores them
        for later access
        :return: file_path -> identifier
        """

        print("Analyzing files...")

        with ThreadPoolExecutor(self._threads) as self.EXECUTOR:
            for directory in self._directories:
                self._walk_directory(root_directory=directory,
                                     recursive=recursive,
                                     file_extensions=file_extensions,
                                     command=lambda file_path: self._analyze_file(file_path))

    def deduplicate(self, recursive: bool, file_extensions: [str] = None, dry_run: bool = False):
        """
        Removes duplicates

        :param recursive:
        :param file_extensions:
        :param dry_run:
        :return:
        """

        self.analyze(recursive, file_extensions)

        with ThreadPoolExecutor(self._threads) as self.EXECUTOR:
            for directory in self._directories:
                self._walk_directory(root_directory=directory,
                                     recursive=recursive,
                                     file_extensions=file_extensions,
                                     command=lambda file_path: self._remove_duplicates(file_path, dry_run=dry_run))

        # remove empty folders
        for directory in self._directories:
            self._remove_empty_folders(directory, remove_root=True, dry_run=dry_run)

    def _walk_directory(self, root_directory: str, recursive: bool, command, file_extensions: [str] = None):
        """
        Walks through the files of the given directory

        :param root_directory: the directory to start with
        :param recursive: also walk through subfolders recursively
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
                if not self._file_extension_matches_filter(file_extensions, file):
                    continue

                # skip if not existent (probably already deleted)
                if not os.path.exists(file_path):
                    continue

                self.EXECUTOR.submit(command, file_path)

            if not recursive:
                return

    def _file_extension_matches_filter(self, file_extensions: [str], file: str) -> bool:
        if not file_extensions:
            return True

        filename, file_extension = os.path.splitext(file)

        if file_extension.lower() not in (ext.lower() for ext in file_extensions):
            # skip file with unwanted file extension
            return False
        else:
            return True

    def _analyze_file(self, file_path):
        print("Analyzing Image '%s' ..." % file_path)
        if self._persistence.add(file_path):
            self._changed_files_dict[file_path] = ""

    def _remove_duplicates(self, reference_file_path: str, dry_run: bool = True):
        """
        Removes all duplicates of the specified file
        :param reference_file_path: the file to check for duplicates
        :param dry_run: if true, no files will actually be removed
        """

        if reference_file_path not in self._changed_files_dict:
            print("File hasn't changed and is not analyzed for duplicates: '%s'" % reference_file_path)
            return
        else:
            print("Searching duplicates for '%s' ..." % reference_file_path)

        if not os.path.exists(reference_file_path):
            return

        duplicate_candidates = self._persistence.search_similar(reference_file_path)

        if (len(duplicate_candidates)) <= 1:
            # skip files that don't have duplicates
            print("No duplicates for '%s' ..." % reference_file_path)
            return

        print("Removing duplicates for '%s' ..." % reference_file_path)

        reference_file_size = os.stat(reference_file_path).st_size
        reference_file_mod_date = os.path.getmtime(reference_file_path)

        candidates_sorted_by_filesize = sorted(duplicate_candidates, key=lambda c: c['metadata']['filesize'])

        for candidate in candidates_sorted_by_filesize:
            candidate_path = candidate['path']
            candidate_dist = candidate['dist']
            candidate_filesize = candidate['metadata']['filesize']
            candidate_modification_date = candidate['metadata']['modification_date']

            # skip candidate if it's the same file
            if candidate_path == reference_file_path:
                continue

            print("File '%s' is duplicate of '%s' with a dist value of '%s'" % (
                reference_file_path, candidate_path, candidate_dist))

            # compare filesize, modification date
            if reference_file_size <= candidate_filesize and \
                    candidate_modification_date <= reference_file_mod_date:

                # remove the smaller/equal sized and/or older/equally old file
                if dry_run:
                    print("DRY RUN: Would remove '%s'" % candidate_path)
                else:
                    print("Removing '%s'" % candidate_path)
                    # remove from file system
                    os.remove(candidate_path)

                # remove from persistence
                self._persistence.remove(candidate_path)

    def _remove_empty_folders(self, root_path: str, remove_root: bool = True, dry_run: bool = True):
        """
        Function to remove empty folders
        :param root_path:
        :param remove_root:
        :return:
        """
        if not os.path.isdir(root_path):
            return

        # remove empty subfolders
        files = os.listdir(root_path)
        if len(files):
            for f in files:
                fullpath = os.path.join(root_path, f)
                if os.path.isdir(fullpath):
                    self._remove_empty_folders(fullpath, dry_run=dry_run)

        # if folder empty, delete it
        files = os.listdir(root_path)
        if len(files) == 0 and remove_root:

            if dry_run:
                print("DRY RUN: Would remove empty folder '%s'" % root_path)
            else:
                print("Removing empty folder '%s'" % root_path)
                os.rmdir(root_path)
