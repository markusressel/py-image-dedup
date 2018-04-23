import hashlib
import os
import time

from py_image_dedup.persistence.IdentifierStore import IdentifierStore


class ImageAnalyzer:
    __BLOCK_SIZE = 65536

    def __init__(self, db_path: str):
        self._persistence = IdentifierStore(db_path)

    def calculate_image_identifier(self, file_path: str) -> str:
        """
        Calculates an "identifier" for a file
        currently composed of the file MD5 hash and it's byte size

        :param file_path: the file to analyze
        :return: an identifier
        """

        stat_info = os.stat(file_path)
        file_size = stat_info.st_size
        file_mod_date = os.path.getmtime(file_path)

        persisted_value = self._persistence.get(file_path)
        if persisted_value and file_mod_date <= persisted_value["date"]:
            return persisted_value["identifier"]

        hasher = hashlib.md5()
        with open(file_path, 'rb') as afile:
            buf = afile.read(self.__BLOCK_SIZE)
            while len(buf) > 0:
                hasher.update(buf)
                buf = afile.read(self.__BLOCK_SIZE)

        hashcode = hasher.hexdigest()

        identifier = "%s_%s" % (hashcode, file_size)
        persistence_object = {
            "date": int(time.time()),
            "identifier": identifier
        }

        self._persistence.set(file_path, persistence_object)
        self._persistence.save()
        return identifier
