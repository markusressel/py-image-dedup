import re
from pathlib import Path

from watchdog.events import FileSystemEventHandler, EVENT_TYPE_MODIFIED, EVENT_TYPE_MOVED, EVENT_TYPE_CREATED, \
    EVENT_TYPE_DELETED

from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.stats import FILE_EVENT_COUNT
from py_image_dedup.util import echo


class EventHandler(FileSystemEventHandler):

    def __init__(self, processing_manager):
        super().__init__()
        self.processing_manager = processing_manager

        self.config = DeduplicatorConfig()

        self.directory_regex = re.compile(rf"^({'|'.join(list(map(str, self.config.SOURCE_DIRECTORIES.value)))}).*$")
        self.file_regex = re.compile(rf"^.*({'|'.join(self.config.FILE_EXTENSION_FILTER.value)})$", re.IGNORECASE)

    def on_any_event(self, event):
        _actions = {
            EVENT_TYPE_CREATED: self.created,
            EVENT_TYPE_MODIFIED: self.modified,
            EVENT_TYPE_MOVED: self.moved,
            EVENT_TYPE_DELETED: self.deleted,
        }
        # other event types (f.ex. "opened", "closed") are irrelevant. They must not raise either:
        # an exception stops the observer thread, and with it all further file watching
        action = _actions.get(event.event_type)
        if action is None or not self._event_matches_filter(event):
            return

        FILE_EVENT_COUNT.labels(type=event.event_type).inc()

        echo("FileSystemEvent: {} {} {}".format(event.event_type,
                                                "directory" if event.is_directory else "file",
                                                event.src_path))

        action(event)

    def created(self, event):
        self._process(event.src_path)

    def modified(self, event):
        self._process(event.src_path)

    def moved(self, event):
        # a file can be renamed into or out of the filter (f.ex. "image.jpg.part" -> "image.jpg")
        if self._path_matches_filter(event.src_path):
            self._cleanup(event.src_path)
        if self._path_matches_filter(event.dest_path):
            self._process(event.dest_path)

    def deleted(self, event):
        self._cleanup(event.src_path)

    def _process(self, path: str):
        self.processing_manager.add(Path(path))

    def _cleanup(self, path: str):
        self.processing_manager.remove(Path(path))

    def _event_matches_filter(self, event) -> bool:
        # directory events are ignored: for new (or moved) directories watchdog emits
        # events for every file inside them, which are handled individually
        if event.is_directory:
            return False
        if event.event_type == EVENT_TYPE_MOVED:
            return self._path_matches_filter(event.src_path) or self._path_matches_filter(event.dest_path)
        return self._path_matches_filter(event.src_path)

    def _path_matches_filter(self, path: str) -> bool:
        return bool(self.directory_regex.match(path)) and bool(self.file_regex.match(path))
