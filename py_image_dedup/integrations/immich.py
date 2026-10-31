import logging
from typing import Dict, List

import requests

LOGGER = logging.getLogger(__name__)

# trashedAfter needs a value, every trashed asset was trashed after this
_EPOCH = "1970-01-01T00:00:00.000Z"


class ImmichError(Exception):

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ImmichClient:
    """
    Minimal client for the Immich API (https://immich.app/docs/api).
    """

    def __init__(self, url: str, api_key: str, path_mappings: List[dict] | None = None, timeout: float = 30,
                 page_size: int = 1000, session: requests.Session | None = None):
        """
        :param url: base URL of the Immich server, f.ex. "http://immich-server:2283"
        :param api_key: API key with the asset.read permission
        :param path_mappings: list of {"immich": "/mnt/photos", "local": "/data/photos"}
        :param timeout: timeout of a single request in seconds
        :param page_size: number of assets requested per page
        :param session: requests session to use
        """
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._path_mappings = [(str(m["immich"]).rstrip("/"), str(m["local"]).rstrip("/"))
                               for m in path_mappings or []]
        self._timeout = timeout
        self._page_size = page_size
        self._session = session or requests.Session()

    def map_path(self, path: str) -> str | None:
        """
        Maps a path reported by Immich to the local path.
        :return: the local path, None if there are mappings and none of them matches
        """
        if not self._path_mappings:
            return path
        for immich_prefix, local_prefix in self._path_mappings:
            if path == immich_prefix or path.startswith(immich_prefix + "/"):
                return local_prefix + path[len(immich_prefix):]
        return None

    def trashed_original_paths(self) -> List[str]:
        """
        :return: original file paths (as reported by Immich) of all assets in the trash
                 of the API key's user, excluding offline assets (files missing on disk)
        """
        items = self._search_items({"withDeleted": True, "trashedAfter": _EPOCH})
        return [item["originalPath"] for item in items if item.get("isTrashed") and not item.get("isOffline")]

    def to_immich_path(self, path: str) -> str | None:
        """
        Maps a local path to the path Immich knows the file under (inverse of map_path).
        :return: the Immich path, None if there are mappings and none of them matches
        """
        if not self._path_mappings:
            return path
        for immich_prefix, local_prefix in self._path_mappings:
            if path == local_prefix or path.startswith(local_prefix + "/"):
                return immich_prefix + path[len(local_prefix):]
        return None

    def find_asset_ids(self, immich_paths: List[str]) -> Dict[str, str]:
        """
        Looks up the (not trashed) assets of the API key's user with the given original paths.
        Needs the asset.read permission.

        :param immich_paths: original paths as Immich knows them
        :return: original path -> asset id, paths without asset are missing
        """
        paths = sorted(set(immich_paths))
        if not paths:
            return {}
        try:
            # structured filter, Immich >= v3.2.0
            items = self._search_items({"filter": {"originalPath": {"in": paths}}})
        except ImmichError as ex:
            if ex.status_code not in (400, 422):
                raise
            # older Immich: the deprecated originalPath parameter matches substrings, one request per path
            items = []
            for path in paths:
                items.extend(self._search_items({"originalPath": path}))
        wanted = set(paths)
        return {item["originalPath"]: item["id"] for item in items if item.get("originalPath") in wanted}

    def _search_items(self, body: dict) -> List[dict]:
        items = []
        page = 1
        while page is not None:
            assets = self._post("/api/search/metadata", {**body, "page": page, "size": self._page_size},
                                permission="asset.read")["assets"]
            items.extend(assets["items"])
            next_page = assets.get("nextPage")
            page = int(next_page) if next_page else None
        return items

    def libraries(self) -> List[dict]:
        """
        :return: all external libraries (needs an admin API key with the library.read permission)
        """
        return self._request("GET", "/api/libraries", permission="library.read (admin)")

    def scan_library(self, library_id: str):
        """
        Queues a scan of an external library (needs an admin API key with the library.update permission).
        """
        self._request("POST", f"/api/libraries/{library_id}/scan", permission="library.update (admin)")

    def rescan_libraries(self, changed_immich_paths: List[str]) -> List[str]:
        """
        Queues a scan of the external libraries containing one of the given paths,
        of all external libraries if none of them matches (f.ex. because the path mappings are incomplete).

        :param changed_immich_paths: paths (as Immich knows them) of files that were moved or deleted
        :return: ids of the libraries a scan was queued for
        """
        libraries = self.libraries()

        def contains(library: dict, path: str) -> bool:
            return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
                       for p in library.get("importPaths") or [])

        affected = [lib for lib in libraries if any(contains(lib, p) for p in changed_immich_paths)]
        if not affected:
            affected = libraries
        for library in affected:
            self.scan_library(library["id"])
        return [lib["id"] for lib in affected]

    def _post(self, path: str, body: dict, permission: str = "asset.read") -> dict:
        return self._request("POST", path, body=body, permission=permission)

    def _request(self, method: str, path: str, body: dict | None = None, permission: str = "asset.read"):
        try:
            response = self._session.request(method, self._url + path, json=body, timeout=self._timeout,
                                             headers={"x-api-key": self._api_key, "Accept": "application/json"})
        except requests.RequestException as ex:
            raise ImmichError(f"Immich is not reachable at {self._url}: {ex}") from ex
        if response.status_code in (401, 403):
            raise ImmichError(f"Immich rejected the API key (HTTP {response.status_code}), "
                              f"it needs the {permission} permission", status_code=response.status_code)
        if not response.ok:
            raise ImmichError(f"Immich request {path} failed: HTTP {response.status_code} {response.text[:200]}",
                              status_code=response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()
