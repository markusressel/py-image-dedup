"""
Tests of the signature stores of the experimental matching pipeline (py_image_dedup.matching.store*).

The same contract tests run against SQLite (always) and Postgres (integration marker, needs
PY_IMAGE_DEDUP_TEST_POSTGRES_DSN, e.g. postgresql://postgres:postgres@127.0.0.1:5432/postgres).
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from py_image_dedup.matching import ImageMetadata, ImageSignature, MatchingPipeline
from py_image_dedup.matching.models import SIGNATURE_VERSION
from py_image_dedup.matching.store import (
    SCHEMA_VERSION, SignatureStore, SqliteSignatureStore, open_signature_store,
)

POSTGRES_DSN_ENV = "PY_IMAGE_DEDUP_TEST_POSTGRES_DSN"
IMAGES = Path(__file__).parent / "images"

CAMERA = ImageMetadata(capture_time=datetime(2019, 9, 3, 19, 31, 51, 120000), capture_time_has_subsec=True,
                       camera_make="Google", camera_model="Pixel 3", image_unique_id="abc", software="HDR+")


def _signature(path: str, phash: int = 0, dhash: int = 0, metadata: ImageMetadata = ImageMetadata(),
               grayscale: bool = False, size: int = 1, mtime_ns: int = 1) -> ImageSignature:
    return ImageSignature(path=path, file_size=size, mtime_ns=mtime_ns, sha256=f"sha-{path}",
                          preview_pixel_hash=f"pixels-{path}", phash=phash, dhash=dhash, width=400, height=300,
                          is_grayscale=grayscale, metadata=metadata)


class StoreContract:
    """
    Behaviour every SignatureStore implementation must provide. Subclasses set self.store in setUp.
    """
    store: SignatureStore

    def test_is_signature_store(self):
        self.assertIsInstance(self.store, SignatureStore)
        self.assertEqual(SCHEMA_VERSION, self.store.schema_version)

    def test_roundtrip_with_unsigned_64_bit_hashes(self):
        signature = _signature("/a.jpg", phash=(1 << 64) - 1, dhash=1 << 63, metadata=CAMERA, grayscale=True)
        self.store.upsert([signature])
        self.assertEqual(signature, self.store.get("/a.jpg"))
        self.assertEqual([signature], self.store.get_all())
        self.assertEqual([signature], list(self.store.iter_all()))

    def test_unknown_path(self):
        self.assertIsNone(self.store.get("/unknown.jpg"))
        self.assertEqual([], self.store.get_all())
        self.assertEqual(0, self.store.count())

    def test_upsert_replaces_and_orders_by_path(self):
        self.store.upsert([_signature("/b.jpg"), _signature("/a.jpg")])
        changed = _signature("/b.jpg", phash=42, size=2, mtime_ns=3)
        self.store.upsert([changed])
        self.store.upsert([])
        self.assertEqual(["/a.jpg", "/b.jpg"], [s.path for s in self.store.get_all()])
        self.assertEqual(changed, self.store.get("/b.jpg"))
        self.assertEqual(2, self.store.count())

    def test_full_pixel_hash_and_remove(self):
        self.store.upsert([_signature("/a.jpg"), _signature("/b.jpg")])
        self.store.set_full_pixel_hash("/a.jpg", "abc")
        self.assertEqual("abc", self.store.get("/a.jpg").full_pixel_hash)
        self.store.remove(["/a.jpg"])
        self.store.remove([])
        self.assertIsNone(self.store.get("/a.jpg"))
        self.assertEqual(1, self.store.count())

    def test_remove_missing(self):
        self.store.upsert([_signature("/a.jpg"), _signature("/b.jpg"), _signature("/c.jpg")])
        self.assertEqual(["/b.jpg"], self.store.remove_missing(["/a.jpg", "/c.jpg", "/new.jpg"]))
        self.assertEqual(["/a.jpg", "/c.jpg"], [s.path for s in self.store.get_all()])

    def test_file_states_and_needs_analysis(self):
        self.store.upsert([_signature("/a.jpg")])
        state = self.store.get_file_states()["/a.jpg"]
        self.assertEqual((1, 1, SIGNATURE_VERSION), state)
        self.assertFalse(SignatureStore.needs_analysis(state, 1, 1))
        self.assertTrue(SignatureStore.needs_analysis(state, 2, 1))
        self.assertTrue(SignatureStore.needs_analysis(state, 1, 2))
        self.assertTrue(SignatureStore.needs_analysis(None, 1, 1))

    def test_pipeline_uses_store(self):
        files = sorted((IMAGES / "bottles").glob("*.jpg"))
        result = MatchingPipeline(self.store, workers=2).analyse(files)
        self.assertEqual(len(files), result.analysed)
        self.assertEqual(len(files), self.store.count())
        self.assertEqual(len(files), MatchingPipeline(self.store).analyse(files).skipped_unchanged)


class SqliteStoreTest(StoreContract, unittest.TestCase):

    def setUp(self):
        self.store = SqliteSignatureStore(":memory:")
        self.addCleanup(self.store.close)


class SqliteFileStoreTest(StoreContract, unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_dir)
        self.store = SqliteSignatureStore(self.temp_dir / "nested" / "signatures.db")
        self.addCleanup(self.store.close)

    def test_persists_across_connections(self):
        self.store.upsert([_signature("/a.jpg", metadata=CAMERA)])
        self.store.close()
        with SqliteSignatureStore(self.temp_dir / "nested" / "signatures.db") as reopened:
            self.assertEqual(_signature("/a.jpg", metadata=CAMERA), reopened.get("/a.jpg"))

    def test_other_schema_version_is_rebuilt(self):
        self.store.upsert([_signature("/a.jpg")])
        self.store.close()
        connection = sqlite3.connect(self.temp_dir / "nested" / "signatures.db")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        connection.close()
        with SqliteSignatureStore(self.temp_dir / "nested" / "signatures.db") as reopened:
            self.assertEqual(SCHEMA_VERSION, reopened.schema_version)
            self.assertEqual(0, reopened.count())


class FactoryTest(unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_dir)

    def test_signature_store_constructor_is_backwards_compatible(self):
        store = SignatureStore(":memory:")
        self.addCleanup(store.close)
        self.assertIsInstance(store, SqliteSignatureStore)

    def test_sqlite_urls(self):
        for url in ("sqlite://", "sqlite:///:memory:", ":memory:"):
            with open_signature_store(url) as store:
                self.assertIsInstance(store, SqliteSignatureStore)

        absolute = self.temp_dir / "abs.db"
        with open_signature_store(f"sqlite:///{absolute}"):
            pass
        self.assertTrue(absolute.exists())

        with open_signature_store(str(self.temp_dir / "plain.db")):
            pass
        self.assertTrue((self.temp_dir / "plain.db").exists())

        cwd = os.getcwd()
        os.chdir(self.temp_dir)
        self.addCleanup(os.chdir, cwd)
        with open_signature_store("sqlite:///relative/signatures.db"):
            pass
        self.assertTrue((self.temp_dir / "relative" / "signatures.db").exists())

    def test_invalid_urls(self):
        for url in ("", "  ", "mysql://host/db", "sqlite://relative.db"):
            with self.assertRaises(ValueError, msg=url):
                open_signature_store(url)

    def test_postgres_url_without_psycopg_or_server_fails_cleanly(self):
        # either psycopg is missing (ImportError) or nothing listens on port 1 (connection error)
        with self.assertRaises(Exception):
            open_signature_store("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")


@pytest.mark.integration
@unittest.skipUnless(os.environ.get(POSTGRES_DSN_ENV), f"{POSTGRES_DSN_ENV} not set")
class PostgresStoreTest(StoreContract, unittest.TestCase):

    def setUp(self):
        pytest.importorskip("psycopg")
        from py_image_dedup.matching import store_postgres
        # isolate every test in its own schema
        self.schema = f"test_{uuid.uuid4().hex[:12]}"
        dsn = os.environ[POSTGRES_DSN_ENV]
        separator = "&" if "?" in dsn else "?"
        self.admin = store_postgres._import_psycopg()[0].connect(dsn, autocommit=True)
        self.addCleanup(self.admin.close)
        self.admin.execute(f"CREATE SCHEMA {self.schema}")
        self.addCleanup(self.admin.execute, f"DROP SCHEMA {self.schema} CASCADE")
        self.dsn = f"{dsn}{separator}options=-csearch_path%3D{self.schema}"
        self.store = open_signature_store(self.dsn)
        self.addCleanup(self.store.close)

    def test_is_postgres_store(self):
        from py_image_dedup.matching.store_postgres import PostgresSignatureStore
        self.assertIsInstance(self.store, PostgresSignatureStore)

    def test_other_schema_version_is_rebuilt(self):
        self.store.upsert([_signature("/a.jpg")])
        self.admin.execute(f"UPDATE {self.schema}.matching_meta SET value = '999' WHERE key = 'schema_version'")
        with open_signature_store(self.dsn) as reopened:
            self.assertEqual(SCHEMA_VERSION, reopened.schema_version)
            self.assertEqual(0, reopened.count())
