import logging
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class InvestingProvider(BaseProvider):
    """
    Investing.com Provider.
    """

    SEARCH_API = "https://api.investing.com/api/search/v2/search"
    ROOT_URL = "https://www.investing.com"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, identifier: str) -> Optional[FinancialMetrics]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            search_result = self._direct_search_result(identifier)
            if not search_result:
                search_result = await self._search_result(client, identifier)
            if not search_result:
                return None

            fallback_metrics = self._metrics_from_search_result(search_result, identifier)
            full_url = search_result["provider_url"]

            # Fetch
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://www.investing.com/",
                }
                resp = await client.get(full_url, headers=headers)
                if resp.status_code == 200:
                    metrics = self._parse_page(
                        HTMLParser(resp.text), search_result.get("ticker") or identifier
                    )
                    return self._merge_search_result(metrics, fallback_metrics)
                logger.warning(
                    f"Investing page fetch returned HTTP {resp.status_code} for {full_url}"
                )
                return fallback_metrics
            except Exception as e:
                logger.error(f"Investing fetch error: {e}")
                return fallback_metrics

        return None

    @classmethod
    def _direct_search_result(cls, identifier: str) -> Optional[dict]:
        if not identifier:
            return None
        identifier = identifier.strip()
        if identifier.startswith("http") or identifier.startswith("/equities/"):
            return {
                "url": identifier,
                "provider_url": cls._full_url(identifier),
                "ticker": None,
                "name": None,
                "isin": None,
            }
        return None

    @classmethod
    def _full_url(cls, url_path: str) -> str:
        if url_path.startswith("http"):
            return url_path
        if url_path.startswith("/"):
            return cls.ROOT_URL + url_path
        return cls.ROOT_URL + "/equities/" + url_path

    @staticmethod
    def _looks_like_isin(value: str) -> bool:
        value = (value or "").strip().upper()
        return len(value) == 12 and value.isalnum() and value[:2].isalpha()

    @classmethod
    def _select_quote(cls, data: dict, query: str) -> Optional[dict]:
        quotes = data.get("quotes") or []
        if not quotes:
            return None
        return quotes[0]

    @classmethod
    def _search_result_from_quote(cls, quote: dict, query: str) -> Optional[dict]:
        if not quote or not quote.get("url"):
            return None

        result = {
            "url": quote.get("url"),
            "provider_url": cls._full_url(quote.get("url")),
            "ticker": quote.get("symbol"),
            "name": quote.get("description"),
            "exchange": quote.get("exchange"),
            "instrument_type": quote.get("type"),
            "isin": query.strip().upper() if cls._looks_like_isin(query) else None,
        }
        return result

    @classmethod
    def _metrics_from_search_result(cls, search_result: dict, identifier: str) -> FinancialMetrics:
        return FinancialMetrics(
            ticker=search_result.get("ticker") or identifier,
            isin=search_result.get("isin"),
            provider_url=search_result.get("provider_url"),
            name=search_result.get("name"),
            exchange=search_result.get("exchange"),
            instrument_type=search_result.get("instrument_type"),
        )

    @staticmethod
    def _merge_search_result(
        metrics: FinancialMetrics, fallback: FinancialMetrics
    ) -> FinancialMetrics:
        fallback_data = fallback.model_dump(exclude_none=True)
        for field, value in fallback_data.items():
            if getattr(metrics, field, None) is None:
                setattr(metrics, field, value)
        return metrics

    async def _search_result(self, client: httpx.AsyncClient, query: str) -> Optional[dict]:
        try:
            resp = await client.get(self.SEARCH_API, params={"q": query})
            if resp.status_code == 200:
                data = resp.json()
                quote = self._select_quote(data, query)
                return self._search_result_from_quote(quote, query)
        except Exception as e:
            logger.warning(f"Investing search error: {e}")
        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)
        h1 = parser.css_first("h1")
        if h1:
            metrics.name = h1.text(strip=True)

        # Investing often puts data in dl pairs
        # e.g. <dt>P/E Ratio</dt><dd>15.2</dd>

        for dt in parser.css("dt"):
            label = dt.text(strip=True)
            dd = dt.next
            if dd and dd.tag == "dd":
                val_text = dd.text(strip=True)
                import re

                val_nums = re.findall(r"(\d+\.?\d*)", val_text)
                if val_nums:
                    val = float(val_nums[0])
                    if "P/E Ratio" in label:
                        metrics.pe_ratio = val
                    elif "EPS" in label:
                        metrics.eps = val
                    elif "Dividend Yield" in label:
                        metrics.dividend_yield = val
        return metrics
