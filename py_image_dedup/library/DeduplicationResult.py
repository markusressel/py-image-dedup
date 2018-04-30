class DeduplicationResult:

    def __init__(self):
        self._removed_files = set()
        self._removed_folders = set()
        self._file_duplicates = {}

    def get_duplicate_count(self) -> int:
        count = 0
        for key, value in self._file_duplicates.items():
            if len(value) > 0:
                count += 1

        return count

    def get_removed_files(self) -> []:
        """
        :return: a list of all the files that have been removed
        """
        return self._removed_files

    def add_removed_file(self, file):
        """
        Adds a file to the list of removed files
        :param file: the file to add
        """
        self._removed_files.add(file)

    def get_removed_empty_folders(self) -> []:
        """
        :return: a list of empty folders that have been removed
        """
        return self._removed_folders

    def add_removed_empty_folder(self, folder):
        """
        Adds a folder to the list of removed empty folders
        :param folder: the folder to add
        """
        self._removed_folders.add(folder)

    def set_file_duplicates(self, reference_file: str, duplicate_files: [str]):
        """
        Set a list of files that are duplicates of the reference file
        :param reference_file: the file that is used as a baseline
        :param duplicate_files: duplicates of the reference_file
        """
        self._file_duplicates[reference_file] = duplicate_files

    def get_file_duplicates(self) -> {}:
        """
        Get a list of files that are duplicates of other files
        """
        return self._file_duplicates
