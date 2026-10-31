"""
Signature stores of the matching pipeline: one record per file with its hashes and matching metadata.

`SignatureStore` is the interface the pipeline uses, `SqliteSignatureStore` the default implementation
(own database file, independent of the web UI state store) and `PostgresSignatureStore`
(`store_postgres.py`, optional `postgres` extra) an alternative for a shared database server.
Stores only persist; the hash similarity search runs in Python/numpy (`search.py`) over `get_all()`.
Use `open_signature_store(url)` to create a store from a URL like setting.
"""
import logging
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Dict, Iterable, Iterator, List, Mapping, Optional

from py_image_dedup.matching.models import ImageMetadata, ImageSignature, SIGNATURE_VERSION
from py_image_dedup.matching.orientation import decode_orientation_hashes, encode_orientation_hashes

LOGGER = logging.getLogger(__name__)

# bump when the table layout changes; signatures are a cache, so a store with another version is rebuilt
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    file_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    preview_pixel_hash TEXT NOT NULL,
    full_pixel_hash TEXT,
    phash INTEGER NOT NULL,
    dhash INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    is_grayscale INTEGER NOT NULL,
    capture_time TEXT,
    capture_time_has_subsec INTEGER NOT NULL DEFAULT 0,
    camera_make TEXT,
    camera_model TEXT,
    image_unique_id TEXT,
    software TEXT,
    signature_version INTEGER NOT NULL,
    orientation_hashes BLOB,
    colour_moments TEXT,
    analysed_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS files_sha256 ON files (sha256);
CREATE INDEX IF NOT EXISTS files_preview_pixel_hash ON files (preview_pixel_hash);
"""

_COLUMNS = (
    "path", "file_size", "mtime_ns", "sha256", "preview_pixel_hash", "full_pixel_hash", "phash", "dhash",
    "width", "height", "is_grayscale", "capture_time", "capture_time_has_subsec", "camera_make", "camera_model",
    "image_unique_id", "software", "signature_version", "orientation_hashes", "colour_moments",
)


def _to_signed(value: int) -> int:
    """
    SQLite integers are signed 64 bit.

    :param value: unsigned 64 bit int
    :return: the same bits as signed int
    """
    return value - (1 << 64) if value >= (1 << 63) else value


def _to_unsigned(value: int) -> int:
    """
    :param value: signed 64 bit int
    :return: the same bits as unsigned int
    """
    return value + (1 << 64) if value < 0 else value


class SignatureStore(ABC):
    """
    Persistence of image signatures and matching metadata, one record per file (keyed by path).

    Implementations must be safe to use from multiple threads.
    For backwards compatibility `SignatureStore(db_path)` creates a `SqliteSignatureStore`.
    """

    def __new__(cls, *args, **kwargs):
        if cls is SignatureStore:
            cls = SqliteSignatureStore
        return super().__new__(cls)

    @property
    @abstractmethod
    def schema_version(self) -> int:
        """
        :return: the schema version of the opened store (equal to SCHEMA_VERSION after opening)
        """

    @abstractmethod
    def close(self):
        """
        Closes the connection.
        """

    @abstractmethod
    def upsert(self, signatures: Iterable[ImageSignature]):
        """
        Inserts or replaces signatures (keyed by path).

        :param signatures: signatures to store
        """

    @abstractmethod
    def set_full_pixel_hash(self, path: str, full_pixel_hash: str):
        """
        Stores the lazily computed full resolution pixel hash of a file.

        :param path: file path
        :param full_pixel_hash: hex SHA-256 of the decoded pixels
        """

    @abstractmethod
    def get(self, path: str) -> Optional[ImageSignature]:
        """
        :param path: file path
        :return: the stored signature, None if unknown
        """

    @abstractmethod
    def get_all(self) -> List[ImageSignature]:
        """
        :return: all stored signatures, ordered by path (input of the similarity search)
        """

    def iter_all(self) -> Iterator[ImageSignature]:
        """
        :return: all stored signatures, ordered by path; implementations may stream them
        """
        return iter(self.get_all())

    @abstractmethod
    def get_file_states(self) -> Dict[str, tuple]:
        """
        :return: path -> (file_size, mtime_ns, signature_version) of all stored files,
                 used to skip files that did not change since they were analysed
        """

    @abstractmethod
    def remove(self, paths: Iterable[str]):
        """
        :param paths: file paths whose entries should be deleted
        """

    def remove_missing(self, existing_paths: Iterable[str]) -> List[str]:
        """
        Deletes the entries of all files that are not part of `existing_paths`.

        :param existing_paths: paths of all files that still exist
        :return: the removed paths
        """
        existing = set(existing_paths)
        missing = [path for path in self.get_file_states() if path not in existing]
        if missing:
            self.remove(missing)
        return missing

    @abstractmethod
    def count(self) -> int:
        """
        :return: number of stored files
        """

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @staticmethod
    def needs_analysis(state: Optional[tuple], file_size: int, mtime_ns: int) -> bool:
        """
        :param state: stored (file_size, mtime_ns, signature_version), None if the file is unknown
        :param file_size: current file size
        :param mtime_ns: current modification time
        :return: True if the file has to be (re-)analysed
        """
        return state is None or state != (file_size, mtime_ns, SIGNATURE_VERSION)

    @staticmethod
    def _to_row(signature: ImageSignature) -> tuple:
        """
        :param signature: signature
        :return: values in the order of _COLUMNS (hashes as signed 64 bit ints)
        """
        metadata = signature.metadata
        return (
            signature.path, signature.file_size, signature.mtime_ns, signature.sha256,
            signature.preview_pixel_hash, signature.full_pixel_hash,
            _to_signed(signature.phash), _to_signed(signature.dhash),
            signature.width, signature.height, bool(signature.is_grayscale),
            metadata.capture_time.isoformat() if metadata.capture_time else None,
            bool(metadata.capture_time_has_subsec), metadata.camera_make, metadata.camera_model,
            metadata.image_unique_id, metadata.software, signature.signature_version,
            encode_orientation_hashes(signature.orientation_hashes) if signature.orientation_hashes else None,
            ",".join(str(v) for v in signature.colour_moments) if signature.colour_moments else None,
        )

    @staticmethod
    def _from_row(row: Mapping) -> ImageSignature:
        """
        :param row: a stored row, accessible by column name
        :return: the signature
        """
        return ImageSignature(
            path=row["path"],
            file_size=row["file_size"],
            mtime_ns=row["mtime_ns"],
            sha256=row["sha256"],
            preview_pixel_hash=row["preview_pixel_hash"],
            full_pixel_hash=row["full_pixel_hash"],
            phash=_to_unsigned(row["phash"]),
            dhash=_to_unsigned(row["dhash"]),
            width=row["width"],
            height=row["height"],
            is_grayscale=bool(row["is_grayscale"]),
            metadata=ImageMetadata(
                capture_time=datetime.fromisoformat(row["capture_time"]) if row["capture_time"] else None,
                capture_time_has_subsec=bool(row["capture_time_has_subsec"]),
                camera_make=row["camera_make"],
                camera_model=row["camera_model"],
                image_unique_id=row["image_unique_id"],
                software=row["software"],
            ),
            signature_version=row["signature_version"],
            orientation_hashes=decode_orientation_hashes(bytes(row["orientation_hashes"]))
            if row["orientation_hashes"] else None,
            colour_moments=tuple(float(v) for v in row["colour_moments"].split(","))
            if row["colour_moments"] else None,
        )


class SqliteSignatureStore(SignatureStore):
    """
    SQLite implementation (stdlib `sqlite3`). Default store, lives in its own database file so it can be
    deleted and rebuilt independently of the web UI state store.
    """

    def __init__(self, db_path: Path or str = ":memory:"):
        """
        :param db_path: SQLite database file (parent directories are created), ":memory:" for an in-memory database
        """
        self._lock = RLock()
        in_memory = str(db_path) == ":memory:"
        if not in_memory:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            if not in_memory:
                self._connection.execute("PRAGMA journal_mode=WAL")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                LOGGER.warning(f"Signature store {db_path} has schema version {version}, expected {SCHEMA_VERSION}, "
                               f"dropping it (all files will be re-analysed)")
                self._connection.execute("DROP TABLE IF EXISTS files")
            self._connection.executescript(_SCHEMA)
            self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._connection.execute("PRAGMA user_version").fetchone()[0]

    def close(self):
        """
        Closes the database connection.
        """
        with self._lock:
            self._connection.close()

    def upsert(self, signatures: Iterable[ImageSignature]):
        """
        Inserts or replaces signatures (keyed by path).

        :param signatures: signatures to store
        """
        rows = [self._to_row(signature) for signature in signatures]
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._lock, self._connection:
            self._connection.executemany(
                f"INSERT OR REPLACE INTO files ({', '.join(_COLUMNS)}) VALUES ({placeholders})", rows)

    def set_full_pixel_hash(self, path: str, full_pixel_hash: str):
        """
        Stores the lazily computed full resolution pixel hash of a file.

        :param path: file path
        :param full_pixel_hash: hex SHA-256 of the decoded pixels
        """
        with self._lock, self._connection:
            self._connection.execute("UPDATE files SET full_pixel_hash = ? WHERE path = ?", (full_pixel_hash, path))

    def get(self, path: str) -> Optional[ImageSignature]:
        """
        :param path: file path
        :return: the stored signature, None if unknown
        """
        with self._lock:
            row = self._connection.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return self._from_row(row) if row is not None else None

    def get_all(self) -> List[ImageSignature]:
        """
        :return: all stored signatures, ordered by path
        """
        with self._lock:
            rows = self._connection.execute("SELECT * FROM files ORDER BY path").fetchall()
        return [self._from_row(row) for row in rows]

    def get_file_states(self) -> Dict[str, tuple]:
        """
        :return: path -> (file_size, mtime_ns, signature_version) of all stored files,
                 used to skip files that did not change since they were analysed
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT path, file_size, mtime_ns, signature_version FROM files").fetchall()
        return {row["path"]: (row["file_size"], row["mtime_ns"], row["signature_version"]) for row in rows}

    def remove(self, paths: Iterable[str]):
        """
        :param paths: file paths whose entries should be deleted
        """
        with self._lock, self._connection:
            self._connection.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])

    def count(self) -> int:
        """
        :return: number of stored files
        """
        with self._lock:
            return self._connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]


SQLITE_SCHEME = "sqlite://"
POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def open_signature_store(url: str) -> SignatureStore:
    """
    Creates a signature store from a URL like setting.

    - `sqlite:///relative/path.db`, `sqlite:////absolute/path.db` (SQLAlchemy style), `sqlite:///:memory:`
    - a plain file path (no scheme) is a SQLite database file as well
    - `postgresql://user:password@host:5432/database` (libpq connection URI, requires the `postgres` extra)

    :param url: store URL
    :return: the opened store
    """
    url = str(url).strip()
    if not url:
        raise ValueError("Empty signature store URL")
    lowered = url.lower()
    if lowered.startswith(POSTGRES_SCHEMES):
        from py_image_dedup.matching.store_postgres import PostgresSignatureStore
        return PostgresSignatureStore(url)
    if lowered.startswith(SQLITE_SCHEME):
        path = url[len(SQLITE_SCHEME):]
        if path in ("", "/", "/:memory:", ":memory:"):
            return SqliteSignatureStore(":memory:")
        if not path.startswith("/"):
            raise ValueError(f"Invalid SQLite URL '{url}', expected sqlite:///relative.db or sqlite:////absolute.db")
        return SqliteSignatureStore(path[1:])
    if "://" in url:
        raise ValueError(f"Unsupported signature store URL '{url}', expected sqlite:// or postgresql://")
    return SqliteSignatureStore(url)
