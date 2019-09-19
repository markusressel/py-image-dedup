import os
from pathlib import Path
from queue import Queue

from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.util.file import get_files_count


class ProcessingManager:
    config = DeduplicatorConfig()

    queued_paths = {}
    queue = Queue()

    def __init__(self, deduplicator):
        self.deduplicator = deduplicator

    def add(self, path: Path):
        if path not in self.queued_paths:
            self.queued_paths[path] = None
            self.queue.put(path)

    def process_queue(self):
        try:
            while True:
                # TODO: only handling file events individually seems painfully slow
                # TODO: maybe try to aggregate multiple events that happen in quick succession into badges

                path: Path = self.queue.get(block=True)
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
                    # TODO: there is still some weird behaviour when moving a couple of files in a short amount of time

                    self.deduplicator._create_file_progressbar(1)
                    self.deduplicator.reset_result()
                    self.deduplicator.analyze_file(path)
                    root_dir = Path(os.path.commonpath([path] + self.config.SOURCE_DIRECTORIES.value))
                    self.deduplicator.find_duplicates_of_file(self.config.SOURCE_DIRECTORIES.value, root_dir, path)
                    self.deduplicator.process_duplicates()

                self.queued_paths.pop(path)
        except KeyboardInterrupt:
            pass
