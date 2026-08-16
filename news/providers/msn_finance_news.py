#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MSNFinanceNewsProvider — News agrégées par MSN Finance.

MSN Finance agrège Reuters, AP News, Bloomberg et d'autres.
Tentative via endpoint JSON interne, fallback HTML.
"""

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from financials.providers.base import BaseFinancialProvider
from schemas.news import RawNewsItem

logger = logging.getLogger(__name__)

_MSN_JSON_URL = "https://assets.msn.com/content/query/api/v1/query"


class MSNFinanceNewsProvider(BaseFinancialProvider):
    name = "msn_finance"
    timeout = 8.0

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

        # Priorité : endpoint JSON interne
        items = await self._fetch_json(symbol, limit)
        if not items:
            items = await self._fetch_html(symbol, limit)

        return items

    # ── JSON endpoint ─────────────────────────────────────────────────────────

    async def _fetch_json(self, ticker: str, limit: int) -> List[RawNewsItem]:
        base_ticker = re.sub(r"\.[A-Z]+$", "", ticker.upper())
        params = {
            "timeOut": "1000",
            "market": "en-us",
            "query": f'{{"topic":"finance/stockquote/{base_ticker}"}}',
            "apikey": "0QfOX3Vn51YCzitbLaRkTkVuZEHd3cZw",  # clé publique MSN
        }
        headers = self._get_headers(
            {
                "Accept": "application/json",
                "Origin": "https://www.msn.com",
                "Referer": "https://www.msn.com/",
            }
        )

        data = await self._get_json(_MSN_JSON_URL, headers=headers, params=params)
        if not data:
            return []

        # Structure MSN : {"subCards": [{"title": ..., "url": ..., ...}]}
        sub_cards = []
        if isinstance(data, dict):
            sub_cards = data.get("subCards") or data.get("value") or data.get("items") or []

        items: List[RawNewsItem] = []
        for card in sub_cards[:limit]:
            if not isinstance(card, dict):
                continue
            title = card.get("title") or card.get("headline")
            url = card.get("url") or card.get("link") or card.get("webUrl")
            if not title or not url:
                continue

            published_at = self._parse_msn_date(
                card.get("publishedDateTime") or card.get("datePublished")
            )
            items.append(
                RawNewsItem(
                    title=title,
                    url=url,
                    source=card.get("provider") or card.get("source"),
                    published_at=published_at,
                    image_url=self._extract_image(card),
                    language="en",
                    provider=self.name,
                )
            )

        return items

    # ── HTML fallback ─────────────────────────────────────────────────────────

    async def _fetch_html(self, ticker: str, limit: int) -> List[RawNewsItem]:
        base_ticker = re.sub(r"\.[A-Z]+$", "", ticker.upper())
        url = f"https://www.msn.com/en-us/money/stockdetails/{base_ticker}"
        headers = self._get_headers({"Accept-Language": "en-US,en;q=0.9"})

        html = await self._get(url, headers=headers)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: List[RawNewsItem] = []

        for container in soup.select("div.newsitem, article, div[data-t='story']")[:limit]:
            link_tag = container.find("a", href=True)
            if not link_tag:
                continue

            article_url = link_tag["href"]
            if article_url.startswith("/"):
                article_url = "https://www.msn.com" + article_url

            title_tag = container.find(
                ["h3", "h4", "span"], class_=re.compile(r"title|headline", re.I)
            )
            title = title_tag.get_text(strip=True) if title_tag else link_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            time_tag = container.find("time")
            published_at = None
            if time_tag:
                dt_attr = time_tag.get("datetime")
                if dt_attr:
                    try:
                        published_at = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                        if published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

            items.append(
                RawNewsItem(
                    title=title,
                    url=article_url,
                    source="MSN Finance",
                    published_at=published_at,
                    language="en",
                    provider=self.name,
                )
            )

        return items

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse_msn_date(self, date_str: Optional[str]) -> Optional[datetime]:
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    def _extract_image(self, card: dict) -> Optional[str]:
        # Try various MSN image fields
        image = card.get("imageUrl") or card.get("image") or card.get("thumbnail")
        if isinstance(image, dict):
            return image.get("url") or image.get("imageUrl")
        return image if isinstance(image, str) else None
