import os
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.library import ActionEnum
from py_image_dedup.util.file import get_files_count


class ProcessingManager:
    config = DeduplicatorConfig()

    queue = OrderedDict()

    latest_event_time = None

    def __init__(self, deduplicator):
        self.deduplicator = deduplicator

    def add(self, path: Path):
        self.latest_event_time = datetime.now()
        if path not in self.queue:
            self.queue[path] = path

    def remove(self, path: Path):
        if path in self.queue:
            self.queue.pop(path)
        self.deduplicator._persistence.remove(str(path))

    def process_queue(self):
        try:
            while True:
                if (len(self.queue) <= 0
                        or self.latest_event_time is None
                        or datetime.now() - timedelta(seconds=10) < self.latest_event_time):
                    time.sleep(10)
                    continue

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

        except KeyboardInterrupt:
            pass
