"""
BaseFinancialProvider — abstract base class common to all Fonrex providers.

Standardizes: HTTP headers, retry/backoff, rate limiting, logging, helpers.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class BaseFinancialProvider(ABC):
    """
    Base class for all Fonrex financial providers.

    Common features:
    - User-Agent rotation on each request
    - Automatic retry with exponential backoff (3 attempts by default)
    - Configurable timeout per provider
    - Rate limiting via asyncio.Semaphore (optional — set _semaphore at class level)
    - Structured logging (provider, url, latency, status)
    - Type conversion helpers (_safe_float, _safe_int)

    Minimal usage:
        class MyProvider(BaseFinancialProvider):
            name = "MyProvider"
            timeout = 8.0

            async def fetch(self, ticker=None, isin=None, **kwargs):
                html = await self._get("https://example.com/api?ticker=" + ticker)
                return self._parse(html)
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
            # Rotate User-Agent on each attempt unless a specific User-Agent is provided
            if not has_custom_ua:
                merged_headers["User-Agent"] = random.choice(self.USER_AGENTS)
            t0 = time.perf_counter()

            try:
                if self._semaphore:
                    async with self._semaphore:
                        response = await self._execute_get(
                            url, merged_headers, params, follow_redirects
                        )
                else:
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
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
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

        if last_exc:
            logger.error(
                "[%s] _get(%s) — all attempts failed: %s",
                self.name,
                url,
                last_exc,
            )
        return None

    async def _execute_get(
        self,
        url: str,
        headers: dict,
        params: Optional[dict],
        follow_redirects: bool,
    ) -> httpx.Response:
        """Executes the actual HTTP request (isolated for easier testing)."""
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=follow_redirects,
        ) as client:
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
            if not has_custom_ua:
                merged_headers["User-Agent"] = random.choice(self.USER_AGENTS)
            t0 = time.perf_counter()
            try:
                if self._semaphore:
                    async with self._semaphore:
                        async with httpx.AsyncClient(timeout=self.timeout) as client:
                            resp = await client.post(
                                url,
                                content=_json.dumps(body).encode(),
                                headers=merged_headers,
                            )
                else:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
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
                wait = self.retry_delay * (2**attempt)
                await asyncio.sleep(wait)
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
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
        }
        if extra:
            base.update(extra)
        return base

    # ── Type conversion helpers ───────────────────────────────────────────────

    def _safe_float(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        """Defensively converts a value to float."""
        if value is None:
            return default
        try:
            cleaned = (
                str(value)
                .replace(",", ".")
                .replace(" ", "")
                .replace("%", "")
                .replace("€", "")
                .replace("$", "")
                .replace("\u202f", "")  # narrow non-breaking space
                .strip()
            )
            if cleaned in ("", "N/A", "n/a", "-", "—", "–"):
                return default
            return float(cleaned)
        except (ValueError, TypeError):
            return default

    def _safe_int(self, value: Any, default: Optional[int] = None) -> Optional[int]:
        """Defensively converts a value to int."""
        f = self._safe_float(value)
        if f is None:
            return default
        try:
            return int(f)
        except (ValueError, TypeError):
            return default


# ── Backward compatibility ────────────────────────────────────────────────────
# All existing providers import BaseProvider from this module.
BaseProvider = BaseFinancialProvider
