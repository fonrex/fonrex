"""GurufocusProvider — the scores and ratios of the summary page of a share on GuruFocus.

One page is read: ``/stock/{symbol}/summary``. GuruFocus renders it on its server
(Nuxt), so the figures are in the HTML. Each figure of its tables sits in a row
whose label links to the definition of the term (``/term/fscore/TSLA``): the row is
found by that code, never by the first number of the page, and the value is the
cell next to the label.

Read: the GF Score (``/100``), the Piotroski F-Score (``/9``), the Beneish M-Score,
the Altman Z-Score, the ROIC, the PE ratio, the net and operating margins, the debt to equity, the EPS
and the revenue of the last twelve months. Percentages are returned as displayed
(``3.11`` for 3.11 %), as declared in ``monitoring/units.py``.
"""

import logging
import re
from typing import Optional

from selectolax.parser import HTMLParser, Node

from financials.exchange import GURUFOCUS_TO_YAHOO
from financials.models import FinancialMetrics
from financials.numbers import parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

# Code of the term in the link of a row → field of FinancialMetrics.
TERM_FIELDS = {
    "fscore": "piotroski_score",
    "mscore": "beneish_m_score",
    "zscore": "altman_z_score",
    "roic": "roic",
    "pettm": "pe_ratio",
    "net-margin": "profit_margin",
    "operating-margin": "operating_margin",
    "debt-to-equity": "debt_to_equity",
    "eps-diluated": "eps",  # sic: the code GuruFocus uses
    "revenue": "revenue",
}

# The scale an amount is displayed in, written in its label: "Revenue (TTM) (Mil $)".
_SCALE = re.compile(r"\((Thou|Mil|Bil)\b", re.IGNORECASE)
_SCALES = {"thou": 1e3, "mil": 1e6, "bil": 1e9}

# A US listing has no exchange in the address of its page (``/stock/TSLA/``).
_US_EXCHANGES = frozenset(code for code, suffix in GURUFOCUS_TO_YAHOO.items() if suffix == "")


class GurufocusProvider(BaseProvider):
    """Scores and ratios of a share, from its GuruFocus summary page."""

    BASE_URL = "https://www.gurufocus.com"

    def __init__(self, max_retries: int = 3, timeout: int = 20):
        self.max_retries = max_retries
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,fr-FR;q=0.8,fr;q=0.7",
            "Origin": "https://www.gurufocus.com",
            "Referer": "https://www.gurufocus.com/",
        }
        self.page_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """Financial metrics, after resolving the ticker with the search API."""
        async with self._session(headers=self.headers) as client:
            # 1. Resolve the GuruFocus ticker (e.g. TFI.PA -> XPAR:TFI)
            gf_ticker = await self._resolve_gf_ticker(client, ticker)
            if not gf_ticker:
                logger.warning("Could not resolve GuruFocus ticker for %s", ticker)
                return None
            logger.info("Resolved GuruFocus ticker: %s -> %s", ticker, gf_ticker)

            # 2. Read the summary page
            url = self.summary_url(gf_ticker)
            page = None
            try:
                resp = await client.get(url, headers=self.page_headers)
                if resp.status_code == 200:
                    page = HTMLParser(resp.text)
                else:
                    logger.warning("GuruFocus page of %s: HTTP %s", gf_ticker, resp.status_code)
            except Exception as e:
                logger.error("Error fetching the GuruFocus page of %s: %s", gf_ticker, e)

            metrics = (
                self._parse_page(page, gf_ticker)
                if page is not None
                else FinancialMetrics(ticker=gf_ticker)
            )
            metrics.ticker = gf_ticker
            metrics.provider_url = url
            return metrics

    @classmethod
    def summary_url(cls, gf_ticker: str) -> str:
        """The summary page: ``/stock/TSLA/summary``, ``/stock/XPAR:AIR/summary``."""
        exchange, _, symbol = gf_ticker.rpartition(":")
        path = symbol if exchange.upper() in _US_EXCHANGES else gf_ticker
        return f"{cls.BASE_URL}/stock/{path}/summary"

    # ── Page ──────────────────────────────────────────────────────────────────

    def _parse_page(self, page: HTMLParser, ticker: str) -> FinancialMetrics:
        """The figures of a summary page; a figure not displayed stays ``None``."""
        metrics = FinancialMetrics(ticker=ticker)

        title = page.css_first("h1")
        if title is not None:
            name = " ".join(title.text(deep=True).split())
            metrics.name = name or None

        gf_score = page.css_first('a[href*="/term/gf-score/"] .t-primary')
        if gf_score is not None:
            metrics.gf_score = parse_number(gf_score.text(deep=True), decimal=".")

        for code, field in TERM_FIELDS.items():
            row = self._row(page, code)
            if row is None:
                continue
            label, value = row
            number = parse_number(value, decimal=".")
            if number is None:
                continue
            if field == "piotroski_score":
                if number != int(number) or not 0 <= number <= 9:
                    continue
                number = int(number)
            elif field == "revenue":
                scale = _SCALE.search(label)
                if scale:
                    number *= _SCALES[scale.group(1).lower()]
            setattr(metrics, field, number)

        return metrics

    @staticmethod
    def _row(page: HTMLParser, code: str) -> Optional[tuple[str, str]]:
        """The label and the value of the first row of a table whose label links to
        the term ``code``. The value drops what follows it (``4/9`` gives ``4``)."""
        for link in page.css(f'tr a[href*="/term/{code}/"]'):
            cell = _ancestor(link, "td")
            if cell is None:
                continue
            value_cell = cell.next
            while value_cell is not None and value_cell.tag != "td":
                value_cell = value_cell.next
            if value_cell is None:
                continue
            for extra in value_cell.css(".t-label, svg"):
                extra.decompose()
            label = " ".join(cell.text(deep=True).split())
            value = " ".join(value_cell.text(deep=True).split())
            return label, value
        return None

    # ── Ticker ────────────────────────────────────────────────────────────────

    async def _resolve_gf_ticker(self, client: ProviderSession, ticker: str) -> Optional[str]:
        """
        Resolve the GuruFocus ticker with the search API and the exchange mapping.
        E.g. TFI.PA -> XPAR:TFI
        """
        # The ticker already holds a GuruFocus exchange (e.g. NAS:AAPL)
        if ":" in ticker:
            return ticker

        # Split the symbol and the exchange suffix, if any
        parts = ticker.split(".")
        symbol = parts[0]
        target_suffix = f".{parts[1].upper()}" if len(parts) > 1 else ""

        resolved = await self._search_symbol(client, symbol, target_suffix)
        if resolved:
            return resolved

        # Fallback: when the search fails, rebuild the ticker from the exchange mapping
        for gf_exch, suffix in GURUFOCUS_TO_YAHOO.items():
            if suffix.upper() == target_suffix.upper():
                # No suffix (US market): prefer NAS or NYSE
                if target_suffix == "" and gf_exch not in ["NAS", "NYSE"]:
                    continue
                return f"{gf_exch}:{symbol}"

        return None

    async def _search_symbol(
        self, client: ProviderSession, query: str, target_suffix: Optional[str]
    ) -> Optional[str]:
        """
        Query the GuruFocus search API and pick the best result.
        """
        search_url = f"{self.BASE_URL}/reader/_api/_search?v=1.8.50&text={query}&type="
        try:
            resp = await client.get(search_url)
            if resp.status_code == 200:
                results = resp.json()

                # First look for an exact match through the exchange suffix
                for item in results:
                    if item.get("type") == "stock":
                        data = item.get("data", {})
                        gf_symbol = data.get("symbol", "").upper()
                        gf_exchange = data.get("exchange", "").upper()

                        # A target suffix (e.g. .PA)
                        if target_suffix:
                            # Turn the GF exchange into a Yahoo suffix to compare
                            mapped_suffix = GURUFOCUS_TO_YAHOO.get(gf_exchange)
                            if mapped_suffix and mapped_suffix.upper() == target_suffix.upper():
                                return f"{gf_exchange}:{gf_symbol}"
                        else:
                            # No suffix: take the first exact symbol match (often US)
                            if gf_symbol == query.upper():
                                return f"{gf_exchange}:{gf_symbol}" if gf_exchange else gf_symbol

                # Fallback: first stock result whose symbol matches
                for item in results:
                    if item.get("type") == "stock":
                        data = item.get("data", {})
                        if data.get("symbol", "").upper() == query.upper():
                            exch = data.get("exchange")
                            symb = data.get("symbol")
                            return f"{exch}:{symb}" if exch else symb

        except Exception as e:
            logger.error("Error during GuruFocus search for %s: %s", query, e)
        return None


def _ancestor(node: Node, tag: str) -> Optional[Node]:
    while node is not None and node.tag != tag:
        node = node.parent
    return node
