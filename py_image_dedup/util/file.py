import os
from pathlib import Path
from typing import List


def get_file_name(file_path: str) -> str:
    folder, file = os.path.split(file_path)
    return file


def get_containing_folder(file_path: str) -> str:
    folder, file = os.path.split(file_path)
    return folder


def file_has_extension(file: Path, extensions: List[str] or None) -> bool:
    """
    Checks if a file has one of the given extensions
    :param file: the file to check
    :param extensions: allowed extensions
    :return: true if it matches (case insensitive), false otherwise
    """
    if not isinstance(extensions, List):
        extensions = [extensions]
    if not extensions:
        return True

    if file.suffix.lower() not in (ext.lower() for ext in extensions):
        # skip file with unwanted file extension
        return False
    else:
        return True


def get_files_count(directory: Path, recursive: bool, file_extensions: List[str] or None) -> int:
    """
    :param directory: the directory to analyze
    :param recursive: whether to search the directory recursively
    :param file_extensions: file extensions to include
    :return: number of files in the given directory that match the currently set file filter
    """
    files_count = 0
    for r, d, files in os.walk(str(directory)):
        for file in files:
            file = Path(file)
            if file_has_extension(file, file_extensions):
                files_count += 1
        if not recursive:
            break

    return files_count
