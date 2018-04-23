import os
import pickle


class IdentifierStore:

    def __init__(self, db_path: str):
        d = {"abc": [1, 2, 3], "qwerty": [4, 5, 6]}

        self._store = None
        self._db_file_path = os.path.join(db_path, "hash_db.pkl")
        self._db_file = None

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

        if not self._store:
            self.load()

        if key in self._store:
            return self._store[key]
        else:
            return None

    def set(self, key: str, value: str):
        """
        Set a value in the store
        """
        self._store[key] = value

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
