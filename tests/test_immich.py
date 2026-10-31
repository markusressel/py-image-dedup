import unittest
from unittest import mock

import requests

from py_image_dedup.integrations.immich import ImmichClient, ImmichError


def _response(status_code: int, data: dict | None = None) -> mock.Mock:
    response = mock.Mock(status_code=status_code, ok=200 <= status_code < 300, text="")
    response.json.return_value = data
    return response


def _page(items: list, next_page: str | None) -> dict:
    return {"albums": {}, "assets": {"items": items, "nextPage": next_page}}


class ImmichClientTest(unittest.TestCase):

    def setUp(self):
        self.session = mock.Mock(spec=requests.Session)
        self.client = ImmichClient("http://immich:2283/", "key", session=self.session, page_size=2,
                                   path_mappings=[{"immich": "/mnt/photos/", "local": "/data/photos"}])

    def test_trashed_original_paths_pages_and_filters(self):
        self.session.post.side_effect = [
            _response(200, _page([{"originalPath": "/mnt/photos/a.jpg", "isTrashed": True, "isOffline": False},
                                  {"originalPath": "/mnt/photos/b.jpg", "isTrashed": False, "isOffline": False}],
                                 "2")),
            _response(200, _page([{"originalPath": "/mnt/photos/c.jpg", "isTrashed": True, "isOffline": True},
                                  {"originalPath": "/mnt/photos/d.jpg", "isTrashed": True}], None)),
        ]

        self.assertEqual(["/mnt/photos/a.jpg", "/mnt/photos/d.jpg"], self.client.trashed_original_paths())

        first, second = self.session.post.call_args_list
        self.assertEqual("http://immich:2283/api/search/metadata", first.args[0])
        self.assertEqual("key", first.kwargs["headers"]["x-api-key"])
        self.assertTrue(first.kwargs["json"]["withDeleted"])
        self.assertEqual((1, 2), (first.kwargs["json"]["page"], second.kwargs["json"]["page"]))

    def test_errors(self):
        self.session.post.return_value = _response(401)
        with self.assertRaisesRegex(ImmichError, "API key"):
            self.client.trashed_original_paths()

        self.session.post.side_effect = requests.ConnectionError("refused")
        with self.assertRaisesRegex(ImmichError, "not reachable"):
            self.client.trashed_original_paths()

    def test_map_path(self):
        self.assertEqual("/data/photos/x/a.jpg", self.client.map_path("/mnt/photos/x/a.jpg"))
        self.assertIsNone(self.client.map_path("/mnt/photos2/a.jpg"))
        self.assertIsNone(self.client.map_path("/upload/a.jpg"))
        self.assertEqual("/upload/a.jpg", ImmichClient("http://immich", "key").map_path("/upload/a.jpg"))
