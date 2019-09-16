from watchdog.events import FileSystemEventHandler

from py_image_dedup.library.processing import ProcessingManager
from py_image_dedup.util import echo


class EventHandler(FileSystemEventHandler):

    def __init__(self, processing_manager: ProcessingManager):
        super().__init__()
        self.processing_manager = processing_manager

    def on_any_event(self, event):
        echo("FileEvent: {} {}".format(event.event_type, event.src_path))

    def on_created(self, event):
        file_path = event.src_path
        self.processing_manager.add(file_path)

    def on_modified(self, event):
        file_path = event.src_path
        self.processing_manager.add(file_path)

    def on_moved(self, event):
        # TODO: remove old file(s) from database
        old_path = event.src_path
        new_path = event.est_path

        if event.is_directory:
            # TODO: remove data with path pattern and add the new folder
            return

        self.processing_manager.add(new_path)

    def on_deleted(self, event):
        # TODO: remove item from database
        pass
