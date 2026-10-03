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

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

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


class CacheEntriesAreDataTest(unittest.TestCase):
    """An entry of the cache is read as JSON, never executed."""

    def test_pickled_entry_is_ignored_and_runs_nothing(self):
        """A pickled entry used to be rebuilt: whoever writes to Redis ran code in the API."""
        import os
        import pickle
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            trace = os.path.join(folder, "created-by-the-entry")

            class Payload:
                def __reduce__(self):
                    # What unpickling calls: here, creating a folder.
                    return (os.mkdir, (trace,))

            cache = CacheServiceStub()
            cache.client.store["eod:AAPL:5d"] = pickle.dumps(Payload())

            self.assertIsNone(cache.get("eod:AAPL:5d"))
            self.assertFalse(os.path.exists(trace))

    def test_unreadable_entries_are_a_miss(self):
        cache = CacheServiceStub()
        for value in (b"\xff\xfe not text", b"not json", "also not json", b""):
            cache.client.store["key"] = value
            self.assertIsNone(cache.get("key"))

    def test_json_entries_are_read_as_bytes_or_text(self):
        cache = CacheServiceStub()
        cache.client.store["bytes"] = b'{"close": 31.378}'
        cache.client.store["text"] = '{"close": 31.378}'

        self.assertEqual(cache.get("bytes"), {"close": 31.378})
        self.assertEqual(cache.get("text"), {"close": 31.378})

    def test_application_code_never_imports_pickle(self):
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob("*.py"):
            relative = path.relative_to(root)
            if relative.parts[0] in {"tests", ".venv", "venv", "path", "node_modules"}:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(name.split(".")[0] in {"pickle", "cPickle", "dill", "shelve"} for name in names):
                    offenders.append(f"{relative}:{node.lineno}")

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
