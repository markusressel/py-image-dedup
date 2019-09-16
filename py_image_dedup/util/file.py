import os
from typing import List

from py_image_dedup.util import echo


def get_file_name(file_path: str) -> str:
    folder, file = os.path.split(file_path)
    return file


def get_file_name_without_extension(file_path: str) -> str:
    file_name = get_file_name(file_path)
    return os.path.splitext(file_name)[0]


def get_file_extension(file_path: str) -> str:
    file_name = get_file_name(file_path)
    return os.path.splitext(file_name)[1]


def get_containing_folder(file_path: str) -> str:
    folder, file = os.path.split(file_path)
    return folder


def validate_directories_exist(directories: [str]) -> [str]:
    """
    Filters a list of directories to only contain existing ones
    :param directories: list of directories
    :return: filtered list
    """

    safe_directories = []
    for directory in directories:
        abs_path = os.path.abspath(directory)

        if not os.path.exists(abs_path):
            echo("Missing directory will be ignored: '{}' ({})".format(abs_path, directory), color='yellow')
            continue
        if not os.path.isdir(abs_path):
            echo("Directory path is not a directory and will be ignored: '{}".format(abs_path, directory),
                 color='yellow')
            continue
        else:
            safe_directories.append(abs_path)

    return safe_directories


def file_has_extension(file: str, extensions: List[str] or None) -> bool:
    """
    Checks if a file matches the filter set for this deduplicator
    :param file: the file to check
    :param extensions: allowed extensions
    :return: true if it matches, false otherwise
    """
    if not extensions:
        return True

    filename, file_extension = os.path.splitext(file)

    if file_extension.lower() not in (ext.lower() for ext in extensions):
        # skip file with unwanted file extension
        return False
    else:
        return True


def get_files_count(directory: str, recursive: bool, file_extensions: List[str] or None) -> int:
    """
    :param directory: the directory to analyze
    :param recursive: whether to search the directory recursively
    :param file_extensions: file extensions to include
    :return: number of files in the given directory that match the currently set file filter
    """
    files_count = 0
    for r, d, files in os.walk(directory):
        for file in files:
            if file_has_extension(file, file_extensions):
                files_count += 1
        if not recursive:
            break

    return files_count
