"""
BaseFinancialProvider — abstract base class common to all Fonrex providers.

Standardizes: HTTP headers, retry/backoff, rate limiting, logging, helpers.

Every HTTP request of a provider goes through this module. It is the single
place where the retry policy, the limit of simultaneous requests per provider
and the optional outbound proxy are applied: a provider must never create its
own HTTP client (``tests/test_provider_http_policy.py`` enforces it).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import weakref
from abc import ABC
from collections.abc import Callable
from typing import Any, Optional, Union

import httpx

from financials.numbers import parse_number
from settings import env_int

logger = logging.getLogger(__name__)

# Outbound proxy used by the providers (your own proxy, or a relay service).
# Empty: direct connection.
PROXY_URL_ENV_VAR = "FONREX_PROXY_URL"
# Optional comma-separated list of provider names: when set, only these
# providers use the proxy and the others keep a direct connection.
PROXY_PROVIDERS_ENV_VAR = "FONREX_PROXY_PROVIDERS"
# Requests one provider may run at the same time (per provider, per process).
MAX_CONCURRENCY_ENV_VAR = "FONREX_PROVIDER_MAX_CONCURRENCY"
DEFAULT_MAX_CONCURRENCY = 4

# A server asking to wait longer than this is not waited for: the caller gives up
# on this provider sooner than that anyway.
MAX_RETRY_AFTER_SECONDS = 10.0

# One limiter per (event loop, provider class): asyncio primitives cannot be
# shared between event loops.
_LIMITERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[type, asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)

HeadersArg = Union[dict, Callable[[], dict], None]


def configured_proxy_url(provider_name: str) -> Optional[str]:
    """Return the outbound proxy URL for a provider, or None for a direct connection."""
    url = (os.environ.get(PROXY_URL_ENV_VAR) or "").strip()
    if not url:
        return None
    only = {
        name.strip().lower()
        for name in (os.environ.get(PROXY_PROVIDERS_ENV_VAR) or "").split(",")
        if name.strip()
    }
    if only and provider_name.lower() not in only:
        return None
    return url


def _pause(retry_after: Optional[str], backoff: float) -> float:
    """Pause before the next attempt: the one the server asks for, within a limit.

    ``Retry-After`` may also be a date, or anything: the usual pause is then kept.
    """
    seconds = parse_number(retry_after, decimal=".") if retry_after else None
    if seconds is None or seconds < 0:
        return backoff
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _client_options(provider_name: str, timeout: float, **options: Any) -> dict:
    """Options shared by every HTTP client created for a provider."""
    kwargs = {"timeout": timeout, **{k: v for k, v in options.items() if v is not None}}
    proxy = configured_proxy_url(provider_name)
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def new_sync_client(
    provider_name: str,
    timeout: float,
    *,
    follow_redirects: bool = True,
    headers: Optional[dict] = None,
) -> httpx.Client:
    """Create the HTTP client of a synchronous provider (same proxy rules)."""
    return httpx.Client(
        **_client_options(
            provider_name, timeout, follow_redirects=follow_redirects, headers=headers
        )
    )


class ProviderSession:
    """HTTP session of one provider call: several requests sharing cookies.

    ``get`` and ``post`` return the ``httpx.Response`` like an ``httpx.AsyncClient``
    would, with the common policy applied on top:

    - at most N requests of the provider run at the same time;
    - a network failure, a timeout or a 429/5xx answer is retried with an
      exponential pause (``Retry-After`` is honoured, up to a limit);
    - any other answer, including 401/403/404, is returned at once: the provider
      decides what to do with it;
    - after the last attempt a retryable answer is returned as is, and a
      network failure is raised, exactly as a plain client would.
    """

    def __init__(self, provider: BaseFinancialProvider, client: httpx.AsyncClient) -> None:
        self._provider = provider
        self._client = client

    async def get(
        self, url: str, *, headers: HeadersArg = None, params: Optional[dict] = None
    ) -> httpx.Response:
        return await self.request("GET", url, headers=headers, params=params)

    async def post(
        self,
        url: str,
        *,
        headers: HeadersArg = None,
        params: Optional[dict] = None,
        data: Any = None,
        json: Any = None,
        content: Any = None,
    ) -> httpx.Response:
        return await self.request(
            "POST", url, headers=headers, params=params, data=data, json=json, content=content
        )

    async def request(
        self, method: str, url: str, *, headers: HeadersArg = None, **kwargs: Any
    ) -> httpx.Response:
        provider = self._provider
        attempts = max(1, int(provider.max_retries))
        kwargs = {key: value for key, value in kwargs.items() if value is not None}

        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            # A callable gives fresh headers (e.g. another User-Agent) at each attempt.
            request_headers = headers() if callable(headers) else headers
            t0 = time.perf_counter()
            try:
                async with provider._limit():
                    response = await self._client.request(
                        method, url, headers=request_headers, **kwargs
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                latency = int((time.perf_counter() - t0) * 1000)
                logger.warning(
                    "[%s] %s %s — network error on attempt %d/%d (%dms): %s",
                    provider.provider_key(),
                    method,
                    url,
                    attempt + 1,
                    attempts,
                    latency,
                    exc,
                )
                if last_attempt:
                    raise
                await asyncio.sleep(provider.retry_delay * (2**attempt))
                continue

            if response.status_code not in provider._RETRY_STATUSES or last_attempt:
                return response

            wait = provider.retry_delay * (2**attempt)
            wait = _pause(response.headers.get("Retry-After"), wait)
            logger.warning(
                "[%s] %s %s → %d, retry in %.1fs (attempt %d/%d)",
                provider.provider_key(),
                method,
                url,
                response.status_code,
                wait,
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(wait)

        raise AssertionError("unreachable")  # pragma: no cover


class BaseFinancialProvider(ABC):
    """
    Base class for all Fonrex financial providers.

    Common features:
    - User-Agent rotation on each request
    - Automatic retry with exponential backoff (3 attempts by default)
    - Configurable timeout per provider
    - Limit of simultaneous requests per provider (FONREX_PROVIDER_MAX_CONCURRENCY,
      or a class-level _semaphore to choose a provider-specific limit)
    - Optional outbound proxy (FONREX_PROXY_URL, FONREX_PROXY_PROVIDERS)
    - Shared session for a search followed by a page (cookies kept): _session()
    - Structured logging (provider, url, latency, status)
    - Type conversion helpers (_safe_float, _safe_int)

    Minimal usage:
        class MyProvider(BaseFinancialProvider):
            name = "MyProvider"
            timeout = 8.0

            async def fetch(self, ticker=None, isin=None, **kwargs):
                html = await self._get("https://example.com/api?ticker=" + ticker)
                return self._parse(html)

    Several requests in one call (search, then page):
            async with self._session() as client:
                found = await client.get(SEARCH_URL, params={"q": ticker})
                page = await client.get(found.json()["url"])
    """

    name: str = "BaseProvider"
    timeout: float = 8.0
    max_retries: int = 3
    retry_delay: float = 1.0  # seconds, multiplied by 2^attempt
    _semaphore: Optional[asyncio.Semaphore] = None  # shared between instances of the same provider

    # HTTP status codes that trigger a retry
    _RETRY_STATUSES = {429, 500, 502, 503, 504}
    # Terminal HTTP status codes — no retry
    _TERMINAL_STATUSES = {400, 401, 403, 404, 410}

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    ]

    # ── Identity, limiter and client ─────────────────────────────────────────

    @classmethod
    def provider_key(cls) -> str:
        """Name used in logs and settings: ``name``, or the class name without ``Provider``."""
        if cls.name != BaseFinancialProvider.name:
            return cls.name
        return cls.__name__.removesuffix("Provider") or cls.__name__

    @classmethod
    def _limit(cls) -> asyncio.Semaphore:
        """Return the limiter of simultaneous requests for this provider."""
        if cls._semaphore is not None:
            return cls._semaphore
        loop = asyncio.get_running_loop()
        per_class = _LIMITERS.setdefault(loop, {})
        limiter = per_class.get(cls)
        if limiter is None:
            limiter = asyncio.Semaphore(
                env_int(MAX_CONCURRENCY_ENV_VAR, DEFAULT_MAX_CONCURRENCY, minimum=1, maximum=64)
            )
            per_class[cls] = limiter
        return limiter

    def _new_client(
        self, *, follow_redirects: bool = True, headers: Optional[dict] = None
    ) -> httpx.AsyncClient:
        """Create the HTTP client of this provider: the only place where one is built."""
        return httpx.AsyncClient(
            **_client_options(
                self.provider_key(),
                self.timeout,
                follow_redirects=follow_redirects,
                headers=headers,
            )
        )

    def _session(
        self, *, follow_redirects: bool = True, headers: Optional[dict] = None
    ) -> _SessionContext:
        """Open a session for several requests sharing cookies and default headers."""
        return _SessionContext(
            self, self._new_client(follow_redirects=follow_redirects, headers=headers)
        )

    # ── Abstract interface ────────────────────────────────────────────────────

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        **kwargs,
    ) -> Optional[Any]:
        """
        Main provider entry point.
        By default, attempts to call get_financials for backward compatibility.
        """
        if type(self).get_financials != BaseFinancialProvider.get_financials:
            return await self.get_financials(ticker or isin)
        raise NotImplementedError(
            f"Provider {self.name} must implement fetch() or get_financials()"
        )

    async def get_financials(self, ticker: str) -> Optional[Any]:
        """Alias for fetch() — retained for backward compatibility."""
        if type(self).fetch != BaseFinancialProvider.fetch:
            return await self.fetch(ticker=ticker)
        raise NotImplementedError(
            f"Provider {self.name} must implement fetch() or get_financials()"
        )

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _get(
        self,
        url: str,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        follow_redirects: bool = True,
    ) -> Optional[str]:
        """
        Executes a GET request with retry and exponential backoff.

        Retry policy:
        - Retry on: network failure, timeout, status 429/5xx
        - No retry on: 400, 401, 403, 404, 410 (terminal client errors)

        Returns:
            Text content (HTML/JSON) or None if all attempts fail.
        """
        merged_headers = self._get_headers(headers)
        has_custom_ua = bool(headers and "User-Agent" in headers)
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries):
            # No pause after the last attempt: nothing follows it.
            last_attempt = attempt == self.max_retries - 1
            # Rotate User-Agent on each attempt unless a specific User-Agent is provided
            if not has_custom_ua:
                merged_headers["User-Agent"] = random.choice(self.USER_AGENTS)
            t0 = time.perf_counter()

            try:
                async with self._limit():
                    response = await self._execute_get(
                        url, merged_headers, params, follow_redirects
                    )
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                latency = int((time.perf_counter() - t0) * 1000)
                logger.warning(
                    "[%s] Attempt %d/%d — network error (%dms): %s",
                    self.name,
                    attempt + 1,
                    self.max_retries,
                    latency,
                    exc,
                )
                last_exc = exc
                if not last_attempt:
                    await asyncio.sleep(self.retry_delay * (2**attempt))
                continue
            except Exception as exc:
                logger.error("[%s] Unexpected error _get(%s): %s", self.name, url, exc)
                return None

            latency = int((time.perf_counter() - t0) * 1000)

            if response.status_code == 200:
                logger.debug("[%s] GET %s → 200 (%dms)", self.name, url, latency)
                return response.text

            if response.status_code in self._TERMINAL_STATUSES:
                logger.warning(
                    "[%s] GET %s → %d (terminal, no retry)",
                    self.name,
                    url,
                    response.status_code,
                )
                return None

            if response.status_code in self._RETRY_STATUSES:
                wait = self.retry_delay * (2**attempt)
                # Respect Retry-After header if present
                wait = _pause(response.headers.get("Retry-After"), wait)
                if last_attempt:
                    logger.warning(
                        "[%s] GET %s → %d after %d attempts, giving up.",
                        self.name,
                        url,
                        response.status_code,
                        self.max_retries,
                    )
                    return None
                logger.warning(
                    "[%s] GET %s → %d, retry in %.1fs (attempt %d/%d)",
                    self.name,
                    url,
                    response.status_code,
                    wait,
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(wait)
                continue

            # Other status codes (201, un-followed 301, etc.)
            logger.warning(
                "[%s] GET %s → %d (%dms), giving up.",
                self.name,
                url,
                response.status_code,
                latency,
            )
            return None

        # Only a network failure at every attempt reaches this point.
        logger.error("[%s] _get(%s) — all attempts failed: %s", self.name, url, last_exc)
        return None

    async def _execute_get(
        self,
        url: str,
        headers: dict,
        params: Optional[dict],
        follow_redirects: bool,
    ) -> httpx.Response:
        """Executes the actual HTTP request (isolated for easier testing)."""
        async with self._new_client(follow_redirects=follow_redirects) as client:
            return await client.get(url, headers=headers, params=params)

    async def _get_json(
        self,
        url: str,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        Variant of _get() that automatically parses JSON response.

        Returns:
            Parsed dict/list or None if request or parsing fails.
        """
        import json as _json

        text = await self._get(url, headers=headers, params=params)
        if text is None:
            return None
        try:
            return _json.loads(text)
        except (_json.JSONDecodeError, ValueError) as exc:
            logger.warning("[%s] _get_json(%s) — invalid JSON: %s", self.name, url, exc)
            return None

    async def _post_json(
        self,
        url: str,
        body: Any,
        headers: Optional[dict] = None,
    ) -> Optional[Any]:
        """
        Executes a JSON POST request with retry.

        Returns:
            Parsed JSON response or None.
        """
        import json as _json

        merged_headers = self._get_headers(headers)
        has_custom_ua = bool(headers and "User-Agent" in headers)
        merged_headers["Content-Type"] = "application/json"

        for attempt in range(self.max_retries):
            last_attempt = attempt == self.max_retries - 1
            if not has_custom_ua:
                merged_headers["User-Agent"] = random.choice(self.USER_AGENTS)
            t0 = time.perf_counter()
            try:
                async with self._limit():
                    async with self._new_client(follow_redirects=False) as client:
                        resp = await client.post(
                            url,
                            content=_json.dumps(body).encode(),
                            headers=merged_headers,
                        )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                latency = int((time.perf_counter() - t0) * 1000)
                logger.warning(
                    "[%s] POST attempt %d/%d KO (%dms): %s",
                    self.name,
                    attempt + 1,
                    self.max_retries,
                    latency,
                    exc,
                )
                if not last_attempt:
                    await asyncio.sleep(self.retry_delay * (2**attempt))
                continue
            except Exception as exc:
                logger.error("[%s] Error _post_json(%s): %s", self.name, url, exc)
                return None

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return resp.text
            if resp.status_code in self._TERMINAL_STATUSES:
                logger.warning("[%s] POST %s → %d (terminal)", self.name, url, resp.status_code)
                return None
            if resp.status_code in self._RETRY_STATUSES:
                if not last_attempt:
                    await asyncio.sleep(self.retry_delay * (2**attempt))
                continue
            return None

        return None

    # ── Header helpers ────────────────────────────────────────────────────────

    def _get_headers(self, extra: Optional[dict] = None) -> dict:
        """
        Returns base HTTP headers with random User-Agent.
        Headers in `extra` take precedence.
        """
        base = {
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8"
            ),
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            # No Accept-Encoding here: httpx advertises the encodings it can really
            # decode. Announcing "br" without a Brotli decoder installed would make a
            # compliant server send a body that cannot be read.
            "DNT": "1",
            "Connection": "keep-alive",
        }
        if extra:
            base.update(extra)
        return base

    # ── Type conversion helpers ───────────────────────────────────────────────

    def _safe_float(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        """Read a displayed number (``financials/numbers.py``), or return ``default``."""
        number = parse_number(value, decimal=",")
        return default if number is None else number

    def _safe_int(self, value: Any, default: Optional[int] = None) -> Optional[int]:
        """Read a displayed number as an integer (truncated), or return ``default``."""
        number = parse_number(value, decimal=",")
        return default if number is None else int(number)


class _SessionContext:
    """``async with provider._session() as client`` — closes the HTTP client on exit."""

    def __init__(self, provider: BaseFinancialProvider, client: httpx.AsyncClient) -> None:
        self._provider = provider
        self._client = client

    async def __aenter__(self) -> ProviderSession:
        await self._client.__aenter__()
        return ProviderSession(self._provider, self._client)

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._client.__aexit__(*exc_info)


# ── Backward compatibility alias ────────────────────────────────────────────────
BaseProvider = BaseFinancialProvider

