import logging
import unittest
from datetime import timedelta
from unittest.mock import patch

import requests

from py_image_dedup.persistence.elasticsearchstorebackend import ElasticSearchStoreBackend

MODULE = "py_image_dedup.persistence.elasticsearchstorebackend"


class FakeClock:
    """
    Replaces time.monotonic and time.sleep, so waiting does not take real time.
    """

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.now += seconds


class ElasticSearchStoreBackendConnectionTest(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        for name, replacement in [
            ("time.monotonic", self.clock.monotonic),
            ("time.sleep", self.clock.sleep),
        ]:
            patcher = patch(f"{MODULE}.{name}", side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        # never talk to a real elasticsearch instance
        for name in ["SignatureES", "Elasticsearch"]:
            patcher = patch(f"{MODULE}.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _create(connection_timeout: timedelta) -> ElasticSearchStoreBackend:
        return ElasticSearchStoreBackend(
            host="127.0.0.1",
            port=9200,
            connections_per_node=1,
            el_index="images",
            setup_database=False,
            connection_timeout=connection_timeout,
        )

    def test_raises_after_timeout(self):
        with patch.object(ElasticSearchStoreBackend, "_detect_db_version", return_value=None) as detect:
            with self.assertRaises(ConnectionError) as context:
                self._create(timedelta(seconds=10))

        self.assertIn("127.0.0.1:9200", str(context.exception))
        # attempts at t=0, 2, 4, 6, 8, 10
        self.assertEqual(6, detect.call_count)
        self.assertAlmostEqual(10, sum(self.clock.sleeps))

    def test_zero_timeout_tries_once(self):
        with patch.object(ElasticSearchStoreBackend, "_detect_db_version", return_value=None) as detect:
            with self.assertRaises(ConnectionError):
                self._create(timedelta(0))

        self.assertEqual(1, detect.call_count)
        self.assertEqual([], self.clock.sleeps)

    def test_succeeds_once_reachable(self):
        with patch.object(ElasticSearchStoreBackend, "_detect_db_version", side_effect=[None, None, 7]) as detect:
            backend = self._create(timedelta(minutes=2))

        self.assertEqual(3, detect.call_count)
        self.assertEqual(7, backend._el_version)
        self.assertEqual(ElasticSearchStoreBackend.DEFAULT_EL_DOC_TYPE_EL_7, backend._el_doctype)

    def test_no_wait_if_reachable_immediately(self):
        with patch.object(ElasticSearchStoreBackend, "_detect_db_version", return_value=7):
            self._create(timedelta(minutes=2))

        self.assertEqual([], self.clock.sleeps)

    def test_retries_log_a_single_line_without_traceback(self):
        error = requests.exceptions.ConnectionError("Max retries exceeded with url: /\n(Caused by NewConnectionError)")
        with patch(f"{MODULE}.requests.get", side_effect=error), \
                self.assertLogs(MODULE, level=logging.DEBUG) as logs:
            with self.assertRaises(ConnectionError) as context:
                self._create(timedelta(seconds=4))

        # attempts at t=0, 2 are retried, the one at t=4 gives up
        self.assertEqual(2, len(logs.records))
        for record in logs.records:
            self.assertIsNone(record.exc_info)
            self.assertNotIn("\n", record.getMessage())
            self.assertIn("ConnectionError", record.getMessage())
        # the final error names the last cause
        self.assertIn("Max retries exceeded", str(context.exception))
        self.assertIs(error, context.exception.__cause__)


if __name__ == '__main__':
    unittest.main()
