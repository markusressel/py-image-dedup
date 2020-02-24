from pathlib import Path

from py_image_dedup.util.file import file_has_extension
from tests import TestBase


class FileExtensionTest(TestBase):

    def test_png(self):
        paths = [
            "file.png",
            "file.PNG"
        ]
        for path in paths:
            path = Path(path)
            self.assertTrue(file_has_extension(path, [".png"]))
