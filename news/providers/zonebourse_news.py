#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ZoneBourseNewsProvider — News financières EU/FR depuis ZoneBourse.

ZoneBourse est la meilleure source pour les actifs français et européens.
HTML propre, pas de JavaScript requis.
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
    "jan": 1,
    "fév": 2,
    "mar": 3,
    "avr": 4,
    "jun": 6,
    "jul": 7,
    "aoû": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "déc": 12,
}


class ZoneBourseNewsProvider(BaseFinancialProvider):
    name = "zonebourse"
    timeout = 8.0
    BASE_URL = "https://www.zonebourse.com"

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
        Récupère les news depuis ZoneBourse.
        """
        url = self._build_url(ticker, provider_url, provider_ticker)
        if not url:
            logger.debug("[zonebourse] Impossible de construire l'URL pour %s", ticker)
            return []

        headers = self._get_headers(
            {
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.5",
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
        ticker: Optional[str],
        provider_url: Optional[str],
        provider_ticker: Optional[str],
    ) -> Optional[str]:
        if provider_url:
            base = provider_url.rstrip("/")
            if not base.endswith("/actualites"):
                base += "/actualites/"
            return base
        if provider_ticker:
            return f"{self.BASE_URL}/cours/action/{provider_ticker}/actualites/"
        if ticker:
            # Retirer le suffix Yahoo pour la recherche
            symbol = re.sub(r"\.[A-Z]+$", "", ticker.upper())
            return f"{self.BASE_URL}/recherche/?q={symbol}"
        return None

    # ── parsing ───────────────────────────────────────────────────────────────

    def _parse(self, html: str, limit: int) -> List[RawNewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: List[RawNewsItem] = []

        # Sélecteurs possibles ZoneBourse
        article_containers = (
            soup.select("ul.liste-actus li")
            or soup.select("div.actu-item")
            or soup.select("article.news-item")
            or soup.select("div.article-item")
            or soup.select("li.list-news-item")
        )

        for container in article_containers[:limit]:
            link_tag = container.find("a", href=True)
            if not link_tag:
                continue

            url = link_tag["href"]
            if url.startswith("/"):
                url = self.BASE_URL + url

            # Titre
            title_tag = container.find(["h3", "h4", "h2"])
            title = title_tag.get_text(strip=True) if title_tag else link_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            # Date
            date_tag = container.find(["time", "span"], class_=re.compile(r"date|time|publi", re.I))
            if date_tag is None:
                date_tag = container.find("span", class_=re.compile(r"source", re.I))
            published_at = None
            if date_tag:
                date_str = date_tag.get("datetime") or date_tag.get_text(strip=True)
                published_at = self._parse_zonebourse_date(date_str)

            # Source
            source_tag = container.find(class_=re.compile(r"source|provider", re.I))
            source = source_tag.get_text(strip=True) if source_tag else "ZoneBourse"

            items.append(
                RawNewsItem(
                    title=title,
                    url=url,
                    source=source,
                    published_at=published_at,
                    language="fr",
                    provider=self.name,
                )
            )

        return items

    # ── date parsing ──────────────────────────────────────────────────────────

    def _parse_zonebourse_date(self, date_str: str) -> Optional[datetime]:
        """
        Parse les formats de date ZoneBourse :
        "16/05/2026"     → 2026-05-16
        "il y a 2h"      → now - 2h
        "hier"           → yesterday
        "15 mai 2026"    → 2026-05-15
        ISO 8601         → parsé directement
        """
        if not date_str:
            return None

        now = datetime.now(tz=timezone.utc)
        s = date_str.strip().lower()

        # ISO 8601
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
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

        # "il y a Xh" or "il y a X heures"
        m = re.match(r"il y a (\d+)\s*h", s)
        if m:
            return now - timedelta(hours=int(m.group(1)))

        m = re.match(r"il y a (\d+)\s*min", s)
        if m:
            return now - timedelta(minutes=int(m.group(1)))

        m = re.match(r"il y a (\d+)\s*jour", s)
        if m:
            return now - timedelta(days=int(m.group(1)))

        # "hier"
        if "hier" in s:
            return (now - timedelta(days=1)).replace(hour=12, minute=0, second=0)

        # "15 mai 2026" or "15 mai" (année courante)
        for month_name, month_num in _MONTHS_FR.items():
            pattern = rf"(\d{{1,2}})\s+{re.escape(month_name)}\s*(\d{{4}})?"
            m = re.search(pattern, s)
            if m:
                year = int(m.group(2)) if m.group(2) else now.year
                try:
                    return datetime(year, month_num, int(m.group(1)), tzinfo=timezone.utc)
                except ValueError:
                    pass

        return None
