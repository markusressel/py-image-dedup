import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Dict, Iterable, List, Optional

from py_image_dedup.matching.models import ImageMetadata, ImageSignature, SIGNATURE_VERSION

SCHEMA_VERSION = 1

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
    analysed_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS files_sha256 ON files (sha256);
CREATE INDEX IF NOT EXISTS files_preview_pixel_hash ON files (preview_pixel_hash);
"""

_COLUMNS = (
    "path", "file_size", "mtime_ns", "sha256", "preview_pixel_hash", "full_pixel_hash", "phash", "dhash",
    "width", "height", "is_grayscale", "capture_time", "capture_time_has_subsec", "camera_make", "camera_model",
    "image_unique_id", "software", "signature_version",
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


class SignatureStore:
    """
    SQLite store for image signatures and matching metadata (one row per file).
    """

    def __init__(self, db_path: Path or str = ":memory:"):
        """
        :param db_path: SQLite database file, ":memory:" for an in-memory database
        """
        self._lock = RLock()
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            if str(db_path) != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(_SCHEMA)
            self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

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
        metadata = signature.metadata
        return (
            signature.path, signature.file_size, signature.mtime_ns, signature.sha256,
            signature.preview_pixel_hash, signature.full_pixel_hash,
            _to_signed(signature.phash), _to_signed(signature.dhash),
            signature.width, signature.height, int(signature.is_grayscale),
            metadata.capture_time.isoformat() if metadata.capture_time else None,
            int(metadata.capture_time_has_subsec), metadata.camera_make, metadata.camera_model,
            metadata.image_unique_id, metadata.software, signature.signature_version,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ImageSignature:
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
        )
