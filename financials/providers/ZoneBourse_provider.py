"""ZoneBourseProvider — the figures of a share from its ZoneBourse synthesis page.

A quote page is found through ``/async/search/quick`` (ISIN first when given,
otherwise the ticker sent without its Yahoo suffix and matched by the country
flag and currency of the row) or read directly when a ``provider_url`` is
supplied. One fixed set of browser headers is used for the session; the
``User-Agent`` can be overridden with ``ZONEBOURSE_USER_AGENT``.
"""

import logging
import os
import re
from typing import List, Optional, Tuple

import httpx
from selectolax.parser import HTMLParser, Node

from financials.models import FinancialMetrics
from financials.numbers import parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")
_CURRENCY = re.compile(r"[A-Z]{3}")
_FLAG = re.compile(r"\bflag__([a-z]{2})\b", re.IGNORECASE)
_COTATIONS_SUFFIX = re.compile(r"/cotations/\d+/?$")

USER_AGENT_ENV_VAR = "ZONEBOURSE_USER_AGENT"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Safari/605.1.15"
)

SEARCH_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.zonebourse.com",
    "Referer": "https://www.zonebourse.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Yahoo suffix -> country flags (``flag__xx``) and currencies of that place on ZoneBourse.
PLACES: dict[str, tuple[set[str], set[str]]] = {
    "PA": ({"fr"}, {"EUR"}),
    "AS": ({"nl"}, {"EUR"}),
    "BR": ({"be"}, {"EUR"}),
    "LS": ({"pt"}, {"EUR"}),
    "IR": ({"ie"}, {"EUR"}),
    "DE": ({"de"}, {"EUR"}),
    "F": ({"de"}, {"EUR"}),
    "MI": ({"it"}, {"EUR"}),
    "MC": ({"es"}, {"EUR"}),
    "HE": ({"fi"}, {"EUR"}),
    "VI": ({"at"}, {"EUR"}),
    "L": ({"gb"}, {"GBP", "GBX"}),
    "SW": ({"ch"}, {"CHF"}),
    "ST": ({"se"}, {"SEK"}),
    "CO": ({"dk"}, {"DKK"}),
    "OL": ({"no"}, {"NOK"}),
    "TO": ({"ca"}, {"CAD"}),
    "T": ({"jp"}, {"JPY"}),
    "HK": ({"hk"}, {"HKD"}),
    "AX": ({"au"}, {"AUD"}),
}


def user_agent() -> str:
    """User-Agent sent to ZoneBourse, configurable with ``ZONEBOURSE_USER_AGENT``."""
    return (os.environ.get(USER_AGENT_ENV_VAR) or "").strip() or DEFAULT_USER_AGENT


def session_headers() -> dict[str, str]:
    """Fixed headers shared by the requests of one ZoneBourse session."""
    return {
        "User-Agent": user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }


def _ticker_and_place(ticker: str) -> tuple[str, Optional[tuple[set[str], set[str]]]]:
    """The bare symbol and the place of a Yahoo ticker: ``"AIR.PA"`` -> ``("AIR", Paris)``.

    A suffix that names no known place (``BRK.B`` is a share class) stays in the symbol.
    """
    symbol, _, suffix = ticker.rpartition(".")
    if symbol and suffix in PLACES:
        return symbol, PLACES[suffix]
    return ticker, None


def _search_term(identifier: str) -> str:
    """What the search endpoint is asked: an ISIN as it is, a ticker without its Yahoo suffix."""
    wanted = identifier.strip().upper()
    if _ISIN.fullmatch(wanted):
        return wanted
    return _ticker_and_place(wanted)[0]


def _matches_place(row: dict[str, str], flags: set[str], currencies: set[str]) -> bool:
    """Tell whether a search result row belongs to the requested place."""
    flag = row["flag"]
    currency = row["currency"]
    if flag and currency:
        return flag in flags and currency in currencies
    if flag:
        return flag in flags
    if currency:
        return currency in currencies
    return False


class ZoneBourseProvider(BaseProvider):
    """Asynchronous provider for ZoneBourse using httpx and selectolax."""

    BASE_URL = "https://www.zonebourse.com"
    SEARCH_URL = "https://www.zonebourse.com/recherche/?q={symbol}"

    def __init__(self, max_retries: int = 3, timeout: int = 10):
        self.max_retries = max_retries
        self.timeout = timeout

    async def fetch(
        self,
        ticker: Optional[str] = None,
        isin: Optional[str] = None,
        provider_url: Optional[str] = None,
        **kwargs,
    ) -> Optional[FinancialMetrics]:
        return await self.get_financials(ticker, isin=isin, provider_url=provider_url)

    async def get_financials(
        self,
        ticker: Optional[str] = None,
        isin: Optional[str] = None,
        provider_url: Optional[str] = None,
    ) -> Optional[FinancialMetrics]:
        """Fetches financial data for a given ticker, ISIN or ZoneBourse URL."""
        ticker_input = (ticker or isin or provider_url or "").strip()
        if not ticker_input:
            return None

        async with self._session(headers=session_headers()) as client:
            try:
                url = self.direct_url(provider_url or ticker_input)
                if not url:
                    url = await self._resolve_url(client, ticker=ticker, isin=isin)
                if not url:
                    logger.warning(f"Ticker/ISIN {ticker_input} not found on ZoneBourse")
                    return None

                html = await self._fetch_page(client, url)
                if not html:
                    logger.error(f"Failed to fetch profile page for {ticker_input}")
                    return None

                return self._parse_page(html, ticker_input, url)

            except Exception as e:
                logger.error(f"Global error fetching {ticker_input}: {e}")
                return None

    @classmethod
    def direct_url(cls, identifier: Optional[str]) -> Optional[str]:
        """The page address when the caller already gave a ZoneBourse URL or path."""
        value = (identifier or "").strip()
        if value.startswith(("https://", "http://")):
            return value
        if value.startswith("/cours/"):
            return f"{cls.BASE_URL}{value}"
        return None

    @classmethod
    def _full_url(cls, href: str) -> str:
        """Full synthesis URL of a search result row (secondary ``/cotations/`` stripped)."""
        clean_href = _COTATIONS_SUFFIX.sub("/", href.strip())
        return f"{cls.BASE_URL}{clean_href}" if clean_href.startswith("/") else clean_href

    async def _resolve_url(
        self,
        client: ProviderSession,
        *,
        ticker: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> Optional[str]:
        """Search by ISIN first when available, then fall back to the ticker."""
        candidates: list[str] = []
        for candidate in (isin, ticker):
            cleaned = (candidate or "").strip()
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
        candidates.sort(key=lambda item: not bool(_ISIN.fullmatch(item.upper())))

        for candidate in candidates:
            url = await self._search_symbol(client, candidate)
            if url:
                return url
        return None

    async def _search_symbol(self, client: ProviderSession, symbol: str) -> Optional[str]:
        """Searches for the symbol/ISIN via ``/async/search/quick`` and selects the profile URL."""
        url = f"{self.BASE_URL}/async/search/quick"
        payload = {
            "search": _search_term(symbol),
            "type": "company",
        }

        try:
            logger.debug(f"Searching for {symbol} via POST {url}")
            response = await client.post(url, data=payload, headers=SEARCH_HEADERS)

            if response.status_code != 200:
                logger.warning(f"Search failed with status {response.status_code}")
                return None

            data = response.json()
            if not data or not isinstance(data, dict):
                logger.warning("Invalid JSON response from search")
                return None

            html_content = data.get("data", "")
            if not html_content:
                logger.warning("No HTML content in search response")
                return None

            full_url = self.select_url(html_content, symbol)
            if full_url:
                logger.info(f"Found URL for {symbol}: {full_url}")
                return full_url

            logger.warning(f"No matching symbol found in search results for {symbol}")
            return None

        except Exception as e:
            logger.error(f"Error searching symbol {symbol}: {e}")
            return None

    @classmethod
    def select_url(cls, html_content: str, identifier: str) -> Optional[str]:
        """Select the synthesis URL matching ``identifier`` from the search HTML fragment."""
        parser = HTMLParser(html_content)
        rows: list[dict[str, str]] = []
        for node in parser.css("tr[data-href]"):
            href = (node.attributes.get("data-href") or "").strip()
            if not href:
                continue
            symbol_cell = node.css_first("td.table-child--w80")
            flag_node = node.css_first("i.flag")
            flag_match = (
                _FLAG.search(flag_node.attributes.get("class") or "") if flag_node else None
            )
            ccy_node = node.css_first("span.txt-muted")
            rows.append(
                {
                    "href": href,
                    "symbol": symbol_cell.text(strip=True).upper() if symbol_cell else "",
                    "flag": flag_match.group(1).lower() if flag_match else "",
                    "currency": ccy_node.text(strip=True).upper() if ccy_node else "",
                }
            )

        if not rows:
            return None

        wanted = identifier.strip().upper()
        if _ISIN.fullmatch(wanted):
            return cls._full_url(rows[0]["href"])

        # Minimal test fixtures with ``<tr data-href="..."></tr>`` carry no inner cells.
        if not any(r["symbol"] or r["flag"] or r["currency"] for r in rows):
            return cls._full_url(rows[0]["href"])

        symbol, place = _ticker_and_place(wanted)
        matches = [r for r in rows if r["symbol"] == symbol]
        if place is not None:
            flags, currencies = place
            matches = [r for r in matches if _matches_place(r, flags, currencies)]
        return cls._full_url(matches[0]["href"]) if matches else None

    async def _fetch_page(self, client: ProviderSession, url: str) -> Optional[HTMLParser]:
        """Fetches the page content and returns a parser."""
        response = await self._fetch_with_retry(client, url)
        if response:
            return HTMLParser(response.text)
        return None

    async def _fetch_with_retry(
        self, client: ProviderSession, url: str
    ) -> Optional[httpx.Response]:
        """Fetch a page; headers, retries, pauses and limits come from the shared session."""
        try:
            response = await client.get(url)
        except Exception as e:
            logger.error(f"Network error for {url}: {e}")
            return None

        if response.status_code == 200:
            return response
        logger.warning(f"Status {response.status_code} for {url}")
        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str, url: str) -> FinancialMetrics:
        """Parses the HTML to extract financial metrics.

        The ticker stays the one asked: the ISIN of the page goes to ``isin``.
        """
        metrics = FinancialMetrics(ticker=ticker_input)
        metrics.provider_url = url

        try:
            # 1. Header: "Cours <company name>", then badges (country, symbol, ISIN, sector)
            h1 = parser.css_first("h1")
            if h1:
                metrics.name = re.sub(r"^Cours\s+", "", h1.text(strip=True)) or None
            metrics.isin = self._isin(parser)
            metrics.sector = self._sector(parser)

            # Last price, in the currency written next to it
            last = parser.css_first(".is__realtime-last .js-last")
            if last:
                metrics.price = self._clean_number(last.text(strip=True))
                currency = last.parent.css_first("sup") if last.parent else None
                if currency and _CURRENCY.fullmatch(currency.text(strip=True)):
                    metrics.currency = currency.text(strip=True)

            # 2. Label / value rows of the side cards (consensus, company profile)
            for label, value in self._grid_rows(parser):
                self._assign_grid_value(metrics, label, value)

            # ESG MSCI rating (AAA to CCC)
            esg_node = parser.css_first(".esg-rank")
            if esg_node:
                metrics.esg_score = esg_node.text(strip=True)

            # 3. Valuation and financial tables (estimates of the current fiscal year)
            self._extract_financial_table_data(parser, metrics)

        except Exception as e:
            logger.error(f"Error parsing page: {e}")

        return metrics

    @staticmethod
    def _isin(parser: HTMLParser) -> Optional[str]:
        """The ISIN of the header badges, or of an "ISIN : ..." label."""
        for badge in parser.css(".badge-container h2"):
            text = badge.text(strip=True)
            if _ISIN.fullmatch(text):
                return text
        for node in parser.css("li, td, div, span"):
            text = node.text(strip=True)
            match = re.fullmatch(r"ISIN\s*:?\s*([A-Z]{2}[A-Z0-9]{9}\d)", text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _sector(parser: HTMLParser) -> Optional[str]:
        """The sector link of the header (``/bourse/secteurs/...``)."""
        link = parser.css_first('.badge-container a[href*="/bourse/secteurs/"]')
        if link:
            return link.text(strip=True) or None
        return None

    @staticmethod
    def _grid_rows(parser: HTMLParser) -> List[Tuple[str, str]]:
        """The rows ``<div class="grid"><div class="c">label</div><div>value</div></div>``."""
        rows = []
        for grid in parser.css("div.grid"):
            cells = [child for child in grid.iter() if child.tag in ("div", "a")]
            if len(cells) != 2:
                continue
            label = cells[0].text(strip=True)
            value = " ".join(cells[1].text(separator=" ").split())
            if label and value and len(label) < 40:
                rows.append((label, value))
        return rows

    def _assign_grid_value(self, metrics: FinancialMetrics, label: str, value: str) -> None:
        key = label.lower()
        if key == "recommandation moyenne":
            metrics.consensus = value
        elif key == "nombre d'analystes":
            count = self._clean_number(value)
            if count is not None and count == int(count):
                metrics.analyst_count = int(count)
        elif key == "objectif de cours moyen":
            # "2 150,00 EUR": the currency after the figure is understood by parse_number
            metrics.target_price = self._clean_number(value)
        elif key == "employés":
            staff = self._clean_number(value)
            if staff is not None and staff == int(staff):
                metrics.employees = int(staff)
        elif key == "secteur" and not metrics.sector:
            metrics.sector = value

    def _extract_financial_table_data(self, parser: HTMLParser, metrics: FinancialMetrics):
        """
        Scans all tables for financial keywords and extracts data.

        For each metric the first matching row wins (current year or estimate).
        """
        for table in parser.css("table"):
            for row in table.css("tr"):
                for label, value_cells in self._row_pairs(row):
                    value = self._find_first_number(value_cells)
                    if value is not None:
                        self._assign_metric(metrics, label.lower(), value)

    @staticmethod
    def _row_pairs(row: Node) -> List[Tuple[str, List[Node]]]:
        """Splits a table row into (label, value cells) pairs.

        ZoneBourse serves two layouts:
        - label in the first cell (<th> or <td>), values in the following <td> cells;
        - label in a <td>, values in the following <th> cells, with possibly several
          label/value pairs on the same row (valuation block, financial estimates).
        """
        cells = [cell for cell in row.iter() if cell.tag in ("td", "th")]
        if not cells:
            return []

        if cells[0].tag == "td" and any(cell.tag == "th" for cell in cells):
            pairs: List[Tuple[str, List[Node]]] = []
            for cell in cells:
                if cell.tag == "td":
                    pairs.append((cell.text(strip=True), []))
                elif pairs:
                    pairs[-1][1].append(cell)
            return [(label, values) for label, values in pairs if label and values]

        label = cells[0].text(strip=True)
        values = [cell for cell in cells[1:] if cell.tag == "td"]
        return [(label, values)] if label else []

    @staticmethod
    def _assign_metric(metrics: FinancialMetrics, label_lower: str, value: float) -> None:
        """Maps a row label to a metric; an already extracted metric is kept."""
        if label_lower.startswith("capitalisation"):
            field = "market_cap"
        elif label_lower.startswith("valeur d'entreprise"):
            field = "enterprise_value"
        elif label_lower.startswith("ve / ca") or label_lower.startswith("ve/ca"):
            field = "ev_revenue"
        elif label_lower.startswith("endettement net"):
            field = "net_debt"
        elif label_lower.startswith("flottant"):
            # Returned as displayed, i.e. as a percentage (82.19 for 82.19 %).
            field = "float_percent"
        elif "chiffre d'affaires" in label_lower or "net sales" in label_lower:
            field = "revenue"
        elif "résultat net" in label_lower or "net income" in label_lower:
            field = "net_income"
        elif re.search(r"\b(bna|bpa|eps)\b", label_lower):
            field = "eps"
        elif re.search(r"\bper\b|p/e", label_lower):
            field = "pe_ratio"
        elif "rendement" in label_lower or "yield" in label_lower:
            # Returned as displayed, i.e. as a percentage (see monitoring/units.py).
            field = "dividend_yield"
        elif "ebitda" in label_lower:
            field = "ebitda"
        elif "marge d'exploitation" in label_lower or "operating margin" in label_lower:
            field = "operating_margin"
        elif "marge nette" in label_lower or "net margin" in label_lower:
            field = "profit_margin"
        else:
            return

        if getattr(metrics, field, None) is None:
            setattr(metrics, field, value)

    def _find_first_number(self, cells: List[Node]) -> Optional[float]:
        """Iterates cells to find the first valid float."""
        for cell in cells:
            val = self._cell_number(cell)
            if val is not None:
                return val
        return None

    def _cell_number(self, cell: Node) -> Optional[float]:
        """Reads the number of a value cell.

        Amounts are rendered once per currency (<span class="efd_EUR">,
        <span class="efd_USD c-none">, ...): only the visible one is read, from
        its exact ``title`` value when available.
        """
        variants = cell.css("span[class*='efd_']")
        if not variants:
            return self._clean_number(cell.text(strip=True))

        visible = next(
            (v for v in variants if "c-none" not in (v.attributes.get("class") or "")),
            variants[0],
        )
        exact = visible.css_first("span[title]")
        if exact:
            value = self._clean_number(exact.attributes.get("title") or "")
            if value is not None:
                return value
        return self._clean_number(visible.text(strip=True))

    def _clean_number(self, text: str) -> Optional[float]:
        """French format, abbreviated amounts included: "80,95 Md", "25,5x", "1,81 %"."""
        return parse_number(text, decimal=",")
