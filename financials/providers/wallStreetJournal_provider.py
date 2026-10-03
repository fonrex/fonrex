import asyncio
import logging
import os
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


class WallStreetJournalProvider(BaseProvider):
    """
    WSJ Provider using httpx and selectolax.
    """

    SEARCH_API = "https://api.wsj.net/api/autocomplete/search"
    TOKEN = os.getenv("WSJ_TOKEN")
    BASE_URL = "https://www.wsj.com/market-data/quotes/{country}/{exchange}/{ticker}"

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

                # Fetch main page
                html = await self._fetch_page(client, search_result["provider_url"])
                if html:
                    page_metrics = self._parse_page(html, search_result.get("url_ticker") or ticker)
                    for field, value in page_metrics.model_dump(exclude_none=True).items():
                        setattr(metrics, field, value)

                # Fetch insider page
                people_url = search_result["provider_url"] + "/company-people"
                html_people = await self._fetch_page(client, people_url)
                if html_people:
                    insider_data = self._parse_insider_transactions(html_people)
                    metrics.insider_transactions = insider_data

                return metrics
            except Exception as e:
                logger.error(f"WSJ error {ticker}: {e}")
                return None

    async def _search_result(self, client: httpx.AsyncClient, ticker: str) -> Optional[dict]:
        params = {
            "q": ticker,
            "it": "fund,exchangetradedfund,stock,Index,Currency,Benchmark,Future,Bond,CryptoCurrency",
            "c": "5",
            "t": "symbol,private-company,person,suggested-search-term,topic,omniture-keyword",
            "xe": "XBAH,XCNQ,XTNX,XCYS,XCAI,XSTU,XBER,XHAN,XTAE,XAMM,XKAZ,XKUW,XCAS,XMUS,XKAR,DSMD,XMIC,RTSX,XSAU,XBRA,XCOL,XADS,XDFM,XCAR,XMSTAR,XOSE",
        }
        headers = {
            "Host": "api.wsj.net",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:140.0) Gecko/20100101 Firefox/140.0",
            "Accept": "application/json",
            "Accept-Language": "fr,fr-FR;q=0.8,en-US;q=0.5,en;q=0.3",
            "Referer": "https://www.wsj.com/",
            "dylan2010.entitlementtoken": self.TOKEN or "",
            "Origin": "https://www.wsj.com",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        }
        try:
            response = await client.get(self.SEARCH_API, params=params, headers=headers)
            if response.status_code == 200:
                data = response.json()
                symbol = self._select_symbol(data, ticker)
                if symbol:
                    return self._search_result_from_symbol(symbol, ticker)
            else:
                logger.warning(f"WSJ search API status {response.status_code} for {ticker}")
        except Exception as e:
            logger.warning(f"WSJ search API error: {e}")
        return None

    async def _search_symbol(self, client: httpx.AsyncClient, ticker: str):
        search_result = await self._search_result(client, ticker)
        if search_result:
            return (
                search_result.get("url_ticker") or search_result.get("ticker") or ticker,
                search_result.get("country", "US"),
                search_result.get("exchange", "XNYS"),
            )
        return ticker, "US", "XNYS"

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> Optional[HTMLParser]:
        for attempt in range(self.max_retries):
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Referer": "https://www.wsj.com/",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"macOS"',
                }
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return HTMLParser(resp.text)
                logger.warning(f"WSJ page fetch status {resp.status_code} for {url}")
                # Wait before retry
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"WSJ fetch error: {e}")
        return None

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

            # The lower, the better
            return (
                query_isin and isin != query_upper,
                not query_isin and url_ticker != query_upper and ticker != query_upper,
                # We prefer Europe for FR ISINs or if we search for a ticker that has an FR version
                (query_isin and query_upper.startswith("FR") and country != "FR")
                or (
                    not query_isin
                    and country == "US"
                    and any(s.get("country") == "FR" for s in symbols)
                ),
                # We prefer stocks to funds
                symbol_type != "stock",
                -score,
            )

        return sorted(symbols, key=rank)[0]

    @classmethod
    def _search_result_from_symbol(cls, symbol: dict, query: str) -> dict:
        ticker = symbol.get("ticker") or query
        url_ticker = ticker.split(":")[-1]
        country = symbol.get("country") or "US"
        exchange = symbol.get("exchangeIsoCode") or "XNYS"

        charting_symbol = symbol.get("chartingSymbol", "")
        if charting_symbol and (
            charting_symbol.startswith("STOCK/") or charting_symbol.startswith("FUND/")
        ):
            parts = charting_symbol.split("/")
            if len(parts) >= 4:
                country = parts[1] or country
                exchange = parts[2] or exchange
                url_ticker = parts[3] or url_ticker

        provider_url = cls.BASE_URL.format(
            country=country.upper(),
            exchange=exchange.upper(),
            ticker=url_ticker.upper(),
        )

        return {
            "ticker": ticker,
            "url_ticker": url_ticker,
            "country": country,
            "exchange": exchange,
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

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)
        h1 = parser.css_first("h1")
        if h1:
            metrics.name = h1.text(strip=True)

        metrics.pe_ratio = self._find_value_by_label(parser, ["P/E Ratio", "Price/Earnings"])
        metrics.dividend_yield = self._find_value_by_label(parser, ["Yield", "Dividend Yield"])
        metrics.eps = self._find_value_by_label(parser, ["EPS", "Earnings Per Share"])
        return metrics

    def _parse_insider_transactions(self, parser: HTMLParser) -> dict:
        """Parse Transaction Summary and Most Recent Insider Transactions."""
        data = {"Summary": {}, "Transactions": []}

        # 1. Transaction Summary
        summary_table = parser.css_first("table.cr_mod_insider")
        if summary_table:
            for row in summary_table.css("tbody tr"):
                cols = row.css("td")
                if len(cols) >= 3:
                    timeframe = cols[0].text(strip=True)
                    # Transactions (ex: "6 Purchases, 0 Sales")
                    tx_text = " ".join(
                        [node.text(strip=True) for node in cols[1].css(".data_data")]
                    )
                    # Shares (ex: "289,500 Purchased, 0 Sold")
                    shares_nodes = cols[2].css(".data_data")
                    shares_text = " / ".join([node.text(strip=True) for node in shares_nodes])

                    data["Summary"][timeframe] = {"transactions": tx_text, "shares": shares_text}

        # 2. Most Recent Insider Transactions
        recent_table = parser.css_first("table.cr_mod_transactions")
        if recent_table:
            for row in recent_table.css("tbody tr"):
                cols = row.css("td")
                if len(cols) >= 5:
                    data["Transactions"].append(
                        {
                            "date": cols[0].text(strip=True),
                            "ownerName": cols[1].text(strip=True),
                            "shares": cols[2].text(strip=True),
                            "description": cols[3].text(strip=True),
                            "value": cols[4].text(strip=True),
                        }
                    )

        return data

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
