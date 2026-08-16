import logging
import random
import re
import unicodedata
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


class FortuneoProvider(BaseProvider):
    """
    Fortuneo Provider.
    """

    SEARCH_API = "https://bourse.fortuneo.fr/api/search"
    BASE_URL = "https://bourse.fortuneo.fr"
    MARKET_KEYS = ("fortuneo", "arkea")

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            search_result = self._direct_search_result(ticker)
            if not search_result:
                search_result = await self._search_result(client, ticker)
            if not search_result:
                return None

            metrics = self._metrics_from_search_result(search_result, ticker)

            try:
                headers = {"User-Agent": random.choice(USER_AGENTS)}
                resp = await client.get(search_result["provider_url"], headers=headers)
                if resp.status_code == 200:
                    page_metrics = self._parse_page(HTMLParser(resp.text), metrics.ticker or ticker)
                    for field, value in page_metrics.model_dump(exclude_none=True).items():
                        setattr(metrics, field, value)
            except Exception as e:
                logger.error(f"Fortuneo fetch error: {e}")
        return metrics

    async def _search_url(self, client: httpx.AsyncClient, query: str) -> Optional[str]:
        search_result = await self._search_result(client, query)
        if search_result:
            return search_result.get("url")
        return None

    async def _search_result(self, client: httpx.AsyncClient, query: str) -> Optional[dict]:
        try:
            resp = await client.get(self.SEARCH_API, params={"term": query})
            if resp.status_code == 200:
                data = resp.json()
                item = self._select_item(data, query)
                if item:
                    search_result = self._search_result_from_item(item, query)
                    if search_result.get("provider_url"):
                        return search_result
        except Exception as e:
            logger.warning(f"Fortuneo search error: {e}")
        return None

    @classmethod
    def _direct_search_result(cls, identifier: str) -> Optional[dict]:
        provider_url = cls._full_url(identifier)
        if not provider_url:
            return None

        return {
            "provider_url": provider_url,
            "url": provider_url.replace(cls.BASE_URL, "", 1),
        }

    @classmethod
    def _full_url(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None

        value = value.strip()
        if value.startswith(cls.BASE_URL):
            return value
        if value.startswith("/"):
            return f"{cls.BASE_URL}{value}"
        return None

    @classmethod
    def _select_item(cls, data: dict, query: str) -> Optional[dict]:
        market = data.get("market") or {}
        items = []

        for key in cls.MARKET_KEYS:
            items.extend((market.get(key) or {}).get("items") or [])

        if not items:
            for payload in market.values():
                if isinstance(payload, dict):
                    items.extend(payload.get("items") or [])

        if not items:
            return None

        query_upper = (query or "").strip().upper()
        query_isin = len(query_upper) == 12 and query_upper.isalnum() and query_upper[:2].isalpha()

        def rank(item):
            isin = (item.get("codeIsin") or "").upper()
            mnemo = (item.get("mnemo") or "").upper()
            instrument_type = (item.get("type") or "").lower()
            return (
                query_isin and isin != query_upper,
                not query_isin and mnemo != query_upper,
                item.get("cours") is None,
                item.get("devise") is None,
                instrument_type != "action",
            )

        return sorted(items, key=rank)[0]

    @classmethod
    def _search_result_from_item(cls, item: dict, query: str) -> dict:
        url = item.get("url") or cls._build_url_path(item)
        provider_url = cls._full_url(url)

        return {
            "ticker": item.get("mnemo") or query,
            "isin": item.get("codeIsin"),
            "name": item.get("libelle"),
            "exchange": item.get("place"),
            "exchange_code": item.get("codePlace"),
            "instrument_type": item.get("type"),
            "currency": item.get("devise"),
            "price": item.get("cours"),
            "change_percent": item.get("variation"),
            "provider_url": provider_url,
            "url": url,
        }

    @classmethod
    def _build_url_path(cls, item: dict) -> Optional[str]:
        isin = item.get("codeIsin")
        mnemo = item.get("mnemo")
        code_place = item.get("codePlace")
        label = item.get("libelle")

        if not all([isin, mnemo, code_place, label]):
            return None

        path_prefix = cls._path_prefix(item.get("type"))
        slug = cls._slugify(label)
        return f"/{path_prefix}/cours-{slug}-{mnemo}-{isin}-{code_place}"

    @staticmethod
    def _path_prefix(instrument_type: Optional[str]) -> str:
        if (instrument_type or "").strip().lower() == "action":
            return "actions"
        return "actions"

    @staticmethod
    def _slugify(value: str) -> str:
        ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()
        return slug

    @staticmethod
    def _metrics_from_search_result(search_result: dict, ticker_input: str) -> FinancialMetrics:
        return FinancialMetrics(
            name=search_result.get("name"),
            ticker=search_result.get("ticker") or ticker_input,
            isin=search_result.get("isin"),
            provider_url=search_result.get("provider_url"),
            exchange=search_result.get("exchange"),
            exchange_code=search_result.get("exchange_code"),
            instrument_type=search_result.get("instrument_type"),
            currency=search_result.get("currency"),
            price=search_result.get("price"),
            change_percent=search_result.get("change_percent"),
        )

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)
        # Parse logic if needed. Fortuneo page structure:
        # <h1>Header</h1>
        h1 = parser.css_first("h1")
        if h1:
            metrics.name = h1.text(strip=True)

        # Try to find generic data
        # ...
        return metrics
