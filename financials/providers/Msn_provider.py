import json
import logging
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class MsnProvider(BaseProvider):
    """
    Msn Provider (Bing Finance).
    """

    SEARCH_API = "https://services.bingapis.com/contentservices-finance.csautosuggest/api/v1/Query"
    BASE_URL = "https://www.msn.com/fr-ca/finances/details-de-l-action/{slug}/fi-{id}?id={id}"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            search_data = await self._search_id(client, ticker)
            if not search_data:
                return None

            full_url = self.BASE_URL.format(slug=search_data["slug"], id=search_data["id"])

            try:
                resp = await client.get(full_url)
                if resp.status_code == 200:
                    metrics = self._parse_page(HTMLParser(resp.text), ticker)
                    metrics.provider_url = full_url
                    return metrics
            except Exception as e:
                logger.error(f"Msn fetch error: {e}")
        return None

    async def _search_id(self, client: httpx.AsyncClient, query: str) -> Optional[dict]:
        # Query params: query=..., count=5
        try:
            resp = await client.get(
                self.SEARCH_API, params={"query": query, "count": 5, "market": "fr-ca"}
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("data") and data["data"].get("stocks"):
                    item = data["data"]["stocks"][0]

                    if isinstance(item, str):
                        try:
                            item = json.loads(item)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    if isinstance(item, dict):
                        sec_id = item.get("SecId") or item.get("SecID")
                        symbol = item.get("OS001Index", "").lower()
                        country = item.get("RT0EC", "").lower()
                        slug = (
                            f"{symbol}-{country}-stock"
                            if symbol and country
                            else f"{query.lower()}-stock"
                        )
                        if sec_id:
                            return {"id": sec_id, "slug": slug}
        except Exception as e:
            logger.warning(f"Msn search error: {e}")
        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)
        h1 = parser.css_first("h1")
        if h1:
            metrics.name = h1.text(strip=True)
        return metrics
