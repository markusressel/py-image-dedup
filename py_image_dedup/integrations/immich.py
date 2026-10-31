import logging
from typing import List

import requests

LOGGER = logging.getLogger(__name__)

# trashedAfter needs a value, every trashed asset was trashed after this
_EPOCH = "1970-01-01T00:00:00.000Z"


class ImmichError(Exception):
    pass


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
        paths = []
        page = 1
        while page is not None:
            assets = self._post("/api/search/metadata", {
                "withDeleted": True,
                "trashedAfter": _EPOCH,
                "page": page,
                "size": self._page_size,
            })["assets"]
            for asset in assets["items"]:
                if asset.get("isTrashed") and not asset.get("isOffline"):
                    paths.append(asset["originalPath"])
            next_page = assets.get("nextPage")
            page = int(next_page) if next_page else None
        return paths

    def _post(self, path: str, body: dict) -> dict:
        try:
            response = self._session.post(self._url + path, json=body, timeout=self._timeout,
                                          headers={"x-api-key": self._api_key, "Accept": "application/json"})
        except requests.RequestException as ex:
            raise ImmichError(f"Immich is not reachable at {self._url}: {ex}") from ex
        if response.status_code in (401, 403):
            raise ImmichError(f"Immich rejected the API key (HTTP {response.status_code}), "
                              f"it needs the asset.read permission")
        if not response.ok:
            raise ImmichError(f"Immich request {path} failed: HTTP {response.status_code} {response.text[:200]}")
        return response.json()
