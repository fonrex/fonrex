#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MarketWatchNewsProvider — News depuis MarketWatch.

URL pattern : https://www.marketwatch.com/investing/stock/{ticker}/news
"""

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from financials.providers.base import BaseFinancialProvider
from schemas.news import RawNewsItem

logger = logging.getLogger(__name__)

# Mapping suffix Yahoo → countrycode MarketWatch
MW_COUNTRY_MAP: dict = {
    ".PA": "fr",
    ".AS": "nl",
    ".BR": "be",
    ".DE": "de",
    ".F": "de",
    ".L": "uk",
    ".MI": "it",
    ".MC": "es",
    ".ST": "se",
    ".HE": "fi",
    ".SW": "ch",
    ".TO": "ca",
}


class MarketWatchNewsProvider(BaseFinancialProvider):
    name = "marketwatch"
    timeout = 10.0
    BASE_URL = "https://www.marketwatch.com/investing/stock"

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        limit: int = 20,
        **kwargs,
    ) -> List[RawNewsItem]:
        symbol = ticker or isin
        if not symbol:
            return []

        url, params = self._build_url(symbol)
        if not url:
            return []

        headers = self._get_headers(
            {
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.marketwatch.com/",
            }
        )

        html = await self._get(url, headers=headers, params=params)
        if not html:
            return []

        return self._parse(html, limit)

    # ── URL building ──────────────────────────────────────────────────────────

    def _build_url(self, ticker: str):
        """
        ticker US (sans suffix) : /stock/{ticker.lower()}/news
        ticker EU avec suffix   : /stock/{base}/news?countrycode={cc}
        """
        params: Optional[dict] = None
        base_ticker = ticker
        for suffix, cc in MW_COUNTRY_MAP.items():
            if ticker.upper().endswith(suffix.upper()):
                base_ticker = ticker[: -len(suffix)]
                params = {"countrycode": cc}
                break

        url = f"{self.BASE_URL}/{base_ticker.lower()}/news"
        return url, params

    # ── parsing ───────────────────────────────────────────────────────────────

    def _parse(self, html: str, limit: int) -> List[RawNewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: List[RawNewsItem] = []

        article_containers = (
            soup.select("div.article__content")
            or soup.select("article.article--story")
            or soup.select("div.collection__elements article")
            or soup.select("div[data-layout='article']")
        )

        for container in article_containers[:limit]:
            link_tag = container.find("a", href=True)
            if not link_tag:
                continue

            url = link_tag["href"]
            if url.startswith("/"):
                url = "https://www.marketwatch.com" + url

            title_tag = container.find(
                class_=re.compile(r"headline|title", re.I)
            ) or container.find(["h3", "h4"])
            title = title_tag.get_text(strip=True) if title_tag else link_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            # Date
            published_at: Optional[datetime] = None
            time_tag = container.find("time") or container.find(
                class_=re.compile(r"timestamp|pubdate", re.I)
            )
            if time_tag:
                dt_attr = time_tag.get("datetime") or time_tag.get("data-est")
                if dt_attr:
                    try:
                        published_at = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                        if published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

            # Author / source
            author_tag = container.find(class_=re.compile(r"author|byline", re.I))
            author = author_tag.get_text(strip=True) if author_tag else None

            items.append(
                RawNewsItem(
                    title=title,
                    url=url,
                    author=author,
                    source="MarketWatch",
                    published_at=published_at,
                    language="en",
                    provider=self.name,
                )
            )

        return items
