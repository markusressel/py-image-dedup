import logging

import requests
from elasticsearch import Elasticsearch
from image_match.elasticsearch_driver import SignatureES

from py_image_dedup.persistence import ImageSignatureStore
from py_image_dedup.util import echo


class ElasticSearchStoreBackend(ImageSignatureStore):
    DEFAULT_DATABASE_HOST = "127.0.0.1"
    DEFAULT_DATABASE_PORT = 9200

    DEFAULT_EL_INDEX = 'images'
    DEFAULT_EL_DOC_TYPE = 'image'

    def __init__(self,
                 host: str = DEFAULT_DATABASE_HOST,
                 port: int = DEFAULT_DATABASE_PORT,
                 el_index: str = DEFAULT_EL_INDEX,
                 el_doctype: str = DEFAULT_EL_DOC_TYPE,
                 max_dist: float = 0.03,
                 use_exif_data: bool = True):
        """
        Image signature persistence backed by image_match and elasticsearch

        :param host: host address of the elasticsearch server
        :param port: port of the elasticsearch server
        :param el_index: elasticsearch index where the data is stored
        :param el_doctype: elasticsearch document type of the stored data
        :param max_dist: maximum "difference" allowed, ranging from [0 .. 1] where 0.2 is still a pretty similar image
        """
        super().__init__(use_exif_data)

        self.host = self.DEFAULT_DATABASE_HOST if host is None else host
        self.port = self.DEFAULT_DATABASE_PORT if port is None else port

        self._el_index = el_index
        self._el_doctype = el_doctype

        try:
            # self._clear_database()
            self._setup_database()
        except Exception as e:
            logging.exception(e)
            raise AssertionError("Could not setup database")

        # noinspection PyTypeChecker
        self._store = SignatureES(
            es=Elasticsearch(
                hosts=[
                    {'host': self.host, 'port': self.port}
                ]
            ),
            index=self._el_index,
            doc_type=self._el_doctype,
            distance_cutoff=max_dist
        )

    def _setup_database(self):
        response = requests.get('http://{}:{}/{}'.format(self.host, self.port, self._el_index))
        if response.status_code == 200:
            return
        elif response.status_code == 404:
            response = requests.put(
                url='http://{}:{}/{}'.format(self.host, self.port, self._el_index),
                json={
                    "mappings": {
                        "properties": {
                            "path": {
                                "type": "keyword",
                                "ignore_above": 256
                            }
                        }
                    }
                }
            )

            response.raise_for_status()
        else:
            response.raise_for_status()

    def _clear_database(self):
        requests.delete('http://{}:{}/{}'.format(self.host, self.port, self._el_index))

    def _add(self, image_file_path: str, image_data: dict) -> None:
        # remove existing entries
        self.remove(image_file_path)

        self._store.add_image(image_file_path, metadata=image_data)

    def get(self, image_file_path: str) -> dict or None:
        """
        Get a store entry by it's file_path
        :param image_file_path: file path to search for
        :return:
        """
        db_entity = self._get(image_file_path)
        return db_entity

    def _get(self, image_file_path: str) -> dict or None:
        """
        Get a store entry by it's file_path
        :param image_file_path: file path to search for
        :return: elasticsearch result dictionary
        """

        es_query = {
            'query': {
                "constant_score": {
                    "filter": {
                        "term": {'path': image_file_path}
                    }
                }
            }
        }

        query_result = self._store.es.search(self._el_index, body=es_query)

        hits = query_result['hits']['hits']

        if len(hits) > 1:
            echo("WARNING: More than a single entry for a file, cleaning up: %s" % image_file_path, color='yellow')
            self.remove(image_file_path)
            self.add(image_file_path)

        if len(hits) == 0:
            return None
        else:
            return hits[0]['_source']

    def get_all(self) -> (int, object):
        es_query = {
            "track_total_hits": True,
            'query': {'match_all': {}}
        }

        item_count = self._store.es.search(self._el_index, body=es_query, size=0)['hits']['total']['value']

        from elasticsearch.helpers import scan
        return item_count, scan(
            self._store.es,
            index=self._el_index,
            # disabled for elasticseach v7+
            # doc_type=self._el_doctype,
            preserve_order=True,
            query=es_query,
        )

    def find_similar(self, reference_image_file_path: str) -> []:
        try:
            entry = self._get(reference_image_file_path)
            if entry is not None:
                result = []
                rec = self._store.search_single_record(entry)
                result.extend(rec)

                return result
            else:
                return self._store.search_image(reference_image_file_path, all_orientations=True)
        except Exception as e:
            echo("Error querying database for similar images of '%s': %s" % (reference_image_file_path, e), color="red")
            return []

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

        return self._store.es.search(self._el_index, body=es_query)

    def remove(self, image_file_path: str) -> None:
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
                        "term": {'path': image_file_path}
                    }
                }
            }
        }

        self._remove_by_query(es_query)

    def remove_all(self) -> None:
        es_query = {
            'query': {'match_all': {}}
        }

        self._remove_by_query(es_query)

    def _remove_by_query(self, es_query: dict):
        return self._store.es.delete_by_query(
            index=self._el_index,
            body=es_query,
            # disabled for elasticseach v7+
            # doc_type=self._el_doctype
        )
