"""
PostgreSQL signature store (optional, install with the `postgres` extra: `pip install py-image-dedup[postgres]`).

Same record semantics as the SQLite store. Postgres is only used for persistence, the hash similarity search
still loads all signatures and runs in Python/numpy. Tables are prefixed with `matching_` so the store can live
in a database that is shared with other applications.
"""
import logging
from threading import RLock
from typing import Dict, Iterable, Iterator, List, Optional

from py_image_dedup.matching.models import ImageSignature
from py_image_dedup.matching.store import SCHEMA_VERSION, SignatureStore, _COLUMNS

LOGGER = logging.getLogger(__name__)

FILES_TABLE = "matching_files"
META_TABLE = "matching_meta"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {META_TABLE} (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {FILES_TABLE} (
    path TEXT PRIMARY KEY,
    file_size BIGINT NOT NULL,
    mtime_ns BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    preview_pixel_hash TEXT NOT NULL,
    full_pixel_hash TEXT,
    phash BIGINT NOT NULL,
    dhash BIGINT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    is_grayscale BOOLEAN NOT NULL,
    capture_time TEXT,
    capture_time_has_subsec BOOLEAN NOT NULL DEFAULT FALSE,
    camera_make TEXT,
    camera_model TEXT,
    image_unique_id TEXT,
    software TEXT,
    signature_version INTEGER NOT NULL,
    orientation_hashes BYTEA,
    colour_moments TEXT,
    analysed_at BIGINT NOT NULL DEFAULT (extract(epoch FROM now())::BIGINT)
);
CREATE INDEX IF NOT EXISTS {FILES_TABLE}_sha256 ON {FILES_TABLE} (sha256);
CREATE INDEX IF NOT EXISTS {FILES_TABLE}_preview_pixel_hash ON {FILES_TABLE} (preview_pixel_hash);
"""

# rows fetched per round trip when streaming all signatures
_FETCH_SIZE = 5000


def _import_psycopg():
    """
    Imports psycopg lazily, so the package works without the optional dependency.

    :return: (psycopg module, dict_row row factory)
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as ex:
        raise ImportError("The Postgres signature store requires psycopg, "
                          "install py-image-dedup with the 'postgres' extra") from ex
    return psycopg, dict_row


class PostgresSignatureStore(SignatureStore):
    """
    PostgreSQL implementation using psycopg 3 (one connection, serialised by a lock).
    """

    def __init__(self, dsn: str):
        """
        :param dsn: libpq connection string or URI, e.g. postgresql://user:password@host:5432/database
        """
        psycopg, dict_row = _import_psycopg()
        self._lock = RLock()
        self._connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        with self._lock, self._connection.transaction():
            # serialise schema creation of processes starting at the same time
            self._connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (FILES_TABLE,))
            self._connection.execute(_SCHEMA)
            version = self._read_schema_version()
            if version is not None and version != SCHEMA_VERSION:
                LOGGER.warning(f"Signature store has schema version {version}, expected {SCHEMA_VERSION}, "
                               f"dropping it (all files will be re-analysed)")
                self._connection.execute(f"DROP TABLE {FILES_TABLE}")
                self._connection.execute(_SCHEMA)
            self._connection.execute(
                f"INSERT INTO {META_TABLE} (key, value) VALUES ('schema_version', %s) "
                f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(SCHEMA_VERSION),))

    def _read_schema_version(self) -> Optional[int]:
        row = self._connection.execute(f"SELECT value FROM {META_TABLE} WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row is not None else None

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._read_schema_version()

    def close(self):
        with self._lock:
            self._connection.close()

    def upsert(self, signatures: Iterable[ImageSignature]):
        rows = [self._to_row(signature) for signature in signatures]
        if not rows:
            return
        placeholders = ", ".join("%s" for _ in _COLUMNS)
        updates = ", ".join(f"{column} = EXCLUDED.{column}" for column in _COLUMNS if column != "path")
        query = (f"INSERT INTO {FILES_TABLE} ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
                 f"ON CONFLICT (path) DO UPDATE SET {updates}, analysed_at = DEFAULT")
        with self._lock, self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.executemany(query, rows)

    def set_full_pixel_hash(self, path: str, full_pixel_hash: str):
        with self._lock:
            self._connection.execute(f"UPDATE {FILES_TABLE} SET full_pixel_hash = %s WHERE path = %s",
                                     (full_pixel_hash, path))

    def get(self, path: str) -> Optional[ImageSignature]:
        with self._lock:
            row = self._connection.execute(f"SELECT * FROM {FILES_TABLE} WHERE path = %s", (path,)).fetchone()
        return self._from_row(row) if row is not None else None

    def get_all(self) -> List[ImageSignature]:
        return list(self.iter_all())

    def iter_all(self) -> Iterator[ImageSignature]:
        # server side cursor: rows are fetched in batches instead of loading the whole result at once,
        # the lock is held until the iterator is exhausted or closed
        with self._lock, self._connection.transaction(), \
                self._connection.cursor(name="matching_iter_all") as cursor:
            cursor.itersize = _FETCH_SIZE
            cursor.execute(f"SELECT * FROM {FILES_TABLE} ORDER BY path")
            for row in cursor:
                yield self._from_row(row)

    def get_file_states(self) -> Dict[str, tuple]:
        with self._lock:
            rows = self._connection.execute(
                f"SELECT path, file_size, mtime_ns, signature_version FROM {FILES_TABLE}").fetchall()
        return {row["path"]: (row["file_size"], row["mtime_ns"], row["signature_version"]) for row in rows}

    def remove(self, paths: Iterable[str]):
        paths = list(paths)
        if not paths:
            return
        with self._lock:
            self._connection.execute(f"DELETE FROM {FILES_TABLE} WHERE path = ANY(%s)", (paths,))

    def count(self) -> int:
        with self._lock:
            return self._connection.execute(f"SELECT COUNT(*) AS n FROM {FILES_TABLE}").fetchone()["n"]
