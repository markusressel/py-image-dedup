import os


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
