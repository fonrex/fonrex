import logging
import os
import random
from typing import Optional

from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.numbers import find_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


class BarronsProvider(BaseProvider):
    """
    Barrons Provider using httpx and selectolax.
    """

    SEARCH_API = "https://api.barrons.com/api/autocomplete/search"
    TOKEN = os.getenv("BARRONS_TOKEN")
    BASE_URL = "https://www.barrons.com/market-data/stocks/{ticker}"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        async with self._session() as client:
            try:
                search_result = await self._search_result(client, ticker)
                if not search_result:
                    return None

                metrics = self._metrics_from_search_result(search_result, ticker)

                html = await self._fetch_page(client, search_result["provider_url"])
                if not html:
                    return metrics

                page_metrics = self._parse_page(html, ticker)
                for field, value in page_metrics.model_dump(exclude_none=True).items():
                    setattr(metrics, field, value)
                return metrics
            except Exception as e:
                logger.error(f"Barrons error {ticker}: {e}")
                return None

    async def _search_result(self, client: ProviderSession, ticker: str) -> Optional[dict]:
        params = {"q": ticker, "it": "stock,etf,fund", "c": 10, "entitlementToken": self.TOKEN}
        try:
            response = await client.get(self.SEARCH_API, params=params)
            if response.status_code == 200:
                data = response.json()
                symbol = self._select_symbol(data, ticker)
                if symbol:
                    return self._search_result_from_symbol(symbol, ticker)
            else:
                logger.warning(f"Barrons search API status {response.status_code} for {ticker}")
        except Exception as e:
            logger.warning(f"Barrons search API error: {e}")
        return None

    async def _search_symbol(self, client: ProviderSession, ticker: str):
        search_result = await self._search_result(client, ticker)
        if search_result:
            return search_result.get("url_ticker") or search_result.get(
                "ticker"
            ) or ticker, search_result.get("country", "US")
        return ticker, "US"

    @staticmethod
    def _select_symbol(data: dict, query: str) -> Optional[dict]:
        symbols = data.get("symbols") or []
        if not symbols:
            return None

        query_upper = (query or "").strip().upper()

        def rank(symbol):
            ticker = (symbol.get("ticker") or "").upper()
            url_ticker = ticker.split(":")[-1]
            country = (symbol.get("country") or "").upper()
            symbol_type = (symbol.get("type") or "").lower()
            score = symbol.get("score") or 0
            return (
                url_ticker != query_upper and ticker != query_upper,
                country != "US",
                symbol_type != "stock",
                -score,
            )

        return sorted(symbols, key=rank)[0]

    @classmethod
    def _search_result_from_symbol(cls, symbol: dict, query: str) -> dict:
        ticker = symbol.get("ticker") or query
        url_ticker = ticker.split(":")[-1]
        country = symbol.get("country") or "US"
        provider_url = cls.BASE_URL.format(ticker=url_ticker.lower())
        if country and country.lower() != "us":
            provider_url += f"?countrycode={country.lower()}"

        return {
            "ticker": ticker,
            "url_ticker": url_ticker,
            "country": country,
            "isin": symbol.get("isin"),
            "name": symbol.get("company"),
            "provider_url": provider_url,
        }

    @staticmethod
    def _metrics_from_search_result(search_result: dict, ticker_input: str) -> FinancialMetrics:
        return FinancialMetrics(
            name=search_result.get("name"),
            ticker=search_result.get("url_ticker") or search_result.get("ticker") or ticker_input,
            isin=search_result.get("isin"),
            provider_url=search_result.get("provider_url"),
        )

    async def _fetch_page(self, client: ProviderSession, url: str) -> Optional[HTMLParser]:
        try:
            # A new User-Agent is drawn at each attempt of the shared retry policy.
            resp = await client.get(url, headers=lambda: {"User-Agent": random.choice(USER_AGENTS)})
            if resp.status_code == 200:
                return HTMLParser(resp.text)
            logger.warning(f"Barrons page fetch status {resp.status_code} for {url}")
        except Exception as e:
            logger.warning(f"Barrons page fetch error: {e}")
        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)

        # Name
        h1 = parser.css_first("h1")
        if h1:
            metrics.name = h1.text(strip=True)

        # Heuristic generic parsing for P/E, Yield etc.
        # Barrons likely uses standard tables or label/value pairs.

        # P/E
        metrics.pe_ratio = self._find_value_by_label(parser, ["P/E Ratio", "Price/Earnings"])

        # Yield
        metrics.dividend_yield = self._find_value_by_label(parser, ["Yield", "Dividend Yield"])

        # EPS
        metrics.eps = self._find_value_by_label(parser, ["EPS", "Earnings Per Share"])

        return metrics

    def _find_value_by_label(self, parser: HTMLParser, labels: list) -> Optional[float]:
        # 1. New DOM structure from browser subagent: div > p (label), p (value)
        for div in parser.css("div"):
            ps = div.css("p")
            if len(ps) >= 2:
                label_text = ps[0].text(strip=True).lower()
                if any(label.lower() in label_text for label in labels):
                    value = find_number(ps[1].text(strip=True))
                    if value is not None:
                        return value

        # 2. Generic finder fallback
        for node in parser.css("div, span, td, li"):
            txt = node.text(strip=True)
            for label in labels:
                if label.lower() in txt.lower() and len(txt) < 50:
                    # The text is "P/E Ratio: 12.5", or the value is in the next node.
                    if ":" in txt:
                        value = find_number(txt, last=True)
                        if value is not None:
                            return value
                    sibling = node.next
                    if sibling:
                        value = find_number(sibling.text(strip=True))
                        if value is not None:
                            return value
        return None
