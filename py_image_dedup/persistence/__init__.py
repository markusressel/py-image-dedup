import os

from py_image_dedup.persistence.MetadataKey import MetadataKey
from py_image_dedup.persistence.StoreEntry import StoreEntry


class ImageSignatureStore:
    """
    Base class for Persistence implementations
    """

    DATAMODEL_VERSION = 5

    def __init__(self, use_exif_data: bool = True):
        self._use_exif_data = use_exif_data

        pass

    def add(self, image_file_path: str):
        """
        Analyze an image file and add it to the store

        :param image_file_path: path to the image file
        :param metadata: Sometimes you want to store information with your images independent of the reverse
        image search functionality. You can do that with the metadata= field in the add_image function.
        :return: true if the file was added, false otherwise
        """

        image_data = self._create_metadata_dict(image_file_path)

        # check if the file has already been analyzed (and didn't change in the meantime)
        existing_entity = self.get(image_file_path)
        if existing_entity is not None:
            is_data_version_ok = False
            if MetadataKey.DATAMODEL_VERSION.value in existing_entity[MetadataKey.METADATA.value]:
                is_data_version_ok = existing_entity[MetadataKey.METADATA.value][
                                         MetadataKey.DATAMODEL_VERSION.value] == self.DATAMODEL_VERSION

            if is_data_version_ok and \
                    existing_entity[MetadataKey.METADATA.value][MetadataKey.FILE_SIZE.value] == image_data[
                MetadataKey.FILE_SIZE.value] and \
                    existing_entity[MetadataKey.METADATA.value][
                        MetadataKey.FILE_MODIFICATION_DATE.value] == image_data[
                MetadataKey.FILE_MODIFICATION_DATE.value]:
                # print("File is the same, not adding again")
                return

        self._add(image_file_path, image_data)

    def _create_metadata_dict(self, image_file_path: str) -> dict:
        """
        Creates a dictionary that should be stored in persistence

        :param image_file_path:
        :return: dictionary containing all relevant information
        """

        from py_image_dedup.util import ImageUtils

        image_data = {}
        image_data[MetadataKey.PATH.value] = image_file_path

        # get some metadata
        file_size = os.stat(image_file_path).st_size
        file_modification_date = os.path.getmtime(image_file_path)

        image_data[MetadataKey.DATAMODEL_VERSION.value] = self.DATAMODEL_VERSION
        image_data[MetadataKey.FILE_SIZE.value] = file_size
        image_data[MetadataKey.FILE_MODIFICATION_DATE.value] = file_modification_date

        image_data[MetadataKey.PIXELCOUNT.value] = ImageUtils.get_pixel_count(image_file_path)

        if self._use_exif_data:
            exif_data = ImageUtils.get_exif_data(image_file_path)
            exif_data = self._normalize_meta_data_for_db(exif_data)
            image_data[MetadataKey.EXIF_DATA.value] = exif_data

        return image_data

    def _normalize_meta_data_for_db(self, dictionary: dict) -> dict:
        """
        :param dictionary:
        :return:
        """
        result = {}
        for k, v in dictionary.items():
            if isinstance(v, dict):
                result[k] = self._normalize_meta_data_for_db(v)
                continue

            normalized_value = v
            if isinstance(v, bytes) or isinstance(v, tuple):
                normalized_value = str(v)

            result[k] = normalized_value

        return result

    def _add(self, image_file_path: str, image_data: dict) -> None:
        """
        Saves image data for the specified image file path

        :param image_file_path: image file path
        :param image_data: metadata for the image
        """
        raise NotImplementedError()

    def get(self, image_file_path: str) -> StoreEntry or None:
        """
        Get a store entry by it's file_path
        :param image_file_path: file path to search for
        :return: store entry or None
        """
        raise NotImplementedError()

    def get_all(self) -> (int, object):
        """
        :return: item count, stored entries as a generator function
        """
        raise NotImplementedError()

    def find_similar(self, reference_image_file_path: str) -> []:
        """
        Search for similar images to the specified one

        :param reference_image_file_path: the reference image file
        :return: list of images that are similar to the reference file
        """
        raise NotImplementedError()

    def remove(self, image_file_path: str) -> None:
        """
        Remove all entries with the given file path

        :param image_file_path: the path of an image file
        """
        raise NotImplementedError()

    def remove_entries_of_missing_files(self):
        """
        Remove all entries with files that don't exist
        """
        entries = self.get_all()
        for entry in entries:
            file_path = entry['path']
            if not os.path.exists(file_path):
                self.remove(file_path)

    def remove_all(self) -> None:
        """
        Remove all entries from Database
        """
        raise NotImplementedError()
