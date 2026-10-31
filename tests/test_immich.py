import unittest
from unittest import mock

import requests

from py_image_dedup.integrations.immich import ImmichClient, ImmichError


def _response(status_code: int, data: dict | None = None) -> mock.Mock:
    response = mock.Mock(status_code=status_code, ok=200 <= status_code < 300, text="",
                         content=b"" if data is None else b"{}")
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
        self.session.request.side_effect = [
            _response(200, _page([{"originalPath": "/mnt/photos/a.jpg", "isTrashed": True, "isOffline": False},
                                  {"originalPath": "/mnt/photos/b.jpg", "isTrashed": False, "isOffline": False}],
                                 "2")),
            _response(200, _page([{"originalPath": "/mnt/photos/c.jpg", "isTrashed": True, "isOffline": True},
                                  {"originalPath": "/mnt/photos/d.jpg", "isTrashed": True}], None)),
        ]

        self.assertEqual(["/mnt/photos/a.jpg", "/mnt/photos/d.jpg"], self.client.trashed_original_paths())

        first, second = self.session.request.call_args_list
        self.assertEqual(("POST", "http://immich:2283/api/search/metadata"), first.args)
        self.assertEqual("key", first.kwargs["headers"]["x-api-key"])
        self.assertTrue(first.kwargs["json"]["withDeleted"])
        self.assertEqual((1, 2), (first.kwargs["json"]["page"], second.kwargs["json"]["page"]))

    def test_errors(self):
        self.session.request.return_value = _response(401)
        with self.assertRaisesRegex(ImmichError, "API key"):
            self.client.trashed_original_paths()

        self.session.request.side_effect = requests.ConnectionError("refused")
        with self.assertRaisesRegex(ImmichError, "not reachable"):
            self.client.trashed_original_paths()

    def test_map_path(self):
        self.assertEqual("/data/photos/x/a.jpg", self.client.map_path("/mnt/photos/x/a.jpg"))
        self.assertIsNone(self.client.map_path("/mnt/photos2/a.jpg"))
        self.assertIsNone(self.client.map_path("/upload/a.jpg"))
        self.assertEqual("/upload/a.jpg", ImmichClient("http://immich", "key").map_path("/upload/a.jpg"))

    def test_to_immich_path(self):
        self.assertEqual("/mnt/photos/x/a.jpg", self.client.to_immich_path("/data/photos/x/a.jpg"))
        self.assertEqual("/mnt/photos", self.client.to_immich_path("/data/photos"))
        self.assertIsNone(self.client.to_immich_path("/data/photos2/a.jpg"))
        self.assertEqual("/a.jpg", ImmichClient("http://immich", "key").to_immich_path("/a.jpg"))

    def test_find_asset_ids(self):
        self.session.request.return_value = _response(200, _page(
            [{"id": "1", "originalPath": "/mnt/photos/a.jpg"}, {"id": "2", "originalPath": "/mnt/photos/b.jpg"}],
            None))

        self.assertEqual({"/mnt/photos/a.jpg": "1", "/mnt/photos/b.jpg": "2"},
                         self.client.find_asset_ids(["/mnt/photos/b.jpg", "/mnt/photos/a.jpg", "/mnt/photos/c.jpg"]))
        body = self.session.request.call_args.kwargs["json"]
        self.assertEqual({"in": ["/mnt/photos/a.jpg", "/mnt/photos/b.jpg", "/mnt/photos/c.jpg"]},
                         body["filter"]["originalPath"])
        self.assertEqual({}, self.client.find_asset_ids([]))

    def test_find_asset_ids_falls_back_to_deprecated_parameter(self):
        # Immich < v3.2.0 does not know the "filter" parameter, "originalPath" matches substrings there
        self.session.request.side_effect = [
            _response(400),
            _response(200, _page([{"id": "1", "originalPath": "/mnt/photos/a.jpg"},
                                  {"id": "9", "originalPath": "/mnt/photos/a.jpg.bak/x.jpg"}], None)),
        ]

        self.assertEqual({"/mnt/photos/a.jpg": "1"}, self.client.find_asset_ids(["/mnt/photos/a.jpg"]))
        self.assertEqual("/mnt/photos/a.jpg", self.session.request.call_args.kwargs["json"]["originalPath"])

    def test_rescan_libraries(self):
        libraries = [{"id": "lib-1", "importPaths": ["/mnt/photos/"]}, {"id": "lib-2", "importPaths": ["/other"]}]
        self.session.request.side_effect = [_response(200, libraries), _response(204)]

        self.assertEqual(["lib-1"], self.client.rescan_libraries(["/mnt/photos/x/a.jpg"]))
        list_call, scan_call = self.session.request.call_args_list
        self.assertEqual(("GET", "http://immich:2283/api/libraries"), list_call.args)
        self.assertEqual(("POST", "http://immich:2283/api/libraries/lib-1/scan"), scan_call.args)

    def test_rescan_libraries_scans_all_if_none_matches(self):
        libraries = [{"id": "lib-1", "importPaths": ["/mnt/photos"]}, {"id": "lib-2", "importPaths": []}]
        self.session.request.side_effect = [_response(200, libraries), _response(204), _response(204)]

        self.assertEqual(["lib-1", "lib-2"], self.client.rescan_libraries(["/elsewhere/a.jpg"]))

    def test_rescan_libraries_needs_admin_permissions(self):
        self.session.request.return_value = _response(403)
        with self.assertRaisesRegex(ImmichError, "library.read"):
            self.client.rescan_libraries(["/mnt/photos/a.jpg"])
