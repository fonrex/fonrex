import logging
import random
import re
from typing import List, Optional

import httpx
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.numbers import parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

# User-Agent rotation list
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]


class BoursoramaProvider(BaseProvider):
    """
    Asynchronous Boursorama provider using httpx and selectolax.
    """

    BASE_URL = "https://www.boursorama.com"
    SEARCH_URL = "https://www.boursorama.com/recherche/ajax?query={symbol}&searchId="

    def __init__(self, max_retries: int = 3, timeout: int = 10):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """
        Financial data of a ticker.
        Flow:
        1. Search for the internal Boursorama ticker
        2. Fetch the page
        3. Parse it
        """
        async with self._session() as client:
            try:
                # 1. Find the URL of the instrument page
                url = await self._search_symbol(client, ticker)
                if not url:
                    logger.warning(f"Ticker {ticker} not found on Boursorama")
                    return None

                # 2. Fetch the page
                html = await self._fetch_page(client, url)
                if not html:
                    return None

                # 3. Parse the data
                metrics = self._parse_page(html, ticker)
                if metrics:
                    metrics.provider_url = url
                return metrics

            except Exception as e:
                logger.error(f"Error for {ticker}: {e}")
                return None

    async def _search_symbol(self, client: ProviderSession, symbol: str) -> Optional[str]:
        """Search for the URL of the instrument page."""
        url = self.SEARCH_URL.format(symbol=symbol)

        try:
            response = await self._fetch_with_retry(client, url)
            if not response:
                return None

            # The answer is often partial HTML or a redirection
            # The first link of the result list is taken
            parser = HTMLParser(response.text)

            # Exact selector, from the network frames of the search
            # Result links (share, ETF, etc.) have the class 'search__list-link'
            link = parser.css_first("a.search__list-link")

            if link:
                href = link.attributes.get("href")
                if href:
                    return f"{self.BASE_URL}{href}" if href.startswith("/") else href

            return None
        except Exception as e:
            logger.error(f"Symbol search error {symbol}: {e}")
            return None

    async def _fetch_page(self, client: ProviderSession, url: str) -> Optional[HTMLParser]:
        """The HTML of a page."""
        response = await self._fetch_with_retry(client, url)
        if response:
            return HTMLParser(response.text)
        return None

    async def _fetch_with_retry(
        self, client: ProviderSession, url: str
    ) -> Optional[httpx.Response]:
        """Fetch a page; retries, pauses and limits come from the shared session."""
        try:
            # A new User-Agent is drawn at each attempt.
            response = await client.get(
                url, headers=lambda: {"User-Agent": random.choice(USER_AGENTS)}
            )
        except Exception as e:
            logger.error(f"Network error for {url}: {e}")
            return None

        if response.status_code == 200:
            return response
        logger.error(f"HTTP error {response.status_code} for {url}")
        return None

    def _parse_page(self, parser: HTMLParser, ticker: str) -> FinancialMetrics:
        """Read the data of the page with selectolax."""
        metrics = FinancialMetrics(ticker=ticker)

        # ISIN
        isin_node = parser.css_first(".c-faceplate__isin")
        if isin_node:
            isin_text = isin_node.text(strip=True).replace("ISIN :", "").strip()
            # The heading also carries the ticker: "NL0000235190 AIR".
            isin_match = re.search(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", isin_text)
            metrics.isin = isin_match.group(0) if isin_match else isin_text

        # Eligibility (PEA, SRD, etc.)
        # Selector based on the common layout: often in the header or 'c-list-info'
        metrics.eligibility = self._extract_eligibility(parser)

        # ESG score (new gauge selector, or list)
        esg_score_node = parser.css_first(".c-gauge__label")
        if not esg_score_node:
            # Try the info list
            for item in parser.css(".c-list-info__item"):
                if "Risque ESG" in item.text():
                    esg_score_node = item
                    break

        if esg_score_node:
            esg_text = esg_score_node.text(strip=True)
            if "Fermer" in esg_text:
                esg_text = esg_text.split("Fermer")[-1].strip()
            metrics.esg_score = esg_text

        # Controversy level and other key ESG data
        for node in parser.css(".o-flex-stretch"):
            label_node = node.css_first("span")
            val_node = node.css_first("strong")
            if label_node and val_node:
                label = label_node.text().lower()
                val = val_node.text(strip=True)
                if "controverse" in label:
                    metrics.esg_controversy = val
                elif "co₂" in label:
                    metrics.esg_co2 = val
                elif "positif" in label:
                    metrics.esg_positive_impact = val
                elif "négatif" in label:
                    metrics.esg_negative_impact = val

        # Valuation data (consensus table or key figures)
        # Look for the key figures table
        # Often .c-table
        self._extract_key_figures(parser, metrics)

        # Risk (ETF)
        gauge = parser.css_first(".c-gauge")
        if gauge:
            current = gauge.attributes.get("data-gauge-current-step")
            max_step = gauge.attributes.get("data-gauge-steps")
            if current and max_step:
                metrics.risk_level = f"{current}/{max_step}"

        return metrics

    def _extract_eligibility(self, parser: HTMLParser) -> List[str]:
        # Instrument page: the "Éligibilité" entry of the info list carries one badge per scheme
        # (SRD, PEA, ...).
        for item in parser.css(".c-list-info__item"):
            heading = item.css_first(".c-list-info__heading")
            if heading and "ligibilit" in heading.text():
                badges = [badge.text(strip=True) for badge in item.css(".c-link-bevel")]
                badges = list(dict.fromkeys(badge for badge in badges if badge))
                if badges:
                    return badges

        items = []
        # Fallback: look for eligibility badges (PEA, SRD, etc.) in the lists.
        # Skip navigation menus, and elements holding scripts or too
        # long to be labels.
        for node in parser.css("li"):
            if "c-navigation" in (node.attributes.get("class") or ""):
                continue
            if node.css_first("script") or node.css_first("style"):
                continue
            text = node.text(strip=True)
            if text and any(token in text for token in ["PEA", "SRD", "CTO"]):
                if len(text) < 50:
                    items.append(text)

        return list(dict.fromkeys(items))

    def _extract_text_by_label(
        self, parser: HTMLParser, label_part: str, parent_css: str = ""
    ) -> Optional[str]:
        selector = f"{parent_css} *" if parent_css else "*"
        for node in parser.css(selector):
            if node.css_first("script") or node.css_first("style"):
                continue
            text = node.text(strip=True)
            if label_part in text and len(text) < 100:
                return text
        return None

    def _extract_key_figures(self, parser: HTMLParser, metrics: FinancialMetrics):
        """
        Try to read the P/E (PER), yield (Rendement) and EPS (BNA) from the tables.
        Tables are often laid out with <thead> (years) and <tbody> (values).
        """
        # Strategy: look for the rows holding "PER", "Rendement", "BPA" (or BNA)
        for row in parser.css("tr"):
            cells = row.css("td")
            header = row.css_first("th")
            row_title = header.text(strip=True) if header else ""

            if not row_title and cells:
                # Sometimes the title is in the first td
                row_title = cells[0].text(strip=True)
                values = cells[1:]
            else:
                values = cells

            if not values:
                continue

            # Take the first value (often the current year, or the N+1 estimate)
            # For PER and Rendement, Boursorama often shows N, N+1, N+2
            # The most relevant year is usually column 0 or 1
            val_text = values[0].text(strip=True)
            val = self._clean_number(val_text)

            if "PER" in row_title or "P/E" in row_title:
                metrics.pe_ratio = val
            elif "Rendement" in row_title:
                metrics.dividend_yield = val
            elif (
                "BPA" in row_title
                or "BNA" in row_title
                or "Bénéfice net par action" in row_title
            ):
                metrics.eps = val
            elif "Résultat net" in row_title:
                metrics.net_income = val
            elif "Chiffre d'affaires" in row_title:
                metrics.revenue = val
            elif "Dividende" in row_title and not metrics.dividend_yield:
                # Gross dividend but no yield
                pass

    def _clean_number(self, text: str) -> Optional[float]:
        """A figure of the page (French format, abbreviated amounts, sign)."""
        return parse_number(text, decimal=",")
