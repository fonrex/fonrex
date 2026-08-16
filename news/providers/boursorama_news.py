#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BoursoramaNewsProvider — News financières FR depuis Boursorama.

Source complémentaire de ZoneBourse, forte couverture marché français.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from financials.providers.base import BaseFinancialProvider
from schemas.news import RawNewsItem

logger = logging.getLogger(__name__)

_MONTHS_FR = {
    "janvier": 1,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
}


class BoursoramaNewsProvider(BaseFinancialProvider):
    name = "boursorama"
    timeout = 8.0
    BASE_URL = "https://www.boursorama.com"

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        provider_ticker: str = None,
        limit: int = 20,
        **kwargs,
    ) -> List[RawNewsItem]:
        """
        Récupère les news depuis Boursorama.
        """
        url = self._build_url(provider_url, provider_ticker, ticker)
        if not url:
            logger.debug("[boursorama] Impossible de construire l'URL pour %s", ticker)
            return []

        headers = self._get_headers(
            {
                "Accept-Language": "fr-FR,fr;q=0.9",
                "Referer": self.BASE_URL + "/",
            }
        )

        html = await self._get(url, headers=headers)
        if not html:
            return []

        return self._parse(html, limit)

    # ── URL building ──────────────────────────────────────────────────────────

    def _build_url(
        self,
        provider_url: Optional[str],
        provider_ticker: Optional[str],
        ticker: Optional[str],
    ) -> Optional[str]:
        if provider_url:
            base = provider_url.rstrip("/")
            if not base.endswith("/actualites"):
                base += "/actualites"
            return base
        if provider_ticker:
            return f"{self.BASE_URL}/cours/{provider_ticker}/actualites"
        if ticker:
            symbol = re.sub(r"\.[A-Z]+$", "", ticker.upper())
            return f"{self.BASE_URL}/recherche/?q={symbol}"
        return None

    # ── parsing ───────────────────────────────────────────────────────────────

    def _parse(self, html: str, limit: int) -> List[RawNewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: List[RawNewsItem] = []

        # Sélecteurs Boursorama actualités
        article_containers = (
            soup.select("div.c-list-info-item")
            or soup.select("article.c-article-item")
            or soup.select("div.l-list-news__item")
            or soup.select("li.news-item")
            or soup.select("div.u-news-item")
        )

        for container in article_containers[:limit]:
            link_tag = container.find("a", href=True)
            if not link_tag:
                continue

            url = link_tag["href"]
            if url.startswith("/"):
                url = self.BASE_URL + url

            # Titre
            title_tag = container.find(
                class_=re.compile(r"title|headline|link", re.I)
            ) or container.find(["h3", "h4", "h2"])
            title = title_tag.get_text(strip=True) if title_tag else link_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            # Date — priorité à l'attribut datetime ISO
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
                        published_at = self._parse_fr_date(time_tag.get_text(strip=True))
                else:
                    published_at = self._parse_fr_date(time_tag.get_text(strip=True))

            # Source
            source_tag = container.find(class_=re.compile(r"source|provider|author", re.I))
            source = source_tag.get_text(strip=True) if source_tag else "Boursorama"

            # Summary (optionnel)
            summary_tag = container.find(class_=re.compile(r"summary|excerpt|intro", re.I))
            summary = summary_tag.get_text(strip=True) if summary_tag else None

            items.append(
                RawNewsItem(
                    title=title,
                    url=url,
                    summary=summary,
                    source=source,
                    published_at=published_at,
                    language="fr",
                    provider=self.name,
                )
            )

        return items

    # ── date parsing ──────────────────────────────────────────────────────────

    def _parse_fr_date(self, date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        now = datetime.now(tz=timezone.utc)
        s = date_str.strip().lower()

        # ISO 8601
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            pass

        # "il y a Xh"
        m = re.match(r"il y a (\d+)\s*h", s)
        if m:
            return now - timedelta(hours=int(m.group(1)))

        m = re.match(r"il y a (\d+)\s*min", s)
        if m:
            return now - timedelta(minutes=int(m.group(1)))

        # "hier"
        if "hier" in s:
            return (now - timedelta(days=1)).replace(hour=12, minute=0, second=0)

        # "15 mai 2026"
        for month_name, month_num in _MONTHS_FR.items():
            m = re.search(rf"(\d{{1,2}})\s+{re.escape(month_name)}\s+(\d{{4}})", s)
            if m:
                try:
                    return datetime(
                        int(m.group(2)), month_num, int(m.group(1)), tzinfo=timezone.utc
                    )
                except ValueError:
                    pass

        # "16/05/2026"
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if m:
            try:
                return datetime(
                    int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc
                )
            except ValueError:
                pass

        return None
