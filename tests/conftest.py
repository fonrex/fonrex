"""
pytest configuration — isolation des suites de tests.

Les deux suites test_migrations.py et test_yfinance_enricher.py
utilisent SQLAlchemy avec StaticPool et peuvent interférer si
exécutées dans le même processus sans isolation explicite.
"""

import os
import warnings

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp")

# The API requires an API key by default. The suite exercises routes without
# credentials, so it runs in the explicit open mode; tests covering the secure
# default set their own variables (see tests/test_auth_defaults.py).
#
# The mode must not come from the developer's shell: a key exported there
# (`export FONREX_API_KEY=...`, as the README asks for Docker) would switch
# authentication back on — the open mode is ignored as soon as a key is
# configured — and every unauthenticated request of the suite would get a 401.
for _credential in (
    "FONREX_API_KEY",
    "FONREX_API_KEYS",
    "FONREX_READ_ONLY_API_KEYS",
    "FONREX_RELAY_KEY",
):
    os.environ.pop(_credential, None)
os.environ["FONREX_AUTH_REQUIRED"] = "false"

# The suite never talks to the developer's services. Left to the defaults, the
# application started by the tests connects to localhost:6379 and localhost:5432:
# the Redis and the database of a running `make docker-run`. Test answers were
# then cached in the real Redis (a fake AAPL price history kept for 24 hours),
# and a test passed or failed depending on what was running on the machine.
# Both addresses point to a closed port: the cache is disabled and the database
# unavailable, as on the CI. Tests that need a database build their own; those
# running on a real PostgreSQL read FONREX_TEST_DATABASE_URL.
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
os.environ["DATABASE_URL"] = "postgresql://fonrex:unused@127.0.0.1:1/fonrex_tests"
os.environ.pop("ASYNC_DATABASE_URL", None)

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated\. Use 'HTTP_422_UNPROCESSABLE_CONTENT' instead\.",
        category=DeprecationWarning,
    )
    import fastapi  # noqa: F401


def pytest_collection_modifyitems(items):
    """Garantit l'ordre d'exécution : migrations avant enricher pour éviter
    les conflits de SQLAlchemy mapper registry."""
    migration_tests = [i for i in items if "test_migrations" in str(i.fspath)]
    enricher_tests = [i for i in items if "test_yfinance_enricher" in str(i.fspath)]
    other_tests = [
        i
        for i in items
        if "test_migrations" not in str(i.fspath) and "test_yfinance_enricher" not in str(i.fspath)
    ]
    items[:] = migration_tests + other_tests + enricher_tests


# ── Simulated network for provider tests ─────────────────────────────────────

import asyncio  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _symbol_lookup_stays_offline(monkeypatch):
    """A test never asks Yahoo for the symbol of a listing.

    The ingestion service looks a symbol up through ``YahooLookup`` when none is
    injected. Reaching it from a test fails loudly instead of calling Yahoo:
    inject a ``symbol_resolver`` (or a fake lookup). Tests of the adapter itself
    request the ``yahoo_adapter`` fixture and patch yfinance.
    """
    from historical.yahoo_symbols import YahooLookup

    def refuse(self, *args, **kwargs):
        raise AssertionError(
            "A test reached Yahoo to look a symbol up: inject a symbol_resolver or a fake lookup"
        )

    originals = (YahooLookup.search, YahooLookup.quote)
    monkeypatch.setattr(YahooLookup, "search", refuse)
    monkeypatch.setattr(YahooLookup, "quote", refuse)
    return originals


@pytest.fixture
def yahoo_adapter(_symbol_lookup_stays_offline, monkeypatch):
    """The real ``YahooLookup`` methods, for tests that patch yfinance themselves."""
    from historical.yahoo_symbols import YahooLookup

    search, quote = _symbol_lookup_stays_offline
    monkeypatch.setattr(YahooLookup, "search", search)
    monkeypatch.setattr(YahooLookup, "quote", quote)
    return YahooLookup()


class FakeNetwork:
    """Answers HTTP requests from canned responses and records them.

    A route matches a method and a URL fragment. Its responses are served in
    order, the last one being repeated. A response is an ``httpx.Response`` or
    an exception to raise (timeout, connection error).
    """

    def __init__(self):
        self.routes: list[tuple[str, str, list]] = []
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        self.sleeps: list[float] = []

    def add(self, method: str, url_part: str, *responses) -> None:
        self.routes.append((method.upper(), url_part, list(responses)))

    def get(self, url_part: str, *responses) -> None:
        self.add("GET", url_part, *responses)

    def post(self, url_part: str, *responses) -> None:
        self.add("POST", url_part, *responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for method, url_part, responses in self.routes:
            if method == request.method and url_part in url:
                answer = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        self.unexpected.append(f"{request.method} {url}")
        return httpx.Response(599, text="unexpected request")

    def urls(self, url_part: str = "") -> list[str]:
        return [str(request.url) for request in self.requests if url_part in str(request.url)]

    def calls(self, url_part: str) -> int:
        return len(self.urls(url_part))

    @property
    def backoffs(self) -> list[float]:
        """Waits of one second or more, i.e. retry pauses rather than jitter."""
        return [delay for delay in self.sleeps if delay >= 1]


@pytest.fixture
def fake_network(monkeypatch):
    """Route every ``httpx.AsyncClient`` request to a FakeNetwork, without real waits."""
    network = FakeNetwork()
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(network.handler)
        return real_client(*args, **kwargs)

    async def no_sleep(delay, *args, **kwargs):
        network.sleeps.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    yield network
    assert not network.unexpected, f"requests without a canned response: {network.unexpected}"
