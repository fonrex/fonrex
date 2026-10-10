import asyncio
import logging
import re
from typing import Optional

from selectolax.parser import HTMLParser

from financials.exchange import GURUFOCUS_TO_YAHOO
from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)


class GurufocusProvider(BaseProvider):
    """
    Gurufocus Provider.
    Fetches specific scores (Piotroski, Beneish, etc.) from term definitions pages.
    """

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

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """
        Financial metrics, after resolving the ticker with the search API.
        """
        async with self._session(headers=self.headers) as client:
            # 1. Resolve the GuruFocus ticker (e.g. TFI.PA -> XPAR:TFI)
            gf_ticker = await self._resolve_gf_ticker(client, ticker)
            if not gf_ticker:
                logger.warning(f"Could not resolve GuruFocus ticker for {ticker}")
                return None

            logger.info(f"Resolved GuruFocus ticker: {ticker} -> {gf_ticker}")
            metrics = FinancialMetrics(ticker=gf_ticker)
            metrics.provider_url = f"{self.BASE_URL}/stock/{gf_ticker}/"

            # 2. Read the scores in parallel
            tasks = [
                self._fetch_score(client, gf_ticker, "fscore", "Piotroski-F-Score"),
                self._fetch_score(client, gf_ticker, "mscore", "Beneish-M-Score"),
                self._fetch_score(client, gf_ticker, "ROIC", "ROIC-Percentage"),
                self._fetch_score(client, gf_ticker, "gf_score", "GF-Score"),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Unpack results
            f_score, m_score, roic, gf_score = results

            # Assign if valid
            if isinstance(f_score, float):
                metrics.piotroski_score = int(f_score)
            if isinstance(m_score, float):
                metrics.beneish_m_score = m_score
            if isinstance(roic, float):
                metrics.roic = roic
            # gf_score extraction logic could be added to models if needed

            return metrics

    async def _resolve_gf_ticker(self, client: ProviderSession, ticker: str) -> Optional[str]:
        """
        Resolve the GuruFocus ticker with the search API and the exchange mapping.
        E.g. TFI.PA -> XPAR:TFI
        """
        # Split the symbol and the exchange suffix, if any
        parts = ticker.split(".")
        symbol = parts[0]
        target_suffix = f".{parts[1].upper()}" if len(parts) > 1 else ""

        # The ticker already holds a GuruFocus exchange (e.g. NAS:AAPL)
        if ":" in ticker:
            return ticker

        resolved = await self._search_symbol(client, symbol, target_suffix)
        if resolved:
            return resolved

        # Fallback: when the search fails, rebuild the ticker from the exchange mapping
        if target_suffix is not None:
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
            logger.error(f"Error during GuruFocus search for {query}: {e}")
        return None

    async def _fetch_score(
        self, client: ProviderSession, ticker: str, term_code: str, term_name: str
    ) -> Optional[float]:
        """
        Read a number from a GuruFocus term definition page.
        """
        url = f"{self.BASE_URL}/term/{term_code}/{ticker}/{term_name}/{ticker}"
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                html = HTMLParser(resp.text)
                # Heuristic: the first decimal number of the main content.
                # GuruFocus usually shows the value prominently.
                content = html.text()
                # Look for a pattern such as "8" or "0.45" once the noise is removed.
                # The legacy code took the first <font> of a given block.
                match = re.search(r"([-+]?\d*\.\d+|\d+)", content)
                if match:
                    return float(match.group(1))
        except Exception as e:
            logger.error(f"Error fetching {term_name} for {ticker}: {e}")
        return None
