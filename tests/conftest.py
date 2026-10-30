import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def tests_working_directory():
    """
    DeduplicatorConfig loads "py_image_dedup.yaml" from the current working directory,
    so make sure tests always use tests/py_image_dedup.yaml, regardless of where pytest is started.
    """
    previous_cwd = os.getcwd()
    os.chdir(Path(__file__).parent)
    yield
    os.chdir(previous_cwd)
