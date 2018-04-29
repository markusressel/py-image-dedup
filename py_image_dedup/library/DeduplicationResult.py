class DeduplicationResult:

    def __init__(self):
        self._removed_files = []
        self._removed_folders = []
        self._file_duplicates = {}

    def get_duplicate_count(self) -> int:
        count = 0
        for key, value in self._file_duplicates.items():
            if len(value) > 0:
                count += 1

        return count

    def get_removed_files(self) -> []:
        return self._removed_files

    def add_removed_file(self, file):
        self._removed_files.append(file)

    def get_removed_empty_folders(self) -> []:
        return self._removed_folders

    def add_removed_empty_folder(self, folder):
        self._removed_folders.append(folder)

    def set_file_duplicates(self, reference_file: str, duplicate_files: [str]):
        self._file_duplicates[reference_file] = duplicate_files

    def get_file_duplicates(self) -> {}:
        return self._file_duplicates
