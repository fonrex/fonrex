import logging
import os
import random
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


class MarketwatchProvider(BaseProvider):
    """
    Marketwatch Provider using httpx and selectolax.
    """

    SEARCH_API = "https://api.wsj.net/api/autocomplete/search"
    TOKEN = os.getenv("MARKETWATCH_TOKEN")
    BASE_URL = "https://www.marketwatch.com/investing/stock/{ticker}"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                search_result = await self._search_result(client, ticker)
                if not search_result:
                    return None

                metrics = self._metrics_from_search_result(search_result, ticker)

                html = await self._fetch_page(client, search_result["provider_url"])
                if not html:
                    return metrics

                page_metrics = self._parse_page(html, search_result.get("url_ticker") or ticker)
                for field, value in page_metrics.model_dump(exclude_none=True).items():
                    setattr(metrics, field, value)
                return metrics
            except Exception as e:
                logger.error(f"Marketwatch error {ticker}: {e}")
                return None

    async def _search_result(self, client: httpx.AsyncClient, ticker: str) -> Optional[dict]:
        params = {
            "q": ticker,
            "t": "marketwatch-topic,marketwatch-search-link,symbol",
            "xe": "xmstar",
            "featureClass": "P",
            "style": "full",
            "maxRows": "5",
            "name_startsWith": ticker,
            "entitlementToken": self.TOKEN,
        }
        try:
            response = await client.get(self.SEARCH_API, params=params)
            if response.status_code == 200:
                data = response.json()
                symbol = self._select_symbol(data, ticker)
                if symbol:
                    return self._search_result_from_symbol(symbol, ticker)
            else:
                logger.warning(f"Marketwatch search API status {response.status_code} for {ticker}")
        except Exception as e:
            logger.warning(f"Marketwatch search API error: {e}")
        return None

    async def _search_symbol(self, client: httpx.AsyncClient, ticker: str):
        search_result = await self._search_result(client, ticker)
        if search_result:
            return search_result.get("url_ticker") or search_result.get(
                "ticker"
            ) or ticker, search_result.get("country", "us")
        return ticker, "us"

    @staticmethod
    def _select_symbol(data: dict, query: str) -> Optional[dict]:
        symbols = data.get("symbols") or []
        if not symbols:
            return None

        query_upper = (query or "").strip().upper()
        query_isin = len(query_upper) == 12 and query_upper.isalnum() and query_upper[:2].isalpha()

        def rank(symbol):
            ticker = (symbol.get("ticker") or "").upper()
            url_ticker = ticker.split(":")[-1]
            isin = (symbol.get("isin") or "").upper()
            country = (symbol.get("country") or "").upper()
            symbol_type = (symbol.get("type") or "").lower()
            score = symbol.get("score") or 0
            return (
                query_isin and isin != query_upper,
                not query_isin and url_ticker != query_upper and ticker != query_upper,
                country != "FR" if query_isin and query_upper.startswith("FR") else country != "US",
                symbol_type != "stock",
                -score,
            )

        return sorted(symbols, key=rank)[0]

    @classmethod
    def _search_result_from_symbol(cls, symbol: dict, query: str) -> dict:
        ticker = symbol.get("ticker") or query
        url_ticker = ticker.split(":")[-1]
        country = symbol.get("country") or "us"
        provider_url = cls.BASE_URL.format(ticker=url_ticker.lower())
        if country and country.lower() != "us":
            provider_url += f"?countrycode={country.lower()}"

        return {
            "ticker": ticker,
            "url_ticker": url_ticker,
            "country": country,
            "exchange": symbol.get("exchangeIsoCode"),
            "exchange_name": symbol.get("exchange"),
            "isin": symbol.get("isin"),
            "name": symbol.get("company"),
            "instrument_type": symbol.get("type"),
            "provider_url": provider_url,
        }

    @staticmethod
    def _metrics_from_search_result(search_result: dict, ticker_input: str) -> FinancialMetrics:
        return FinancialMetrics(
            name=search_result.get("name"),
            ticker=search_result.get("url_ticker") or search_result.get("ticker") or ticker_input,
            isin=search_result.get("isin"),
            provider_url=search_result.get("provider_url"),
            exchange=search_result.get("exchange"),
            exchange_name=search_result.get("exchange_name"),
            instrument_type=search_result.get("instrument_type"),
        )

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> Optional[HTMLParser]:
        for attempt in range(self.max_retries):
            try:
                headers = {"User-Agent": random.choice(USER_AGENTS)}
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return HTMLParser(resp.text)
                logger.warning(f"Marketwatch page fetch status {resp.status_code} for {url}")
            except Exception:
                pass
        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)
        h1 = parser.css_first("h1")
        if h1:
            metrics.name = h1.text(strip=True)

        metrics.pe_ratio = self._find_value_by_label(parser, ["P/E Ratio", "P/E"])
        metrics.dividend_yield = self._find_value_by_label(parser, ["Yield", "Dividend Yield"])
        metrics.eps = self._find_value_by_label(parser, ["EPS"])
        return metrics

    def _find_value_by_label(self, parser: HTMLParser, labels: list) -> Optional[float]:
        import re

        for node in parser.css("div, span, td, li"):
            txt = node.text(strip=True)
            for label in labels:
                if label.lower() in txt.lower() and len(txt) < 50:
                    nums = re.findall(r"(\d+\.?\d*)", txt)
                    if nums and ":" in txt:
                        return float(nums[-1])
                    sibling = node.next
                    if sibling:
                        sib_txt = sibling.text(strip=True)
                        sib_nums = re.findall(r"(\d+\.?\d*)", sib_txt)
                        if sib_nums:
                            return float(sib_nums[0])
        return None
