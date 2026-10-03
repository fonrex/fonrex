"""HTTP helpers shared by the providers (financials/providers/base.py).

Retry policy, terminal statuses, pauses and JSON handling, against the
simulated network of ``tests/conftest.py``.
"""

import asyncio

import httpx
import pytest

from financials.providers.base import BaseFinancialProvider, BaseProvider

URL = "https://example.test/data"


class SampleProvider(BaseFinancialProvider):
    name = "Sample"
    max_retries = 3
    retry_delay = 1.0

    async def get_financials(self, ticker):
        return {"ticker": ticker}


def text(body: str, code: int = 200, **headers) -> httpx.Response:
    return httpx.Response(code, text=body, headers=headers)


class TestGet:
    async def test_success_returns_the_body(self, fake_network):
        fake_network.get(URL, text("hello"))

        assert await SampleProvider()._get(URL) == "hello"
        assert fake_network.sleeps == []

    async def test_query_parameters_are_sent(self, fake_network):
        fake_network.get(URL, text("ok"))

        await SampleProvider()._get(URL, params={"q": "AIR", "size": 10})

        assert fake_network.urls() == [f"{URL}?q=AIR&size=10"]

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
    async def test_client_errors_are_not_retried(self, fake_network, code):
        fake_network.get(URL, text("", code))

        assert await SampleProvider()._get(URL) is None
        assert len(fake_network.requests) == 1
        assert fake_network.sleeps == []

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    async def test_server_errors_are_retried_until_success(self, fake_network, code):
        fake_network.get(URL, text("", code), text("", code), text("finally"))

        assert await SampleProvider()._get(URL) == "finally"
        assert len(fake_network.requests) == 3
        # Exponential pause: retry_delay * 2^attempt
        assert fake_network.sleeps == [1.0, 2.0]

    async def test_persistent_server_error_gives_none(self, fake_network):
        fake_network.get(URL, text("", 503))

        assert await SampleProvider()._get(URL) is None
        assert len(fake_network.requests) == 3
        assert fake_network.sleeps == [1.0, 2.0, 4.0]

    async def test_retry_after_header_sets_the_pause(self, fake_network):
        fake_network.get(URL, text("", 429, **{"Retry-After": "7"}), text("ok"))

        assert await SampleProvider()._get(URL) == "ok"
        assert fake_network.sleeps == [7.0]

    async def test_unreadable_retry_after_falls_back_to_the_backoff(self, fake_network):
        fake_network.get(
            URL, text("", 429, **{"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), text("ok")
        )

        assert await SampleProvider()._get(URL) == "ok"
        assert fake_network.sleeps == [1.0]

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectTimeout("timed out"),
            httpx.ReadTimeout("timed out"),
            httpx.ConnectError("refused"),
            httpx.RemoteProtocolError("server disconnected"),
        ],
        ids=["connect-timeout", "read-timeout", "connect-error", "protocol-error"],
    )
    async def test_network_errors_are_retried_then_recover(self, fake_network, error):
        fake_network.get(URL, error, text("recovered"))

        assert await SampleProvider()._get(URL) == "recovered"
        assert fake_network.sleeps == [1.0]

    async def test_persistent_network_error_gives_none(self, fake_network):
        fake_network.get(URL, httpx.ConnectError("refused"))

        assert await SampleProvider()._get(URL) is None
        assert len(fake_network.requests) == 3

    async def test_unexpected_error_gives_none_without_retry(self, fake_network):
        fake_network.get(URL, RuntimeError("boom"))

        assert await SampleProvider()._get(URL) is None
        assert len(fake_network.requests) == 1

    async def test_other_status_gives_none_without_retry(self, fake_network):
        fake_network.get(URL, text("", 204))

        assert await SampleProvider()._get(URL) is None
        assert len(fake_network.requests) == 1

    async def test_redirect_is_followed(self, fake_network):
        fake_network.get(URL, httpx.Response(302, headers={"Location": "https://example.test/new"}))
        fake_network.get("https://example.test/new", text("moved"))

        assert await SampleProvider()._get(URL) == "moved"

    async def test_redirect_is_not_followed_when_disabled(self, fake_network):
        fake_network.get(URL, httpx.Response(302, headers={"Location": "https://example.test/new"}))

        assert await SampleProvider()._get(URL, follow_redirects=False) is None
        assert len(fake_network.requests) == 1

    async def test_user_agent_rotates_within_the_known_list(self, fake_network):
        fake_network.get(URL, text("", 503))

        await SampleProvider()._get(URL)

        agents = {request.headers["User-Agent"] for request in fake_network.requests}
        assert agents <= set(SampleProvider.USER_AGENTS)

    async def test_custom_user_agent_is_kept_on_every_attempt(self, fake_network):
        fake_network.get(URL, text("", 503))

        await SampleProvider()._get(URL, headers={"User-Agent": "Fonrex/1.0", "X-Extra": "1"})

        assert [r.headers["User-Agent"] for r in fake_network.requests] == ["Fonrex/1.0"] * 3
        assert fake_network.requests[0].headers["X-Extra"] == "1"

    async def test_shared_semaphore_is_released_after_each_call(self, fake_network):
        class Limited(SampleProvider):
            _semaphore = asyncio.Semaphore(1)

        fake_network.get(URL, text("", 503), text("ok"))
        provider = Limited()

        assert await provider._get(URL) == "ok"
        assert await provider._get(URL) == "ok"
        assert not Limited._semaphore.locked()


class TestGetJson:
    async def test_valid_json(self, fake_network):
        fake_network.get(URL, text('{"quotes": [1, 2]}'))

        assert await SampleProvider()._get_json(URL) == {"quotes": [1, 2]}

    async def test_invalid_json_gives_none(self, fake_network):
        fake_network.get(URL, text("<html>blocked</html>"))

        assert await SampleProvider()._get_json(URL) is None

    async def test_failed_request_gives_none(self, fake_network):
        fake_network.get(URL, text("", 404))

        assert await SampleProvider()._get_json(URL) is None


class TestPostJson:
    async def test_body_is_sent_as_json(self, fake_network):
        fake_network.post(URL, httpx.Response(200, json=[{"figi": "BBG000"}]))

        result = await SampleProvider()._post_json(URL, [{"idValue": "NL0000235190"}])

        request = fake_network.requests[0]
        assert result == [{"figi": "BBG000"}]
        assert request.headers["Content-Type"] == "application/json"
        assert request.content == b'[{"idValue": "NL0000235190"}]'

    async def test_non_json_answer_is_returned_as_text(self, fake_network):
        fake_network.post(URL, text("plain answer"))

        assert await SampleProvider()._post_json(URL, {}) == "plain answer"

    @pytest.mark.parametrize("code", [400, 401, 403, 404])
    async def test_client_errors_are_not_retried(self, fake_network, code):
        fake_network.post(URL, text("", code))

        assert await SampleProvider()._post_json(URL, {}) is None
        assert len(fake_network.requests) == 1

    async def test_server_error_is_retried(self, fake_network):
        fake_network.post(URL, text("", 503), httpx.Response(200, json={"ok": True}))

        assert await SampleProvider()._post_json(URL, {}) == {"ok": True}
        assert fake_network.sleeps == [1.0]

    async def test_persistent_server_error_gives_none(self, fake_network):
        fake_network.post(URL, text("", 500))

        assert await SampleProvider()._post_json(URL, {}) is None
        assert len(fake_network.requests) == 3

    async def test_timeout_is_retried(self, fake_network):
        fake_network.post(URL, httpx.ReadTimeout("timed out"), httpx.Response(200, json={"ok": 1}))

        assert await SampleProvider()._post_json(URL, {}) == {"ok": 1}

    async def test_unexpected_error_gives_none(self, fake_network):
        fake_network.post(URL, RuntimeError("boom"))

        assert await SampleProvider()._post_json(URL, {}) is None
        assert len(fake_network.requests) == 1

    async def test_other_status_gives_none(self, fake_network):
        fake_network.post(URL, text("", 204))

        assert await SampleProvider()._post_json(URL, {}) is None

    async def test_custom_user_agent_is_kept(self, fake_network):
        fake_network.post(URL, httpx.Response(200, json={}))

        await SampleProvider()._post_json(URL, {}, headers={"User-Agent": "Fonrex/1.0"})

        assert fake_network.requests[0].headers["User-Agent"] == "Fonrex/1.0"

    async def test_shared_semaphore_is_released(self, fake_network):
        class Limited(SampleProvider):
            _semaphore = asyncio.Semaphore(1)

        fake_network.post(URL, httpx.Response(200, json={"ok": True}))

        assert await Limited()._post_json(URL, {}) == {"ok": True}
        assert not Limited._semaphore.locked()


class TestEntryPoints:
    async def test_fetch_delegates_to_get_financials(self):
        assert await SampleProvider().fetch(ticker="AIR") == {"ticker": "AIR"}
        assert await SampleProvider().fetch(isin="NL0000235190") == {"ticker": "NL0000235190"}

    async def test_get_financials_delegates_to_fetch(self):
        class FetchOnly(BaseFinancialProvider):
            async def fetch(self, ticker=None, isin=None, provider_url=None, **kwargs):
                return {"fetched": ticker}

        assert await FetchOnly().get_financials("AIR") == {"fetched": "AIR"}

    async def test_provider_without_any_implementation_is_rejected(self):
        with pytest.raises(NotImplementedError):
            await BaseFinancialProvider().fetch(ticker="AIR")
        with pytest.raises(NotImplementedError):
            await BaseFinancialProvider().get_financials("AIR")

    def test_legacy_alias(self):
        assert BaseProvider is BaseFinancialProvider


class TestConversions:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("12,5", 12.5),
            ("1 234,56", 1234.56),
            ("1 234,5", 1234.5),
            ("3,4 %", 3.4),
            ("$8.72", 8.72),
            ("189,30 €", 189.3),
            (7, 7.0),
        ],
    )
    def test_safe_float(self, value, expected):
        assert SampleProvider()._safe_float(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [None, "", "N/A", "n/a", "-", "—", "abc"])
    def test_safe_float_without_figure(self, value):
        assert SampleProvider()._safe_float(value) is None
        assert SampleProvider()._safe_float(value, default=0.0) == 0.0

    def test_safe_int(self):
        assert SampleProvider()._safe_int("1 252") == 1252
        assert SampleProvider()._safe_int("12,9") == 12
        assert SampleProvider()._safe_int("N/A", default=0) == 0
