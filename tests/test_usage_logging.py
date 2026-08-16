import tempfile
import unittest

from database.service import DatabaseService
from models import Base, UsageLog


class UsageLoggingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db")
        self.db = DatabaseService(f"sqlite:///{self.tmp.name}")
        Base.metadata.create_all(self.db.engine)

    def tearDown(self):
        self.db.Session.remove()
        self.db.engine.dispose()
        self.tmp.close()

    def test_log_usage_persists_api_call_metadata(self):
        success = self.db.log_usage(
            endpoint="/eod/AAPL",
            method="GET",
            status_code=200,
            latency_ms=42,
            api_key_id="key_123",
            provider_used="cache",
            cache_hit=True,
            cost_bucket="eod",
            ip_address="127.0.0.1",
            user_agent="unit-test",
        )

        self.assertTrue(success)

        session = self.db.get_session()
        try:
            usage_log = session.query(UsageLog).one()
            self.assertEqual(usage_log.endpoint, "/eod/AAPL")
            self.assertEqual(usage_log.method, "GET")
            self.assertEqual(usage_log.provider_used, "cache")
            self.assertTrue(usage_log.cache_hit)
            self.assertEqual(usage_log.latency_ms, 42)
        finally:
            session.close()

    def test_check_connection_returns_true_for_available_database(self):
        self.assertTrue(self.db.check_connection())


if __name__ == "__main__":
    unittest.main()
