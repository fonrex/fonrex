import unittest
from datetime import date

from cache.service import CacheService


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    def ping(self):
        return True

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl

    def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [key.encode("utf-8") for key in self.store if key.startswith(prefix)]

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            decoded = key.decode("utf-8") if isinstance(key, bytes) else key
            if decoded in self.store:
                deleted += 1
                del self.store[decoded]
        return deleted

    def info(self):
        return {
            "redis_version": "fake",
            "connected_clients": 1,
            "used_memory_human": "0B",
            "uptime_in_seconds": 1,
        }


class CacheServiceStub(CacheService):
    def _connect(self):
        self.client = FakeRedis()
        self.enabled = True


class CacheServiceTest(unittest.TestCase):
    def test_generates_readable_keys_and_uses_type_ttl(self):
        cache = CacheServiceStub(ttl=300)
        key = cache.generate_key(
            "aapl",
            "1mo",
            cache_type="eod",
            fmt="json",
            from_date="2026-01-01",
        )

        self.assertEqual(key, "eod:AAPL:1mo:fmt-json:from_date-2026-01-01")

        cache.set(key, {"retrieved_at": date(2026, 1, 2)}, cache_type="eod")
        self.assertEqual(cache.client.ttls[key], 86400)
        self.assertEqual(cache.get(key), {"retrieved_at": "2026-01-02"})

    def test_clear_ticker_cache_uses_readable_patterns(self):
        cache = CacheServiceStub()
        key = cache.generate_key("TSLA", "5d")
        cache.set(key, {"ok": True})

        success, deleted_count, error = cache.clear_ticker_cache("TSLA")

        self.assertTrue(success)
        self.assertEqual(deleted_count, 1)
        self.assertIsNone(error)
        self.assertIsNone(cache.get(key))


if __name__ == "__main__":
    unittest.main()
