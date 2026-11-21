#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
YFinanceNewsProvider — news from yfinance Ticker.get_news

The simplest source: no scraping, native JSON.
Worldwide coverage. Asks Yahoo for as many recent articles as the caller's
limit (yfinance would return 10 otherwise).

Since yfinance 1.8.0 an article is ``{"id": ..., "content": {...}}`` (Yahoo's
GraphQL news); the flat shape of older versions (``title``, ``link``,
``providerPublishTime``...) is still read.
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import yfinance as yf

from concurrency import run_sync
from financials.providers.base import BaseFinancialProvider
from schemas.news import RawNewsItem

logger = logging.getLogger(__name__)


def _first_image_url(thumbnail: Any) -> Optional[str]:
    """URL of the first resolution of a Yahoo thumbnail, None when absent."""
    resolutions = (thumbnail or {}).get("resolutions") or []
    if resolutions:
        return (resolutions[0] or {}).get("url") or None
    return None


def _parse_pub_date(value: Any) -> Optional[datetime]:
    """ISO 8601 ``pubDate`` (``2026-11-12T13:35:01Z``) → aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class YFinanceNewsProvider(BaseFinancialProvider):
    name = "yfinance"
    timeout = 10.0

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        limit: int = 20,
        **kwargs,
    ) -> List[RawNewsItem]:
        """
        News from yfinance.
        yfinance is synchronous: the call goes through the shared worker pool.
        """
        symbol = ticker or isin
        if not symbol:
            return []

        try:
            raw_news = await run_sync(self._fetch_sync, symbol, limit)
        except Exception as exc:
            logger.warning("[yfinance] News fetch error for %s: %s", symbol, exc)
            return []

        if not raw_news:
            return []

        items: List[RawNewsItem] = []
        for article in raw_news[:limit]:
            try:
                item = self._map_article(article)
            except Exception as exc:
                logger.debug("[yfinance] Cannot map an article: %s", exc)
                continue
            if item is not None:
                items.append(item)

        return items

    # ── sync helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_sync(symbol: str, limit: int):
        """Blocking yfinance call — run in a thread pool."""
        t = yf.Ticker(symbol)
        return t.get_news(count=limit) or []

    def _map_article(self, article: dict) -> Optional[RawNewsItem]:
        """One yfinance article → RawNewsItem; None when it has no URL."""
        content = article.get("content")
        if isinstance(content, dict):
            return self._map_content(content)
        return self._map_flat(article)

    def _map_content(self, c: dict) -> Optional[RawNewsItem]:
        """Shape of yfinance >= 1.8.0: ``{"id": ..., "content": {...}}``.

        Stories, videos and outlinks are all kept: each has a title, a
        publisher and a page on Yahoo or the publisher's site.
        """
        url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get(
            "url"
        )
        if not url:
            return None

        tickers = ((c.get("finance") or {}).get("stockTickers")) or []
        return RawNewsItem(
            title=c.get("title") or "",
            url=url,
            summary=c.get("summary") or c.get("description") or None,
            source=(c.get("provider") or {}).get("displayName"),
            published_at=_parse_pub_date(c.get("pubDate")),
            image_url=_first_image_url(c.get("thumbnail")),
            related_tickers=[
                t["symbol"] for t in tickers if isinstance(t, dict) and t.get("symbol")
            ],
            language="en",
            provider=self.name,
        )

    def _map_flat(self, article: dict) -> Optional[RawNewsItem]:
        """Flat shape of yfinance < 1.8.0 (``title``, ``link``, epoch time)."""
        url = article.get("link")
        if not url:
            return None

        epoch = article.get("providerPublishTime")
        published_at: Optional[datetime] = None
        if epoch and epoch > 0:
            published_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

        return RawNewsItem(
            title=article.get("title") or "",
            url=url,
            source=article.get("publisher"),
            published_at=published_at,
            image_url=_first_image_url(article.get("thumbnail")),
            related_tickers=article.get("relatedTickers") or [],
            language="en",
            provider=self.name,
        )
