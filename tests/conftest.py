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
# default remove this variable (see tests/test_auth_defaults.py).
os.environ.setdefault("FONREX_AUTH_REQUIRED", "false")

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
