import logging
import threading

import uvicorn
from fastapi import FastAPI

LOGGER = logging.getLogger(__name__)


class WebServer:
    """
    Runs the web UI in a background thread of the daemon process.
    """

    def __init__(self, app: FastAPI, host: str, port: int):
        self._server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                                     access_log=False))
        self._thread = threading.Thread(target=self._server.run, name="py-image-dedup-web", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self, timeout: float = 5):
        self._server.should_exit = True
        self._thread.join(timeout)
