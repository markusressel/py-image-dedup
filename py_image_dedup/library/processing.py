import logging
import os
from queue import Queue

from py_image_dedup.config.deduplicator_config import DeduplicatorConfig
from py_image_dedup.util import echo
from py_image_dedup.util.file import get_files_count, get_containing_folder


class ProcessingManager:
    config = DeduplicatorConfig()

    queued_paths = {}
    queue = Queue()

    def __init__(self, deduplicator):
        self.deduplicator = deduplicator

    def add(self, path):
        if path not in self.queued_paths:
            self.queued_paths[path] = None
            self.queue.put(path)

    def process_queue(self):
        try:
            while True:
                path = self.queue.get(block=True)
                if os.path.isdir(path):
                    files_count = get_files_count(
                        path,
                        self.config.RECURSIVE.value,
                        self.config.FILE_EXTENSION_FILTER.value
                    )
                    directory_map = {
                        path: files_count
                    }

                    self.deduplicator.analyze_directories(directory_map)
                    self.deduplicator.find_duplicates(directory_map)
                    self.deduplicator.process_duplicates()

                if os.path.isfile(path):
                    try:
                        self.deduplicator._persistence.add(path)
                    except Exception as e:
                        logging.exception(e)
                        echo("Error analyzing file '%s': %s" % (path, e))

                    folder_path = get_containing_folder(path)

                    files_count = get_files_count(
                        folder_path,
                        self.config.RECURSIVE.value,
                        self.config.FILE_EXTENSION_FILTER.value
                    )
                    directory_map = {
                        path: files_count
                    }

                    self.deduplicator.find_duplicates(directory_map)
                    self.deduplicator.process_duplicates()

                self.queued_paths.pop(path)
        except KeyboardInterrupt:
            pass
