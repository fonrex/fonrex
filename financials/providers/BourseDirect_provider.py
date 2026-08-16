import logging
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class BourseDirectProvider(BaseProvider):
    """
    BourseDirect Provider.
    """

    SEARCH_API = "https://www.boursedirect.fr/api/search/{query}"
    BASE_URL = "https://www.boursedirect.fr"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            url_path = await self._search_url(client, ticker)
            if not url_path:
                return None

            # url_path is typically like /fr/marche/...
            full_url = self.BASE_URL + url_path

            try:
                # User-Agent is often required to avoid 403
                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                }
                resp = await client.get(full_url, headers=headers)
                if resp.status_code == 200:
                    metrics = self._parse_page(HTMLParser(resp.text), ticker)
                    if metrics:
                        metrics.provider_url = full_url
                    return metrics
                else:
                    logger.warning(
                        f"BourseDirect fetch error: Status {resp.status_code} for {full_url}"
                    )
            except Exception as e:
                logger.error(f"BourseDirect fetch error: {e}")
        return None

    async def _search_url(self, client: httpx.AsyncClient, query: str) -> Optional[str]:
        # URL needs replacing {query}
        url = self.SEARCH_API.format(query=query)
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                # data['instruments']['data'][0]['url']
                if data.get("instruments") and data["instruments"].get("data"):
                    items = data["instruments"]["data"]
                    if items:
                        return items[0].get("url")
        except Exception as e:
            logger.warning(f"BourseDirect search error: {e}")
        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)

        # Name
        h1 = parser.css_first("h1")
        if h1:
            metrics.name = h1.text(strip=True)

        # Price
        price_node = parser.css_first(".quotation-last")
        if price_node:
            metrics.price = self._clean_number(price_node.text(strip=True))

        # Variation
        var_node = parser.css_first(".quotation-variation")
        if var_node:
            metrics.change_percent = self._clean_number(var_node.text(strip=True))

        # Ticker / Mnemo
        ticker_node = parser.css_first(".sticker.sticker-default-reverse")
        if ticker_node:
            metrics.ticker = ticker_node.text(strip=True)

        # ISIN from meta
        meta_desc = parser.css_first('meta[property="og:description"]')
        if meta_desc:
            import re

            content = meta_desc.attributes.get("content", "")
            match = re.search(r"([A-Z]{2}[A-Z0-9]{9}[0-9])", content)
            if match:
                metrics.isin = match.group(1)

        return metrics

    def _clean_number(self, text: str) -> Optional[float]:
        try:
            # Nettoyage format français "1 234,56" -> "1234.56"
            clean = (
                text.replace(" ", "").replace(",", ".").replace("%", "").replace("+", "").strip()
            )
            return float(clean)
        except (ValueError, TypeError):
            return None
