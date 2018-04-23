import os

from py_image_dedup.library.ImageAnalyzer import ImageAnalyzer


class Deduplicator:

    def __init__(self, directories: [str]):
        self._directories = directories
        self._image_analyzer = ImageAnalyzer(directories[0])

    def analyze(self, recursive: bool) -> {str, str}:
        """
        Analyzes all files, generates identifiers (if necessary) and stores them
        for later access
        """

        all_hashes = {}
        for directory in self._directories:
            all_hashes.update(self._walk_directory(directory, recursive))

        return all_hashes

    def deduplicate(self, recursive: bool):
        pass

    def _walk_directory(self, root_directory: str, recursive: bool) -> {str, str}:
        result_map = {}

        # to avoid ascii char problems
        root_directory = str(root_directory)
        for (root, dirs, files) in os.walk(root_directory):
            # root is the place you're listing
            # dirs is a list of directories directly under root
            # files is a list of files directly under root

            print('Analyzing "%s" ...' % root)

            for file in files:
                file_path = os.path.abspath(os.path.join(root, file))
                # click.echo('File: %s' % file)
                image_hash = self._image_analyzer.calculate_image_identifier(file_path)

                # store in map
                result_map[file_path] = image_hash

            if not recursive:
                return result_map

        return result_map
