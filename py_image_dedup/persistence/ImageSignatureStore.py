import os

from elasticsearch import Elasticsearch
from image_match.elasticsearch_driver import SignatureES

from py_image_dedup.persistence.MetadataKey import MetadataKey


class ImageSignatureStore:
    # counter used to track how many new values were set
    # and limit the amount of disk writes
    __SET_CALL_COUNTER = 0

    EL_INDEX = 'images'
    EL_DOC_TYPE = 'image'

    DEFAULT_DATABASE_PORT = 9200

    DATAMODEL_VERSION = 2

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_DATABASE_PORT, max_dist: float = 0.03):
        if port is None:
            port = self.DEFAULT_DATABASE_PORT

        self._store = SignatureES(
            es=Elasticsearch(
                hosts=[
                    {'host': host, 'port': port}
                ]
            ),
            index=self.EL_INDEX,
            doc_type=self.EL_DOC_TYPE,
            distance_cutoff=max_dist
        )

    def add(self, image_file: str, metadata: dict = None) -> bool:
        """
        Add an image to the store
        :param image_file:
        :param metadata: Sometimes you want to store information with your images independent of the reverse
        image search functionality. You can do that with the metadata= field in the add_image function.
        :return: true if the file was added, false otherwise
        """

        # get some metadata
        file_size = os.stat(image_file).st_size
        file_modification_date = os.path.getmtime(image_file)

        # check if the file has already been analyzed (and didn't change in the meantime)
        existing_entities = self.get(image_file)

        if len(existing_entities) > 1:
            print("WARNING: More than a single entry for this file: %s" % image_file)

        for existing_entity in existing_entities:
            is_data_version_ok = False
            if MetadataKey.DATAMODEL_VERSION.value in existing_entity[MetadataKey.METADATA.value]:
                is_data_version_ok = existing_entity[MetadataKey.METADATA.value][
                                         MetadataKey.DATAMODEL_VERSION.value] == self.DATAMODEL_VERSION

            if is_data_version_ok and \
                    existing_entity[MetadataKey.METADATA.value][MetadataKey.FILE_SIZE.value] == file_size and \
                    existing_entity[MetadataKey.METADATA.value][
                        MetadataKey.FILE_MODIFICATION_DATE.value] == file_modification_date:
                # print("File is the same, not adding again")
                return False

        # remove existing entries
        self.remove(image_file)

        if not metadata:
            metadata = {}

        metadata[MetadataKey.DATAMODEL_VERSION.value] = self.DATAMODEL_VERSION
        metadata[MetadataKey.FILE_SIZE.value] = file_size
        metadata[MetadataKey.FILE_MODIFICATION_DATE.value] = file_modification_date

        # read exif data if possible
        self._append_exif_data_if_possible(metadata, image_file)

        try:
            self._store.add_image(image_file, metadata=metadata)
            # print("Added file to SignatureStore: '%s'" % image_file)
            return True
        except Exception as e:
            print(e)
            return False

    def _append_exif_data_if_possible(self, metadata, image_file) -> {}:
        import PIL.Image
        img = PIL.Image.open(image_file)

        exif_data = img._getexif()
        if exif_data:
            import PIL.ExifTags
            exif_data = {
                PIL.ExifTags.TAGS[key]: value for key, value in exif_data.items() if key in PIL.ExifTags.TAGS
            }

            if 'DateTimeDigitized' in exif_data:
                metadata['exif_date_time_digitized'] = exif_data['DateTimeDigitized']
            if 'DateTimeOriginal' in exif_data:
                metadata['exif_date_time_original'] = exif_data['DateTimeOriginal']
            if 'Orientation' in exif_data:
                metadata['exif_orientation'] = exif_data['Orientation']

            if 'XResolution' in exif_data:
                metadata['exif_x_resolution'] = exif_data['XResolution']
            if 'YResolution' in exif_data:
                metadata['exif_y_resolution'] = exif_data['YResolution']

            if 'ExifImageWidth' in exif_data:
                metadata['exif_image_width'] = exif_data['ExifImageWidth']
            if 'ExifImageHeight' in exif_data:
                metadata['exif_image_height'] = exif_data['ExifImageHeight']

        return metadata

    def get(self, file_path: str):
        es_query = {
            'query': {
                "constant_score": {
                    "filter": {
                        "term": {'path': file_path}
                    }
                }
            }
        }

        query_result = self._store.es.search(self.EL_INDEX, body=es_query)

        hits = query_result['hits']['hits']
        result = []

        for entity in hits:
            result.append(entity['_source'])

        return result

    def get_all(self) -> []:
        """
        :return: all stored entries
        """

        es_query = {
            'query': {'match_all': {}}
        }

        from elasticsearch.helpers import scan
        return scan(
            self._store.es,
            index=self.EL_INDEX,
            doc_type=self.EL_DOC_TYPE,
            preserve_order=True,
            query=es_query,
        )

    def search_similar(self, image_file: str, all_orientations: bool = True) -> []:
        """
        Search for similar images to the specified one
        :param image_file: the reference image file
        :param all_orientations: include rotated versions of the reference image
        :return:
        """

        entities = self.get(image_file)
        if len(entities) > 0:
            result = []
            rec = self._store.search_single_record(entities[0])
            result.extend(rec)

            return result
        else:
            return self._store.search_image(image_file, all_orientations=all_orientations)

    def search_metadata(self, metadata: dict) -> []:
        """
        Search for images with metadata properties.

        Note: Metadata will be empty if you did not provide it when adding an image
        :param metadata:
        :return:
        """

        search_dict = {}
        for key, value in metadata.items():
            search_dict["metadata.%s" % key] = value

        es_query = {
            'query': {'match': search_dict}
        }

        return self._store.es.search(self.EL_INDEX, body=es_query)

    def update_metadata(self, file_path: str, metadata: {}):
        """
        Update the metadata of an entry
        :param file_path: entry path
        :param metadata: metadata to update (merged with exisiting metadata
        :return: true if an entity was found and updated, false otherwise
        """

    def remove(self, file_path: str) -> None:
        """
        Remove all entries with the given file path
        :param file_path: the path of an image file
        """

        # NOTE: this query will only work if the index has been created
        # with a custom mapping for the path property:

        # # remove existing index
        # curl -X DELETE "192.168.2.24:9200/images"
        #
        # # create index with custom mapping for "path"
        # curl -X PUT "192.168.2.24:9200/images?pretty" -H "Content-Type: application/json" -d
        # "
        # {
        #   "mappings": {
        #     "image": {
        #       "properties": {
        #         "path": {
        #           "type": "keyword",
        #           "ignore_above": 256
        #         }
        #       }
        #     }
        #   }
        # }
        # "

        es_query = {
            'query': {
                "constant_score": {
                    "filter": {
                        "term": {'path': file_path}
                    }
                }
            }
        }

        self.__remove_by_query(es_query)

    def remove_entries_of_missing_files(self):
        """
        Remove all entries with files that don't exist
        """
        entries = self.get_all()
        for entry in entries:
            file_path = entry['path']
            if not os.path.exists(file_path):
                self.remove(file_path)

    def clear(self) -> None:
        """
        Remove all entries from Database
        """

        es_query = {
            'query': {'match_all': {}}
        }

        self.__remove_by_query(es_query)

    def __remove_by_query(self, es_query: dict):
        return self._store.es.delete_by_query(index=self.EL_INDEX, body=es_query, doc_type=self.EL_DOC_TYPE)
