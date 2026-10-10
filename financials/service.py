import asyncio
import json
import logging
from typing import List

import redis.asyncio as redis

from concurrency import run_sync
from financials.models import StandardFinancials, StockSummary
from financials.providers.base import BaseProvider
from financials.providers.BourseDirect_provider import BourseDirectProvider
from financials.providers.yfinance_provider import YFinanceProvider

logger = logging.getLogger(__name__)


class FinancialsAggregator:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

        # Providers called with the ticker
        self.ticker_providers: List[BaseProvider] = [
            YFinanceProvider(),
        ]

        # Providers called with the ISIN (found through the ticker)
        self.isin_providers: List[BaseProvider] = [
            BourseDirectProvider(),
        ]

        self.cache_ttl = 86400  # 24 hours

    async def get_financials(self, ticker: str) -> StandardFinancials:
        """
        The merged financial data of a ticker.
        Reads the cache first, otherwise asks the providers in parallel.
        """
        cache_key = f"financials:{ticker}"

        # 1. Cache
        if self.redis:
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.info(f"Cache hit for {ticker}")
                return StandardFinancials(**json.loads(cached_data))

        # 2. Providers called with the ticker (step 1)
        logger.info(f"Step 1: reading the ticker data of {ticker}")
        ticker_tasks = [provider.get_financials(ticker) for provider in self.ticker_providers]
        ticker_results_raw = await asyncio.gather(*ticker_tasks, return_exceptions=True)

        valid_results = []
        found_isin = None

        # Ticker results
        for res in ticker_results_raw:
            if isinstance(res, Exception):
                logger.error(f"Ticker provider error: {res}")
            elif res:
                valid_results.append(res)
                # Take the ISIN of the first result that has one
                if not found_isin and res.isin:
                    found_isin = res.isin

        # 3. Providers called with the ISIN (step 2) - when an ISIN was found
        if found_isin and self.isin_providers:
            logger.info(f"Step 2: ISIN found ({found_isin}), calling the ISIN providers")
            isin_tasks = [provider.get_financials(found_isin) for provider in self.isin_providers]
            isin_results_raw = await asyncio.gather(*isin_tasks, return_exceptions=True)

            for res in isin_results_raw:
                if isinstance(res, Exception):
                    logger.error(f"ISIN provider error: {res}")
                elif res:
                    valid_results.append(res)
        elif self.isin_providers:
            logger.warning(f"No ISIN found for {ticker}, ISIN providers skipped")

        if not valid_results:
            logger.warning(f"No data found for {ticker}")
            return StandardFinancials()

        # 4. Merge the data
        final_data = {}
        # Merge every field of StandardFinancials
        fields = StandardFinancials.model_fields.keys()

        for field in fields:
            value = None
            # Priority: the valid results are read in order (ticker, then ISIN).
            # valid_results holds [Res_Ticker1, Res_Ticker2, ..., Res_ISIN1, Res_ISIN2...]
            # The order of valid_results is the priority: the first result wins.
            for result in valid_results:
                val = getattr(result, field, None)
                if val is not None:
                    value = val
                    break
            final_data[field] = value

        aggregated_financials = StandardFinancials(**final_data)

        # 5. Cache it
        if self.redis:
            await self.redis.set(
                cache_key, aggregated_financials.model_dump_json(), ex=self.cache_ttl
            )

        return aggregated_financials

    async def get_market_overview(self) -> List[StockSummary]:
        """
        A short list for the market overview.
        For now, a static list of popular tickers.
        """
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "BRK-B", "JPM", "V"]

        # A batch call would be faster if the provider supported it
        # Here: simple parallel calls for the basic information

        async def fetch_summary(ticker):
            # YFinanceProvider.get_financials gives no price or name yet:
            # yfinance is called directly here for both.
            import yfinance as yf

            try:
                stock = await run_sync(yf.Ticker, ticker)
                info = await run_sync(lambda: stock.info)
                return StockSummary(
                    ticker=ticker,
                    name=info.get("shortName"),
                    sector=info.get("sector"),
                    price=info.get("currentPrice"),
                    change_percent=info.get(
                        "regularMarketChangePercent"
                    ),  # or another change field
                )
            except Exception as e:
                logger.error(f"Overview error for {ticker}: {e}")
                return None

        tasks = [fetch_summary(t) for t in tickers]
        results = await asyncio.gather(*tasks)

        return [r for r in results if r is not None]
