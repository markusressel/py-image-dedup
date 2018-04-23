import os


class ImageAnalyzer:

    def calculate_image_hash(self, file: str):
        """
        Calculates an "identifier" for a file
        currently composed of the file MD5 hash and it's byte size

        :param file: the file to analyze
        :return: an identifier
        """

        stat_info = os.stat(file)
        file_size = stat_info.st_size

        import hashlib
        BLOCKSIZE = 65536
        hasher = hashlib.md5()
        with open(file, 'rb') as afile:
            buf = afile.read(BLOCKSIZE)
            while len(buf) > 0:
                hasher.update(buf)
                buf = afile.read(BLOCKSIZE)

        hashcode = hasher.hexdigest()

        return "%s_%s" % (hashcode, file_size)
