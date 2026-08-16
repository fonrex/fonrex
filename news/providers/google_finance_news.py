#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GoogleFinanceNewsProvider — News aggregated by Google Finance.

Google Finance aggregates articles from Reuters, Bloomberg, AP,
Barron's, WSJ and others without a paywall.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from financials.providers.base import BaseFinancialProvider
from schemas.news import RawNewsItem

logger = logging.getLogger(__name__)

# Mapping Yahoo suffix -> Google Finance exchange code
GOOGLE_EXCHANGE_MAP: dict = {
    ".PA": "EPA",
    ".AS": "AMS",
    ".BR": "EBR",
    ".DE": "ETR",
    ".F": "FRA",
    ".L": "LON",
    ".MI": "BIT",
    ".MC": "BME",
    ".ST": "STO",
    ".HE": "HEL",
    ".SW": "SWX",
    ".TO": "TSX",
    ".AX": "ASX",
    ".HK": "HKEX",
    ".T": "TYO",
}

# Mapping suffix -> language
SUFFIX_LANGUAGE_MAP: dict = {
    ".PA": "fr",
    ".AS": "nl",
    ".BR": "fr",
    ".DE": "de",
    ".F": "de",
    ".L": "en",
    ".MI": "it",
    ".MC": "es",
    ".ST": "sv",
    ".HE": "fi",
    ".SW": "de",
    ".TO": "en",
}


class GoogleFinanceNewsProvider(BaseFinancialProvider):
    name = "google_finance"
    timeout = 10.0
    BASE_URL = "https://www.google.com/finance/quote"

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

        goog_symbol, exchange = self._resolve_google_symbol(symbol)
        language = self._detect_language(symbol)
        url = f"{self.BASE_URL}/{goog_symbol}:{exchange}"

        headers = self._get_headers(
            {
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"
                if language == "fr"
                else "en-US,en;q=0.9",
            }
        )

        html = await self._get(url, headers=headers)
        if not html:
            return []

        # Tentative JSON d'abord, fallback HTML
        items = self._parse_json(html, language)
        if not items:
            items = self._parse_html(html, language)

        return items[:limit]

    # ── symbol resolution ─────────────────────────────────────────────────────

    def _resolve_google_symbol(self, ticker: str) -> Tuple[str, str]:
        """Convertit un ticker Yahoo en (symbol, exchange) Google Finance."""
        for suffix, exchange in GOOGLE_EXCHANGE_MAP.items():
            if ticker.upper().endswith(suffix.upper()):
                symbol = ticker[: -len(suffix)].upper()
                return symbol, exchange
        # US ticker — tenter NASDAQ par défaut
        return ticker.upper(), "NASDAQ"

    def _detect_language(self, ticker: str) -> str:
        ticker_upper = ticker.upper()
        for suffix, lang in SUFFIX_LANGUAGE_MAP.items():
            if ticker_upper.endswith(suffix.upper()):
                return lang
        return "en"

    # ── parsing JSON embedded ─────────────────────────────────────────────────

    def _parse_json(self, html: str, language: str) -> List[RawNewsItem]:
        """
        Tente d'extraire les news depuis les blocs JSON embarqués dans la page.
        Google Finance encode les données dans des tableaux JS.
        """
        items: List[RawNewsItem] = []

        # Pattern : chercher des arrays avec des objets {title, url, time, source}
        # Google Finance news data is often in AF_initDataCallback scripts
        pattern = re.compile(
            r'\["([^"]{10,500})",\s*"(https?://[^"]+)",\s*"([^"]*)",\s*"([^"]*)"',
            re.DOTALL,
        )
        for match in pattern.finditer(html):
            title, url, source, time_str = match.groups()
            if not url.startswith("http"):
                continue
            # Filtrer les faux positifs (liens CSS, JS, images)
            if any(ext in url for ext in (".css", ".js", ".png", ".jpg", ".svg")):
                continue
            published_at = self._parse_relative_date(time_str) if time_str else None
            items.append(
                RawNewsItem(
                    title=title,
                    url=url,
                    source=source or None,
                    published_at=published_at,
                    language=language,
                    provider=self.name,
                )
            )

        return items

    # ── parsing HTML ──────────────────────────────────────────────────────────

    def _parse_html(self, html: str, language: str) -> List[RawNewsItem]:
        """Fallback: scraping des blocs d'articles dans le DOM."""
        soup = BeautifulSoup(html, "html.parser")
        items: List[RawNewsItem] = []

        # Sélecteurs courants de Google Finance (peuvent changer)
        for container in soup.select("div.yY3Lee, div.Yfwt5, div[data-article-id]"):
            link_tag = container.find("a", href=True)
            title_tag = container.find(
                ["h3", "h4", "span", "div"], class_=re.compile(r"title|headline", re.I)
            )
            time_tag = container.find("time") or container.find(
                class_=re.compile(r"time|date|ago", re.I)
            )
            source_tag = container.find(class_=re.compile(r"source|publisher", re.I))

            if not link_tag:
                continue

            url = link_tag.get("href", "")
            if url.startswith("/"):
                url = "https://www.google.com" + url

            title = title_tag.get_text(strip=True) if title_tag else link_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            time_str = time_tag.get_text(strip=True) if time_tag else ""
            published_at = self._parse_relative_date(time_str) if time_str else None
            source = source_tag.get_text(strip=True) if source_tag else None

            items.append(
                RawNewsItem(
                    title=title,
                    url=url,
                    source=source,
                    published_at=published_at,
                    language=language,
                    provider=self.name,
                )
            )

        return items

    # ── date parsing ──────────────────────────────────────────────────────────

    def _parse_relative_date(self, time_str: str) -> Optional[datetime]:
        """
        Parse les dates relatives Google Finance en datetime UTC.

        "1 hour ago"    → now - 1h
        "3 hours ago"   → now - 3h
        "1 day ago"     → now - 1d
        "2 days ago"    → now - 2d
        "May 16"        → 2026-05-16
        "Jan 15, 2025"  → 2025-01-15
        """
        if not time_str:
            return None

        now = datetime.now(tz=timezone.utc)
        s = time_str.strip().lower()

        # "X hour(s) ago"
        m = re.match(r"(\d+)\s+hours?\s+ago", s)
        if m:
            return now - timedelta(hours=int(m.group(1)))

        # "X minute(s) ago"
        m = re.match(r"(\d+)\s+minutes?\s+ago", s)
        if m:
            return now - timedelta(minutes=int(m.group(1)))

        # "X day(s) ago"
        m = re.match(r"(\d+)\s+days?\s+ago", s)
        if m:
            return now - timedelta(days=int(m.group(1)))

        # "X week(s) ago"
        m = re.match(r"(\d+)\s+weeks?\s+ago", s)
        if m:
            return now - timedelta(weeks=int(m.group(1)))

        # "Jan 15, 2025" or "Jan 15 2025"
        month_map = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        m = re.match(r"([a-z]+)\s+(\d{1,2}),?\s*(\d{4})", s)
        if m:
            month = month_map.get(m.group(1)[:3])
            if month:
                try:
                    return datetime(int(m.group(3)), month, int(m.group(2)), tzinfo=timezone.utc)
                except ValueError:
                    pass

        # "May 16" (année courante)
        m = re.match(r"([a-z]+)\s+(\d{1,2})$", s)
        if m:
            month = month_map.get(m.group(1)[:3])
            if month:
                try:
                    return datetime(now.year, month, int(m.group(2)), tzinfo=timezone.utc)
                except ValueError:
                    pass

        return None
