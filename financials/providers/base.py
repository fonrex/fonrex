"""
BaseFinancialProvider — classe abstraite commune à tous les providers Fonrex.

Standardise : headers HTTP, retry/backoff, rate limiting, logging, helpers.
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
    Classe de base pour tous les providers financiers Fonrex.

    Fonctionnalités communes :
    - Rotation du User-Agent à chaque requête
    - Retry automatique avec backoff exponentiel (3 tentatives par défaut)
    - Timeout configurable par provider
    - Rate limiting via asyncio.Semaphore (optionnel — définir _semaphore au niveau classe)
    - Logging structuré (provider, url, latence, statut)
    - Helpers de conversion (_safe_float, _safe_int)

    Usage minimal :
        class MonProvider(BaseFinancialProvider):
            name = "MonProvider"
            timeout = 8.0

            async def fetch(self, ticker=None, isin=None, **kwargs):
                html = await self._get("https://example.com/api?ticker=" + ticker)
                return self._parse(html)
    """

    name: str = "BaseProvider"
    timeout: float = 8.0
    max_retries: int = 3
    retry_delay: float = 1.0  # secondes, multiplié par 2^attempt
    _semaphore: Optional[asyncio.Semaphore] = None  # partagé entre instances du même provider

    # Codes HTTP qui déclenchent un retry
    _RETRY_STATUSES = {429, 500, 502, 503, 504}
    # Codes HTTP définitifs — pas de retry
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
        Point d'entrée principal du provider.
        Par défaut, tente d'appeler get_financials pour la rétrocompatibilité.
        """
        if type(self).get_financials != BaseFinancialProvider.get_financials:
            return await self.get_financials(ticker or isin)
        raise NotImplementedError(
            f"Provider {self.name} must implement fetch() or get_financials()"
        )

    async def get_financials(self, ticker: str) -> Optional[Any]:
        """Alias de fetch() — conservé pour la rétrocompatibilité."""
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
        Effectue une requête GET avec retry et backoff exponentiel.

        Politique de retry :
        - Retry sur : réseau KO, timeout, status 429/5xx
        - Pas de retry sur : 400, 401, 403, 404, 410 (erreurs client définitives)

        Returns:
            Contenu texte (HTML/JSON) ou None si toutes les tentatives échouent.
        """
        merged_headers = self._get_headers(headers)
        has_custom_ua = bool(headers and "User-Agent" in headers)
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries):
            # Tourner le User-Agent à chaque tentative sauf si un User-Agent spécifique est fourni
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
                    "[%s] Attempt %d/%d — réseau KO (%dms): %s",
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
                logger.error("[%s] Erreur inattendue _get(%s): %s", self.name, url, exc)
                return None

            latency = int((time.perf_counter() - t0) * 1000)

            if response.status_code == 200:
                logger.debug("[%s] GET %s → 200 (%dms)", self.name, url, latency)
                return response.text

            if response.status_code in self._TERMINAL_STATUSES:
                logger.warning(
                    "[%s] GET %s → %d (terminal, pas de retry)",
                    self.name,
                    url,
                    response.status_code,
                )
                return None

            if response.status_code in self._RETRY_STATUSES:
                wait = self.retry_delay * (2**attempt)
                # Respecter l'en-tête Retry-After si présent
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
                logger.warning(
                    "[%s] GET %s → %d, retry dans %.1fs (attempt %d/%d)",
                    self.name,
                    url,
                    response.status_code,
                    wait,
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(wait)
                continue

            # Autres codes (201, 301 non suivi, etc.)
            logger.warning(
                "[%s] GET %s → %d (%dms), abandon.",
                self.name,
                url,
                response.status_code,
                latency,
            )
            return None

        if last_exc:
            logger.error(
                "[%s] _get(%s) — toutes les tentatives ont échoué: %s",
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
        """Exécute la requête HTTP réelle (isolée pour faciliter les tests)."""
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
        Variante de _get() qui parse automatiquement la réponse JSON.

        Returns:
            dict/list parsé ou None si la requête ou le parsing échoue.
        """
        import json as _json

        text = await self._get(url, headers=headers, params=params)
        if text is None:
            return None
        try:
            return _json.loads(text)
        except (_json.JSONDecodeError, ValueError) as exc:
            logger.warning("[%s] _get_json(%s) — JSON invalide: %s", self.name, url, exc)
            return None

    async def _post_json(
        self,
        url: str,
        body: Any,
        headers: Optional[dict] = None,
    ) -> Optional[Any]:
        """
        Effectue une requête POST JSON avec retry.

        Returns:
            Réponse JSON parsée ou None.
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
                logger.error("[%s] Erreur _post_json(%s): %s", self.name, url, exc)
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
        Retourne les headers HTTP de base avec User-Agent aléatoire.
        Les headers dans `extra` ont la priorité.
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
        """Convertit une valeur en float de façon défensive."""
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
                .replace("\u202f", "")  # espace insécable fine
                .strip()
            )
            if cleaned in ("", "N/A", "n/a", "-", "—", "–"):
                return default
            return float(cleaned)
        except (ValueError, TypeError):
            return default

    def _safe_int(self, value: Any, default: Optional[int] = None) -> Optional[int]:
        """Convertit une valeur en int de façon défensive."""
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
