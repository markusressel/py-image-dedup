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
        :return: file_path -> identifier
        """

        all_identifiers = {}
        for directory in self._directories:
            new_identifiers = self._walk_directory(directory, recursive)
            all_identifiers.update(new_identifiers)

        return all_identifiers

    def deduplicate(self, recursive: bool):
        all_identifiers = self.analyze(recursive)

        files_with_same_identifier = self._collect_same_identifier(all_identifiers)

        result = {
            "files_with_duplicates": 0,
            "deleted": [],
            "kept": [],
            "ignored": [],
        }
        for key, value in files_with_same_identifier.items():
            if (len(value)) <= 1:
                # skip identifiers that only occur once
                continue

            result["files_with_duplicates"] += 1

            # currently without effect since the files have the exact same filesize
            # but will be of use when images can be compared without an exact file match
            files_sorted_by_filesize = sorted(value, key=lambda f: os.stat(f).st_size)

            remnant_found = False
            for file in files_sorted_by_filesize:
                if "Camera" in file:
                    result["ignored"].append(file)
                    result["kept"].append(file)
                    remnant_found = True
                    continue
                else:
                    if remnant_found:
                        os.remove(file)
                        result["deleted"].append(file)
                        continue
                    else:
                        remnant_found = True
                        result["kept"].append(file)
                        continue

        return result

    def _collect_same_identifier(self, all_hashes):
        same_hashes = {}
        for path, identifier in all_hashes.items():
            if identifier not in same_hashes:
                same_hashes[identifier] = []

            same_hashes[identifier].append(path)

        return same_hashes

    def _walk_directory(self, root_directory: str, recursive: bool) -> {str, str}:
        """
        :param root_directory:
        :param recursive:
        :return: file_path -> identifier
        """

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
                image_identifier = self._image_analyzer.calculate_image_identifier(file_path)

                # store in map
                result_map[file_path] = image_identifier

            if not recursive:
                return result_map

        return result_map
