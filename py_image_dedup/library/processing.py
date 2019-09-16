import os
from queue import Queue

from py_image_dedup.config.deduplicator_config import DeduplicatorConfig
from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
from py_image_dedup.util.file import get_files_count, get_containing_folder


class ProcessingManager:
    config = DeduplicatorConfig()
    deduplicator = ImageMatchDeduplicator(config)

    file_queue = Queue()

    def add(self, file_path):
        self.file_queue.put(file_path)

    def process_queue(self):
        try:
            while True:
                item = self.file_queue.get(block=True)
                if os.path.isfile(item):
                    item = get_containing_folder(item)

                if os.path.isdir(item):
                    files_count = get_files_count(
                        item,
                        self.config.RECURSIVE.value,
                        self.config.FILE_EXTENSION_FILTER.value
                    )
                    directory_map = {
                        item: files_count
                    }

                    self.deduplicator.analyze_directories(directory_map)
                    self.deduplicator.deduplicate_directories(directory_map)
        except KeyboardInterrupt:
            pass
