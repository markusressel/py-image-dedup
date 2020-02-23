import os
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import List

from watchdog.observers.inotify import InotifyObserver
from watchdog.observers.polling import PollingObserver

from py_image_dedup.config import DeduplicatorConfig, FILE_OBSERVER_TYPE_INOTIFY, FILE_OBSERVER_TYPE_POLLING
from py_image_dedup.library import ActionEnum, RegularIntervalWorker
from py_image_dedup.library.file_watch import EventHandler
from py_image_dedup.util.file import get_files_count


class ProcessingManager(RegularIntervalWorker):
    lock = Lock()
    queue = OrderedDict()

    latest_event_time = None

    def __init__(self, deduplicator):
        self.config = DeduplicatorConfig()
        timeout = self.config.DAEMON_PROCESSING_TIMEOUT.value
        interval = timeout.total_seconds()
        super().__init__(interval)
        self.deduplicator = deduplicator
        self.event_handler = EventHandler(self)
        self.observers = []

    def start(self):
        observer_type = self.config.DAEMON_FILE_OBSERVER_TYPE.value
        directories = self.config.SOURCE_DIRECTORIES.value
        self.observers = self._setup_file_observers(observer_type, directories)
        super().start()

    def stop(self):
        for observer in self.observers:
            observer.stop()
            observer.join()

        self.observers.clear()

    def _setup_file_observers(self, observer_type: str, source_directories: List[Path]):
        observers = []

        for directory in source_directories:
            if observer_type == FILE_OBSERVER_TYPE_INOTIFY:
                observer = InotifyObserver()
            elif observer_type == FILE_OBSERVER_TYPE_POLLING:
                observer = PollingObserver()
            else:
                raise ValueError(f"Unexpected file observer type {observer_type}")

            observer.schedule(self.event_handler, str(directory), recursive=True)
            observer.start()
            observers.append(observer)

        return observers

    def add(self, path: Path):
        with self.lock:
            self.latest_event_time = datetime.now()
            if path not in self.queue:
                self.queue[path] = path

    def remove(self, path: Path):
        if path in self.queue:
            self.queue.pop(path)
        self.deduplicator._persistence.remove(str(path))

    def _should_process(self):
        return not (len(self.queue) <= 0
                    or self.latest_event_time is None
                    or datetime.now() - timedelta(seconds=10) < self.latest_event_time)

    def _run(self):
        with self.lock:
            self.process_queue()

    def process_queue(self):
        if not self._should_process():
            return

        # TODO: only handling file events individually seems painfully slow
        # TODO: maybe try to aggregate multiple events that happen in quick succession into badges

        path, value = self.queue.popitem()
        if path.is_dir():
            self.deduplicator.reset_result()

            files_count = get_files_count(
                path,
                self.config.RECURSIVE.value,
                self.config.FILE_EXTENSION_FILTER.value
            )
            directory_map = {
                path: files_count
            }

            self.deduplicator.analyze_directories(directory_map)
            self.deduplicator.find_duplicates_in_directories(directory_map)
            self.deduplicator.process_duplicates()

        if path.is_file():
            self.deduplicator._create_file_progressbar(1)
            self.deduplicator.reset_result()
            self.deduplicator.analyze_file(path)
            root_dir = Path(os.path.commonpath([path] + self.config.SOURCE_DIRECTORIES.value))
            self.deduplicator.find_duplicates_of_file(self.config.SOURCE_DIRECTORIES.value, root_dir, path)
            self.deduplicator.process_duplicates()
            # remove items that have been (re-)moved already from the event queue
            removed_items = self.deduplicator._deduplication_result.get_file_with_action(ActionEnum.DELETE)
            moved_items = self.deduplicator._deduplication_result.get_file_with_action(ActionEnum.MOVE)
            for item in removed_items + moved_items:
                if item in self.queue:
                    self.queue.pop(item)
