import os

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
    :param directories:
    :return:
    """

    safe_directories = []
    for directory in directories:
        if not os.path.exists(directory):
            echo("Missing directory will be ignored: '%s'" % directory, color='yellow')
            continue
        if not os.path.isdir(directory):
            echo("Directory path is not a directory and will be ignored: '%s'" % directory, color='yellow')
            continue
        else:
            safe_directories.append(directory)

    return safe_directories
