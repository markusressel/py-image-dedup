import os
import pickle


class IdentifierStore:
    # counter used to track how many new values were set
    # and limit the amount of disk writes
    __SET_CALL_COUNTER = 0

    def __init__(self, db_path: str):
        self._store = None
        self._db_file_path = os.path.join(db_path, "py-image-dedup_db.pkl")
        self._db_file = None

        self._store = self.load()
        self._cleanup()

    def _open_db(self, mode: str) -> object:
        try:
            return open(r'%s' % self._db_file_path, mode)
        except FileNotFoundError:
            self._write({})

        return open(r'%s' % self._db_file_path, mode)

    def _close_db(self):
        if self._db_file:
            self._db_file.close()

    def _write(self, value):
        db = self._open_db('wb')
        pickle.dump(value, db)
        self._close_db()

    def load(self):
        db = self._open_db('rb')
        self._store = pickle.load(db)
        self._close_db()

        return self._store

    def get(self, key: str) -> str or None:
        """
        Get a stored value
        :param key:
        :return:
        """

        if key in self._store:
            return self._store[key]
        else:
            return None

    def set(self, key: str, value: {}):
        """
        Set a value in the store
        """
        self._store[key] = value

        # autosave if enough new values were stored
        self.__SET_CALL_COUNTER += 1
        if self.__SET_CALL_COUNTER % 50 == 0:
            self.save()

    def set_all(self, hashes: {str, str}):
        """
        :return:
        """
        self._store = hashes

    def save(self):
        """
        Store hashes in persistence
        """

        self._write(self._store)

    def _cleanup(self):
        cleaned = {}
        for key, value in self._store.items():
            if os.path.exists(key) and os.path.isfile(key):
                cleaned[key] = value
        self._store = cleaned

        self.save()
