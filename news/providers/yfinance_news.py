#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
YFinanceNewsProvider — news from yfinance ticker.news

The simplest source: no scraping, native JSON.
Worldwide coverage. About 50 recent articles per asset.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

import yfinance as yf

from concurrency import run_sync
from financials.providers.base import BaseFinancialProvider
from schemas.news import RawNewsItem

logger = logging.getLogger(__name__)


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
            raw_news = await run_sync(self._fetch_sync, symbol)
        except Exception as exc:
            logger.warning("[yfinance] News fetch error for %s: %s", symbol, exc)
            return []

        if not raw_news:
            return []

        items: List[RawNewsItem] = []
        for article in raw_news[:limit]:
            try:
                items.append(self._map_article(article))
            except Exception as exc:
                logger.debug("[yfinance] Cannot map an article: %s", exc)

        return items

    # ── sync helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_sync(symbol: str):
        """Blocking yfinance call — run in a thread pool."""
        t = yf.Ticker(symbol)
        return t.news or []

    def _map_article(self, article: dict) -> RawNewsItem:
        # Timestamp Unix → datetime UTC
        epoch = article.get("providerPublishTime")
        published_at: Optional[datetime] = None
        if epoch and epoch > 0:
            published_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

        # Image URL from the thumbnail
        image_url: Optional[str] = None
        thumbnail = article.get("thumbnail") or {}
        resolutions = thumbnail.get("resolutions") or []
        if resolutions:
            image_url = resolutions[0].get("url")

        return RawNewsItem(
            title=article.get("title", ""),
            url=article.get("link", ""),
            source=article.get("publisher"),
            published_at=published_at,
            image_url=image_url,
            related_tickers=article.get("relatedTickers") or [],
            language="en",
            provider=self.name,
        )
