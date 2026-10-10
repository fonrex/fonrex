import asyncio
import logging
import os
import random
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from concurrency import run_sync
from database.price_series import resolve_price_series
from database.query import QueryService
from database.service import DatabaseService
from historical.adjustment import fetch_on_one_basis, record_scheme
from historical.normalization import normalize_bars
from historical.price_writer import write_price_bars
from historical.providers import HistoricalMarketDataFetcher
from historical.yahoo_symbols import YahooSymbolResolver
from models import Asset, AssetListing, IngestLog
from schemas.historical import IngestResult

logger = logging.getLogger(__name__)

# First segment of the cache keys holding answers computed from stored prices:
# /ticker/{symbol}/history, /eod/{ticker}, /technical/{ticker}, /dcf/{ticker}.
PRICE_CACHE_PREFIXES = ("history", "eod", "technical", "dcf")


class HistoricalIngestionService:
    """
    Historical EOD price data ingestion service.
    Handles multi-source (yfinance / TradingView) and multi-resolution (1D, 1W, 1M).
    """

    def __init__(
        self,
        db_service: DatabaseService,
        query_service: QueryService,
        redis_client: Optional[Any] = None,
        market_data_fetcher: HistoricalMarketDataFetcher | None = None,
        symbol_resolver: YahooSymbolResolver | None = None,
    ):
        self.db_service = db_service
        self.query_service = query_service
        self.redis_client = redis_client
        self._market_data_fetcher = market_data_fetcher or HistoricalMarketDataFetcher()
        # A ticker of the catalogue is not a Yahoo symbol: the symbol of a listing
        # is found from its ISIN, checked against its currency, and remembered.
        self._symbols = symbol_resolver or YahooSymbolResolver(db_service)

        # Configuration from environment variables
        self.concurrency = int(os.environ.get("INGEST_CONCURRENCY", 5))
        self.yf_delay = float(os.environ.get("INGEST_YF_DELAY", 0.5))
        self.tv_delay = float(os.environ.get("INGEST_TV_DELAY", 2.0))
        self.batch_size = int(os.environ.get("INGEST_BATCH_SIZE", 1000))

    async def ingest(
        self,
        ticker: str,
        resolution: str = "1D",
        source: str = "auto",
        force_refresh: bool = False,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        currency: Optional[str] = None,
        exchange: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> IngestResult:
        """
        Ingest the history of one ticker.

        ``currency``, ``exchange`` and ``isin`` name one listing among those
        sharing the ticker (``GOVY`` in EUR or in CHF, ``NEM`` of Newmont or of
        Nemetschek); without them the primary listing is ingested.
        """
        start_time = time.time()
        resolution = resolution.upper()
        if resolution not in ["1D", "1W", "1M"]:
            return IngestResult(
                ticker=ticker,
                resolution=resolution,
                status="failed",
                error=f"Unsupported resolution: {resolution}",
            )

        # 1. Resolve the asset and the listing
        asset_id, listing_id, _listing_isin, listing_currency = await run_sync(
            self._resolve_asset_context, ticker, currency, exchange, isin
        )
        if not asset_id or not listing_id:
            choice = ", ".join(
                f"{name} {value}"
                for name, value in (("ISIN", isin), ("currency", currency), ("exchange", exchange))
                if value
            )
            wanted = f" ({choice})" if choice else ""
            return IngestResult(
                ticker=ticker,
                resolution=resolution,
                status="failed",
                error=(
                    f"No listing found for ticker {ticker}{wanted}: prices are stored per listing"
                ),
            )

        # 2. Find the date ranges to load (gap detection)
        fetch_start, fetch_end, is_up_to_date = await self._detect_gaps(
            ticker, resolution, force_refresh, from_date, to_date, currency, exchange, isin
        )

        async def stop(status: str, error_msg: Optional[str] = None) -> IngestResult:
            """End the ingestion without writing anything: up to date, or failed."""
            duration_ms = int((time.time() - start_time) * 1000)
            dates = {"from_date": from_date, "to_date": to_date} if not error_msg else {}
            await self._log_ingest(
                asset_id=asset_id,
                ticker=ticker,
                resolution=resolution,
                source=source,
                status=status,
                records_added=0,
                error_msg=error_msg,
                duration_ms=duration_ms,
                **dates,
            )
            return IngestResult(
                ticker=ticker,
                resolution=resolution,
                status=status,
                source_used=source if error_msg else None,
                records_added=0,
                duration_ms=duration_ms,
                error=error_msg,
                **dates,
            )

        if is_up_to_date:
            return await stop("up_to_date")

        # 3. Source symbol. The prices of a listing are fetched with the
        # symbol verified for it (ISIN + currency), never with the ticker as typed:
        # a ticker shared with another instrument would bring that instrument's prices.
        yahoo_symbol, symbol_note = None, None
        if source in ("auto", "yfinance"):
            resolved = await self._symbols.resolve(listing_id, refresh=force_refresh)
            yahoo_symbol, symbol_note = resolved.symbol, resolved.reason

        async def fetch(first: date, last: date) -> Optional[Dict[str, Any]]:
            logger.info(
                f"🔄 Ingestion {ticker} ({resolution}): {first} -> {last} via {source}"
                f" [{yahoo_symbol or 'no verified Yahoo symbol'}]"
            )
            return await self._fetch_with_fallback(
                ticker,
                resolution,
                source,
                first,
                last,
                yahoo_symbol=yahoo_symbol,
                currency=listing_currency,
                symbol_note=symbol_note,
            )

        # 4-5. Fetch and normalize, on one adjustment basis for the whole series
        # (``historical/adjustment.py``): new bars are added only when the last
        # stored bars still match the source; otherwise the series is fetched again.
        outcome = await fetch_on_one_basis(
            fetch,
            lambda bars: self._normalize_bars(bars, asset_id, listing_id, resolution),
            self.db_service.get_session,
            listing_id,
            resolution,
            fetch_start,
            fetch_end,
            force_refresh,
        )
        if outcome.error:
            return await stop("failed", outcome.error)
        fetch_result, normalized_bars = outcome.result, outcome.bars
        source_used = fetch_result["source_used"]

        # 6. Upsert into the database. A series fetched in one piece replaces the
        # fetched range instead of merging into it, so that no bar of an earlier
        # fetch (other adjustment basis, wrongly dated row) survives next to the
        # new ones.
        records_added = await self._upsert_prices_eod(
            normalized_bars, replace=force_refresh or outcome.rebase_reason is not None
        )
        if outcome.whole_series and normalized_bars:
            await record_scheme(self.db_service.get_session, listing_id, resolution)

        # 7. Invalidate the Redis cache
        await self._invalidate_cache(ticker)

        # 8. Log the operation
        duration_ms = int((time.time() - start_time) * 1000)
        status = "success" if records_added > 0 else "up_to_date"

        actual_from = min(b["time"].date() for b in normalized_bars) if normalized_bars else None
        actual_to = max(b["time"].date() for b in normalized_bars) if normalized_bars else None

        await self._log_ingest(
            asset_id=asset_id,
            ticker=ticker,
            resolution=resolution,
            source=source_used,
            status=status,
            records_added=records_added,
            from_date=actual_from,
            to_date=actual_to,
            duration_ms=duration_ms,
        )

        return IngestResult(
            ticker=ticker,
            resolution=resolution,
            status=status,
            source_used=source_used,
            provider_symbol=fetch_result.get("symbol"),
            note=outcome.note,
            records_added=records_added,
            from_date=actual_from,
            to_date=actual_to,
            duration_ms=duration_ms,
        )

    async def ingest_bulk(
        self,
        tickers: List[str],
        resolution: str = "1D",
        source: str = "auto",
        force_refresh: bool = False,
        concurrency: Optional[int] = None,
    ) -> List[IngestResult]:
        """
        Ingest a list of tickers in parallel, with limited concurrency.
        """
        sem_limit = concurrency or self.concurrency
        semaphore = asyncio.Semaphore(sem_limit)
        results = []

        async def worker(ticker: str):
            async with semaphore:
                # Random delay to spread the load and avoid bans
                await asyncio.sleep(random.uniform(0.1, 0.5))
                try:
                    res = await self.ingest(
                        ticker=ticker,
                        resolution=resolution,
                        source=source,
                        force_refresh=force_refresh,
                    )
                    results.append(res)
                except Exception as e:
                    logger.error(f"❌ Critical error in the bulk ingestion of {ticker}: {e}")
                    results.append(
                        IngestResult(
                            ticker=ticker, resolution=resolution, status="failed", error=str(e)
                        )
                    )

        tasks = [worker(t) for t in tickers]
        await asyncio.gather(*tasks)
        return results

    async def _detect_gaps(
        self,
        ticker: str,
        resolution: str,
        force_refresh: bool,
        from_date: Optional[date],
        to_date: Optional[date],
        currency: Optional[str] = None,
        exchange: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> Tuple[date, date, bool]:
        """
        The missing date range.
        """
        today = date.today()
        end_date = to_date or today

        if force_refresh:
            start_date = from_date or (today - timedelta(days=365 * 10))  # 10 years by default
            return start_date, end_date, False

        # Look at the data already stored
        db_range = await self.query_service.get_history_range(
            ticker, resolution, currency=currency, exchange=exchange, isin=isin
        )

        if db_range["count"] == 0:
            start_date = from_date or (today - timedelta(days=365 * 10))
            return start_date, end_date, False

        max_date_db = db_range["max_date"]

        # Up to date when the last stored day is today or yesterday
        if max_date_db and max_date_db >= (today - timedelta(days=1)):
            if from_date and from_date < db_range["min_date"]:
                # The user asks for older history
                return from_date, db_range["min_date"] - timedelta(days=1), False
            return max_date_db, end_date, True

        # Incremental ingestion
        start_date = max_date_db + timedelta(days=1)
        if start_date >= end_date:
            return max_date_db, end_date, True

        return start_date, end_date, False

    async def _fetch_with_fallback(
        self,
        ticker: str,
        resolution: str,
        source: str,
        start: date,
        end: date,
        yahoo_symbol: Optional[str] = None,
        currency: Optional[str] = None,
        symbol_note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch from the chosen source, with fallback in auto mode (``historical/providers.py``)."""
        return await self._market_data_fetcher.fetch(
            ticker,
            resolution,
            source,
            start,
            end,
            yahoo_symbol=yahoo_symbol,
            currency=currency,
            symbol_note=symbol_note,
            pause=self.yf_delay,
        )

    async def _fetch_yfinance(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> Optional[Dict[str, Any]]:
        return await self._market_data_fetcher.fetch_yfinance(ticker, resolution, start, end)

    def _resolve_asset(
        self,
        ticker: str,
        session: Session,
        currency: Optional[str] = None,
        exchange: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> Tuple[Optional[int], Optional[int]]:
        """
        Resolve a ticker into (asset_id, listing_id), with the rule shared by every
        reader of ``prices_eod`` (``database/price_series.py``).
        """
        series = resolve_price_series(
            session, ticker, currency=currency, exchange=exchange, isin=isin
        )
        if series is None:
            return None, None
        return series.asset_id, series.listing_id

    def _normalize_bars(
        self, bars: List[Dict[str, Any]], asset_id: int, listing_id: int, resolution: str
    ) -> List[Dict[str, Any]]:
        return normalize_bars(bars, asset_id, listing_id, resolution)

    async def _upsert_prices_eod(self, bars: List[Dict[str, Any]], replace: bool = False) -> int:
        """Write the bars (``historical/price_writer.py``); ``replace`` swaps the range."""
        if not bars:
            return 0
        return await run_sync(self._upsert_prices_eod_sync, bars, replace)

    def _upsert_prices_eod_sync(self, bars: List[Dict[str, Any]], replace: bool = False) -> int:
        session = self.db_service.get_session()
        try:
            written = write_price_bars(session, bars, self.batch_size, replace)
            session.commit()
            return written
        except Exception as e:
            session.rollback()
            logger.error(f"❌ EOD price upsert error: {e}")
            raise e
        finally:
            session.close()

    async def _invalidate_cache(self, ticker: str):
        """Drop every cached answer computed from the prices of this ticker.

        The price routes, the indicators and the valuation each keep their own
        entries. Leaving one out served the prices of before the ingestion for as
        long as its lifetime (24 hours for ``/eod``), even after a forced refresh
        that replaced a wrong series.
        """
        if not self.redis_client:
            return
        symbol = ticker.strip().upper()
        # A key segment holds no ":" nor space ("XETR:SPFF" is written "XETR_SPFF").
        symbols = {symbol, symbol.replace(":", "_").replace(" ", "_")}
        try:
            keys_to_delete = []
            for pattern in (f"{p}:{s}:*" for p in PRICE_CACHE_PREFIXES for s in sorted(symbols)):
                cursor = 0
                while True:
                    cursor, keys = await self.redis_client.scan(
                        cursor=cursor, match=pattern, count=100
                    )
                    keys_to_delete.extend(keys)
                    if cursor == 0:
                        break
            if keys_to_delete:
                await self.redis_client.delete(*keys_to_delete)
                logger.info(
                    f"🧹 Cache invalidated for {ticker} ({len(keys_to_delete)} keys deleted)"
                )
        except Exception as e:
            logger.warning(f"⚠️ Redis invalidation error for {ticker}: {e}")

    async def _log_ingest(
        self,
        asset_id: int,
        ticker: str,
        resolution: str,
        source: str,
        status: str,
        records_added: int = 0,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        error_msg: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ):
        """
        Write an ingestion log row.
        """
        await run_sync(
            self._log_ingest_sync,
            asset_id,
            ticker,
            resolution,
            source,
            status,
            records_added,
            from_date,
            to_date,
            error_msg,
            duration_ms,
        )

    def _resolve_asset_context(self, ticker, currency=None, exchange=None, isin=None):
        """Return (asset_id, listing_id, isin, currency of the listing)."""
        session = self.db_service.get_session()
        try:
            asset_id, listing_id = self._resolve_asset(ticker, session, currency, exchange, isin)
            asset = session.get(Asset, asset_id) if asset_id else None
            listing = session.get(AssetListing, listing_id) if listing_id else None
            return (
                asset_id,
                listing_id,
                asset.isin if asset else None,
                (listing.currency or None) if listing else None,
            )
        finally:
            session.close()

    def _log_ingest_sync(
        self,
        asset_id: int,
        ticker: str,
        resolution: str,
        source: str,
        status: str,
        records_added: int,
        from_date: Optional[date],
        to_date: Optional[date],
        error_msg: Optional[str],
        duration_ms: Optional[int],
    ):
        session = self.db_service.get_session()
        try:
            log_entry = IngestLog(
                asset_id=asset_id,
                ticker=ticker,
                resolution=resolution,
                source=source,
                status=status,
                records_added=records_added,
                from_date=from_date,
                to_date=to_date,
                error_msg=error_msg,
                duration_ms=duration_ms,
            )
            session.add(log_entry)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"❌ Ingestion log write error: {e}")
        finally:
            session.close()
