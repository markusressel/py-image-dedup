import os
import pickle

from py_image_dedup.persistence import ImageSignatureStore, StoreEntry


class FileStoreBackend(ImageSignatureStore):
    # counter used to track how many new values were set
    # and limit the amount of disk writes
    __SET_CALL_COUNTER = 0

    def __init__(self, db_path: str):
        """
        Image signature persistence backed by pythons Pickle file storage

        :param db_path: Path (not a file) where the database file will be located
        """
        super().__init__()

        self._store: dict = None
        self._db_file_path: str = os.path.join(db_path, "py-image-dedup_db.pkl")
        self._db_file = None

        self._store = self.load()

    def _open_db(self, mode: str) -> object:
        try:
            return open(r'%s' % self._db_file_path, mode)
        except FileNotFoundError:
            self._write_all({})

        return open(r'%s' % self._db_file_path, mode)

    def _close_db(self):
        if self._db_file:
            self._db_file.close()

    def _write_all(self, value):
        db = self._open_db('wb')
        pickle.dump(value, db)
        self._close_db()

    def _write_append(self, value):
        db = self._open_db('a+b')
        pickle.dump(value, db)
        self._close_db()

    def load(self):
        db = self._open_db('rb')
        self._store = pickle.load(db)
        self._close_db()

        return self._store

    def _add(self, image_file_path: str, image_data: dict) -> None:
        """
        Set a value in the store
        """
        self._store[image_file_path] = image_data
        self._write_append()

        # autosave if enough new values were stored
        self.__SET_CALL_COUNTER += 1
        if self.__SET_CALL_COUNTER % 50 == 0:
            self.save()

    def get(self, file_path: str) -> StoreEntry or None:
        if file_path in self._store:
            return self._store[file_path]
        else:
            return None

    def get_all(self) -> [StoreEntry]:
        pass

    def find_similar(self, reference_image_file_path: str) -> []:
        pass

    def remove(self, image_file_path: str) -> None:
        if image_file_path in self._store:
            self._store.pop(image_file_path)

        pass

    def remove_all(self) -> None:
        pass

    def save(self):
        """
        Store hashes in persistence
        """

        self._write_all(self._store)
