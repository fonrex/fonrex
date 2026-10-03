"""One HTTP policy for every provider (financials/providers/base.py).

The base module is the single place where a provider gets an HTTP client: the
retry policy, the limit of simultaneous requests and the outbound proxy are
applied there. These tests check the policy and that no provider bypasses it.
"""

import asyncio
import importlib
import re
from pathlib import Path

import httpx
import pytest

import main
from financials.providers import base
from financials.providers.base import (
    MAX_RETRY_AFTER_SECONDS,
    BaseFinancialProvider,
    configured_proxy_url,
    new_sync_client,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
URL = "https://example.test/page"
SEARCH = "https://example.test/search"

PROXY_ENV_VARS = ("FONREX_PROXY_URL", "FONREX_PROXY_PROVIDERS", "FONREX_PROVIDER_MAX_CONCURRENCY")


class SampleProvider(BaseFinancialProvider):
    name = "Sample"
    max_retries = 3
    retry_delay = 1.0

    async def get_financials(self, ticker):
        return {"ticker": ticker}


def text(body: str = "", code: int = 200, **headers) -> httpx.Response:
    return httpx.Response(code, text=body, headers=headers)


@pytest.fixture
def clean_proxy_env(monkeypatch):
    for name in PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestNoProviderBypassesTheCommonClient:
    FORBIDDEN = re.compile(
        r"httpx\.(AsyncClient|Client|get|post|request|stream)\(|requests\.(get|post|Session|request)\(|"
        r"aiohttp\.ClientSession\(|urllib\.request\.urlopen\("
    )

    def _provider_modules(self):
        for package in ("financials/providers", "news/providers"):
            for path in sorted((PROJECT_ROOT / package).glob("*.py")):
                if path.name not in ("__init__.py", "base.py"):
                    yield path

    def test_no_provider_creates_its_own_http_client(self):
        offenders = []
        for path in self._provider_modules():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.FORBIDDEN.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
        assert not offenders, (
            "Providers must use self._session(), self._get() or new_sync_client() from "
            "financials/providers/base.py:\n" + "\n".join(offenders)
        )

    def test_every_registered_provider_extends_the_common_base(self):
        specs = [(module, cls) for _name, module, cls in main.PROVIDER_SPECS]
        specs += [(module, cls) for _state, _label, module, cls in main.SPECIALIZED_PROVIDER_SPECS]
        for module, cls in specs:
            provider_class = getattr(importlib.import_module(module), cls)
            assert issubclass(provider_class, BaseFinancialProvider), cls


class TestSessionRetryPolicy:
    async def test_success_is_returned_without_pause(self, fake_network):
        fake_network.get(URL, text("hello"))

        async with SampleProvider()._session() as client:
            response = await client.get(URL, params={"q": "AIR"})

        assert response.text == "hello"
        assert fake_network.requests[0].url.params["q"] == "AIR"
        assert fake_network.sleeps == []

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    async def test_retryable_answer_is_retried_with_growing_pauses(self, fake_network, code):
        fake_network.get(URL, text("", code), text("", code), text("ok"))

        async with SampleProvider()._session() as client:
            response = await client.get(URL)

        assert response.text == "ok"
        assert fake_network.calls(URL) == 3
        assert fake_network.sleeps == [1.0, 2.0]

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 302])
    async def test_final_answer_is_returned_at_once(self, fake_network, code):
        fake_network.get(URL, text("", code))

        async with SampleProvider()._session(follow_redirects=False) as client:
            response = await client.get(URL)

        assert response.status_code == code
        assert fake_network.calls(URL) == 1
        assert fake_network.sleeps == []

    async def test_last_retryable_answer_is_returned_without_a_useless_pause(self, fake_network):
        fake_network.get(URL, text("", 503))

        async with SampleProvider()._session() as client:
            response = await client.get(URL)

        assert response.status_code == 503
        assert fake_network.calls(URL) == 3
        # Two pauses between three attempts, none after the last one.
        assert fake_network.sleeps == [1.0, 2.0]

    async def test_retry_after_is_honoured(self, fake_network):
        fake_network.get(URL, text("", 429, **{"Retry-After": "3"}), text("ok"))

        async with SampleProvider()._session() as client:
            await client.get(URL)

        assert fake_network.sleeps == [3.0]

    async def test_retry_after_is_capped(self, fake_network):
        fake_network.get(URL, text("", 429, **{"Retry-After": "3600"}), text("ok"))

        async with SampleProvider()._session() as client:
            await client.get(URL)

        assert fake_network.sleeps == [MAX_RETRY_AFTER_SECONDS]

    async def test_unreadable_retry_after_falls_back_to_the_backoff(self, fake_network):
        fake_network.get(URL, text("", 503, **{"Retry-After": "soon"}), text("ok"))

        async with SampleProvider()._session() as client:
            await client.get(URL)

        assert fake_network.sleeps == [1.0]

    async def test_network_error_is_retried_then_recovers(self, fake_network):
        fake_network.get(URL, httpx.ConnectTimeout("slow"), httpx.ConnectError("down"), text("ok"))

        async with SampleProvider()._session() as client:
            response = await client.get(URL)

        assert response.text == "ok"
        assert fake_network.sleeps == [1.0, 2.0]

    async def test_network_error_is_raised_after_the_last_attempt(self, fake_network):
        fake_network.get(URL, httpx.ReadError("reset"))

        async with SampleProvider()._session() as client:
            with pytest.raises(httpx.ReadError):
                await client.get(URL)

        assert fake_network.calls(URL) == 3
        assert fake_network.sleeps == [1.0, 2.0]

    async def test_max_retries_of_the_provider_is_respected(self, fake_network):
        fake_network.get(URL, text("", 503))
        provider = SampleProvider()
        provider.max_retries = 1

        async with provider._session() as client:
            response = await client.get(URL)

        assert response.status_code == 503
        assert fake_network.calls(URL) == 1
        assert fake_network.sleeps == []


class TestSessionRequests:
    async def test_headers_given_as_a_function_are_drawn_at_each_attempt(self, fake_network):
        fake_network.get(URL, text("", 503), text("", 503), text("ok"))
        agents = iter(["agent-1", "agent-2", "agent-3"])

        async with SampleProvider()._session() as client:
            await client.get(URL, headers=lambda: {"User-Agent": next(agents)})

        sent = [request.headers["User-Agent"] for request in fake_network.requests]
        assert sent == ["agent-1", "agent-2", "agent-3"]

    async def test_request_headers_are_sent_as_given(self, fake_network):
        fake_network.get(URL, text("ok"))

        async with SampleProvider()._session() as client:
            await client.get(
                URL, headers={"User-Agent": "Fonrex/1.0", "Referer": "https://x.test/"}
            )

        request = fake_network.requests[0]
        assert request.headers["User-Agent"] == "Fonrex/1.0"
        assert request.headers["Referer"] == "https://x.test/"

    async def test_session_headers_apply_to_every_request(self, fake_network):
        fake_network.get(SEARCH, text("{}"))
        fake_network.get(URL, text("ok"))

        async with SampleProvider()._session(headers={"Origin": "https://x.test"}) as client:
            await client.get(SEARCH)
            await client.get(URL)

        assert [r.headers["Origin"] for r in fake_network.requests] == ["https://x.test"] * 2

    async def test_cookies_set_by_the_search_are_sent_with_the_page(self, fake_network):
        fake_network.get(SEARCH, text("{}", **{"Set-Cookie": "sid=abc123; Path=/"}))
        fake_network.get(URL, text("ok"))

        async with SampleProvider()._session() as client:
            await client.get(SEARCH)
            await client.get(URL)

        assert "sid=abc123" in fake_network.requests[1].headers["Cookie"]

    async def test_post_sends_form_data(self, fake_network):
        fake_network.post(SEARCH, text("found"))

        async with SampleProvider()._session() as client:
            response = await client.post(SEARCH, data={"q": "FR0000120073"})

        assert response.text == "found"
        assert fake_network.requests[0].content == b"q=FR0000120073"

    def test_default_headers_do_not_advertise_brotli(self):
        # httpx announces the encodings it can decode; "br" needs an extra package.
        assert "Accept-Encoding" not in SampleProvider()._get_headers()


class TestConcurrencyLimit:
    async def _peak(self, monkeypatch, provider_class, requests: int) -> int:
        state = {"running": 0, "peak": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.01)
            state["running"] -= 1
            return httpx.Response(200, text="ok")

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda *args, **kwargs: real_client(
                *args, **{**kwargs, "transport": httpx.MockTransport(handler)}
            ),
        )
        provider = provider_class()
        async with provider._session() as client:
            await asyncio.gather(*(client.get(f"{URL}/{i}") for i in range(requests)))
        return state["peak"]

    async def test_default_limit(self, monkeypatch, clean_proxy_env):
        class Fresh(SampleProvider):
            pass

        assert await self._peak(monkeypatch, Fresh, 12) == base.DEFAULT_MAX_CONCURRENCY

    async def test_limit_follows_the_environment(self, monkeypatch, clean_proxy_env):
        class Fresh(SampleProvider):
            pass

        clean_proxy_env.setenv("FONREX_PROVIDER_MAX_CONCURRENCY", "2")

        assert await self._peak(monkeypatch, Fresh, 8) == 2

    async def test_provider_specific_limit_wins(self, monkeypatch, clean_proxy_env):
        class Limited(SampleProvider):
            _semaphore = asyncio.Semaphore(1)

        clean_proxy_env.setenv("FONREX_PROVIDER_MAX_CONCURRENCY", "8")

        assert await self._peak(monkeypatch, Limited, 5) == 1

    async def test_limit_is_per_provider(self, clean_proxy_env):
        class First(SampleProvider):
            pass

        class Second(SampleProvider):
            pass

        assert First._limit() is First._limit()
        assert First._limit() is not Second._limit()

    def test_limiter_is_not_shared_between_event_loops(self, clean_proxy_env):
        class Fresh(SampleProvider):
            pass

        async def limiter():
            return Fresh._limit()

        assert asyncio.run(limiter()) is not asyncio.run(limiter())


class TestOutboundProxy:
    def test_direct_connection_by_default(self, clean_proxy_env):
        assert configured_proxy_url("Boursorama") is None
        assert "proxy" not in base._client_options("Boursorama", 10)

    def test_proxy_applies_to_every_provider(self, clean_proxy_env):
        clean_proxy_env.setenv("FONREX_PROXY_URL", " http://user:secret@proxy.test:8888 ")

        assert configured_proxy_url("Boursorama") == "http://user:secret@proxy.test:8888"
        assert configured_proxy_url("GoogleFinance") == "http://user:secret@proxy.test:8888"

    def test_proxy_can_be_restricted_to_some_providers(self, clean_proxy_env):
        clean_proxy_env.setenv("FONREX_PROXY_URL", "http://proxy.test:8888")
        clean_proxy_env.setenv("FONREX_PROXY_PROVIDERS", "Investing, gurufocus ,wallStreetJournal")

        assert configured_proxy_url("Investing") == "http://proxy.test:8888"
        assert configured_proxy_url("Gurufocus") == "http://proxy.test:8888"
        assert configured_proxy_url("WallStreetJournal") == "http://proxy.test:8888"
        assert configured_proxy_url("Boursorama") is None

    def test_provider_list_without_proxy_changes_nothing(self, clean_proxy_env):
        clean_proxy_env.setenv("FONREX_PROXY_PROVIDERS", "Investing")

        assert configured_proxy_url("Investing") is None

    def test_async_client_is_built_with_the_proxy(self, monkeypatch, clean_proxy_env):
        captured = {}
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kwargs: captured.update(kwargs) or object()
        )
        clean_proxy_env.setenv("FONREX_PROXY_URL", "http://proxy.test:8888")

        SampleProvider()._new_client(headers={"Origin": "https://x.test"})

        assert captured["proxy"] == "http://proxy.test:8888"
        assert captured["timeout"] == SampleProvider.timeout
        assert captured["follow_redirects"] is True
        assert captured["headers"] == {"Origin": "https://x.test"}

    def test_async_client_without_proxy(self, monkeypatch, clean_proxy_env):
        captured = {}
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kwargs: captured.update(kwargs) or object()
        )

        SampleProvider()._new_client()

        assert "proxy" not in captured
        assert "headers" not in captured

    def test_sync_client_follows_the_same_rules(self, monkeypatch, clean_proxy_env):
        captured = {}
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: captured.update(kwargs) or object())
        clean_proxy_env.setenv("FONREX_PROXY_URL", "http://proxy.test:8888")
        clean_proxy_env.setenv("FONREX_PROXY_PROVIDERS", "JustETF")

        new_sync_client("JustETF", 15.0, headers={"User-Agent": "x"})

        assert captured == {
            "timeout": 15.0,
            "follow_redirects": True,
            "headers": {"User-Agent": "x"},
            "proxy": "http://proxy.test:8888",
        }

    def test_real_clients_accept_the_proxy_option(self, clean_proxy_env):
        # Guards against an httpx version whose clients do not know ``proxy=``.
        clean_proxy_env.setenv("FONREX_PROXY_URL", "http://proxy.test:8888")

        new_sync_client("JustETF", 5.0).close()
        assert isinstance(SampleProvider()._new_client(), httpx.AsyncClient)


class TestProviderKey:
    def test_explicit_name_is_used(self):
        assert SampleProvider.provider_key() == "Sample"

    def test_class_name_is_used_without_the_provider_suffix(self):
        class WallStreetJournalProvider(BaseFinancialProvider):
            pass

        assert WallStreetJournalProvider.provider_key() == "WallStreetJournal"

    @pytest.mark.parametrize(("name", "module", "cls"), main.PROVIDER_SPECS)
    def test_registered_names_select_their_provider_for_the_proxy(
        self, clean_proxy_env, name, module, cls
    ):
        """A name of the provider list in FONREX_PROXY_PROVIDERS must match its class."""
        if name == "YahooFinance":
            pytest.skip("YahooFinance goes through the yfinance library, not through this client")
        provider_class = getattr(importlib.import_module(module), cls)
        clean_proxy_env.setenv("FONREX_PROXY_URL", "http://proxy.test:8888")
        clean_proxy_env.setenv("FONREX_PROXY_PROVIDERS", name)

        assert configured_proxy_url(provider_class.provider_key()) == "http://proxy.test:8888"
