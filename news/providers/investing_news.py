#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
InvestingComNewsProvider — News depuis Investing.com.

Couverture mondiale, excellente qualité.
Protection Cloudflare modérée — headers Chrome complets requis.
"""

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from financials.providers.base import BaseFinancialProvider
from schemas.news import RawNewsItem

logger = logging.getLogger(__name__)


class InvestingComNewsProvider(BaseFinancialProvider):
    name = "investing_com"
    timeout = 12.0
    BASE_URL = "https://www.investing.com/equities"

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        provider_ticker: str = None,
        limit: int = 20,
        **kwargs,
    ) -> List[RawNewsItem]:
        url = self._build_url(provider_url, provider_ticker)
        if not url:
            logger.debug("[investing_com] Pas d'URL disponible pour %s", ticker)
            return []

        headers = self._get_headers(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            }
        )

        html = await self._get(url, headers=headers)
        if not html:
            return []

        # Détecter Cloudflare / accès refusé
        if "Just a moment" in html or "cf-browser-verification" in html:
            logger.warning("[investing_com] Cloudflare détecté pour %s", url)
            return []

        return self._parse(html, limit)

    # ── URL building ──────────────────────────────────────────────────────────

    def _build_url(
        self,
        provider_url: Optional[str],
        provider_ticker: Optional[str],
    ) -> Optional[str]:
        if provider_url:
            base = provider_url.rstrip("/").rstrip("-news")
            return base + "-news"
        if provider_ticker:
            slug = provider_ticker.lower().rstrip("/")
            return f"{self.BASE_URL}/{slug}-news"
        return None

    # ── parsing ───────────────────────────────────────────────────────────────

    def _parse(self, html: str, limit: int) -> List[RawNewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: List[RawNewsItem] = []

        # Sélecteurs Investing.com articles news
        article_containers = (
            soup.select("div.articleItem")
            or soup.select("article.news-item")
            or soup.select("div[data-test='news-item']")
            or soup.select("li.js-article-item")
        )

        for container in article_containers[:limit]:
            link_tag = container.find("a", href=True)
            if not link_tag:
                continue

            url = link_tag["href"]
            if url.startswith("/"):
                url = "https://www.investing.com" + url

            # Titre
            title_tag = container.find(class_=re.compile(r"title|headline", re.I)) or link_tag
            title = title_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            # Date
            published_at: Optional[datetime] = None
            time_tag = container.find("time")
            if time_tag:
                dt_attr = time_tag.get("datetime")
                if dt_attr:
                    try:
                        published_at = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                        if published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

            # Source
            source_tag = container.find(
                class_=re.compile(r"source|publisher|provider|details", re.I)
            )
            source = source_tag.get_text(strip=True) if source_tag else "Investing.com"

            # Thumbnail
            img_tag = container.find("img")
            image_url = img_tag.get("src") or img_tag.get("data-src") if img_tag else None

            items.append(
                RawNewsItem(
                    title=title,
                    url=url,
                    source=source,
                    published_at=published_at,
                    image_url=image_url,
                    language="en",
                    provider=self.name,
                )
            )

        return items
