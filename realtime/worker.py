"""
RealtimePriceWorker — asyncio worker that keeps the TradingView WebSocket
connections open and distributes realtime prices through Redis Pub/Sub.

Design:
- One asyncio.Task per subscribed ticker
- Global semaphore: at most TV_MAX_CONNECTIONS simultaneous TradingView connections
- Automatic reconnection after a disconnection (exponential backoff)
- Distribution through the Redis Pub/Sub channel "price:{ticker}"
- Redis cache key "quote:{ticker}" with a configurable TTL
- Every complete 1-minute candle stored in prices_intraday

Lifecycle:
    worker = RealtimePriceWorker(redis_client, db_session_factory)
    await worker.start()           # Starts the worker (FastAPI lifespan)
    await worker.subscribe("AIR.PA")
    await worker.unsubscribe("AIR.PA")
    await worker.stop()            # Clean shutdown (FastAPI lifespan)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Optional, Set

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Asset, PriceIntraday, RealtimeSubscription
from schemas.realtime import RealtimeTick

logger = logging.getLogger(__name__)

# ── Configuration from environment variables ────────────────────────────────
TV_MAX_CONNECTIONS = int(os.getenv("TV_MAX_CONNECTIONS", "10"))
RECONNECT_DELAY_INIT = int(os.getenv("TV_RECONNECT_DELAY", "5"))
RECONNECT_DELAY_MAX = 60
QUOTE_TTL = int(os.getenv("REALTIME_QUOTE_TTL", "60"))

# ── Redis keys ───────────────────────────────────────────────────────────────
REDIS_QUOTE_KEY = "quote:{ticker}"  # Last price (TTL QUOTE_TTL)
REDIS_PUBSUB_CHAN = "price:{ticker}"  # Pub/Sub channel
REDIS_SUBS_SET = "realtime:subscriptions"  # Set of active tickers

# ── Mapping Yahoo ticker suffix → TradingView exchange ───────────────────────
TV_EXCHANGE_MAP: dict[str, str] = {
    ".PA": "EURONEXT",
    ".AS": "EURONEXT",
    ".BR": "EURONEXT",
    ".DE": "XETRA",
    ".F": "FWB",
    ".L": "LSE",
    ".MI": "MIL",
    ".MC": "BME",
    ".ST": "OMX",
    ".OL": "OSL",
    ".HE": "OMXHEX",
    ".SW": "SIX",
    ".TO": "TSX",
    ".AX": "ASX",
}

# Dedicated thread pool running the (synchronous) TradingView Streamer
_executor = ThreadPoolExecutor(max_workers=TV_MAX_CONNECTIONS + 2, thread_name_prefix="tv_stream")


class RealtimePriceWorker:
    """
    Central realtime streaming worker.
    Created once when the API starts (FastAPI lifespan).
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        async_session_factory: Callable[[], AsyncSession],
    ):
        self.redis = redis_client
        self._session_factory = async_session_factory

        # asyncio state
        self._tasks: Dict[str, asyncio.Task] = {}  # ticker → Task
        self._running: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

        # Rate-limiting TradingView
        self._semaphore = asyncio.Semaphore(TV_MAX_CONNECTIONS)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the worker.
        Restores the active subscriptions stored in realtime_subscriptions.
        Call it in the FastAPI lifespan context manager.
        """
        self._running = True
        logger.info("[RealtimeWorker] Starting...")

        active_subs = await self._load_active_subscriptions()
        for sub in active_subs:
            await self.subscribe(sub.ticker, persist=False)

        logger.info(f"[RealtimeWorker] {len(active_subs)} subscriptions restored")

    async def stop(self) -> None:
        """
        Stop every streamer cleanly.
        Call it in the FastAPI lifespan context manager (shutdown).
        """
        self._running = False
        for ticker, task in list(self._tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("[RealtimeWorker] Stopped cleanly")

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def subscribe(self, ticker: str, persist: bool = True) -> bool:
        """
        Subscribe a ticker to realtime streaming.

        1. Resolve (tv_exchange, tv_symbol) from the Yahoo ticker
        2. Check that the asset exists in the database
        3. Create or activate the RealtimeSubscription (if persist=True)
        4. Start the streaming asyncio.Task of the ticker
        5. Add the ticker to the Redis set realtime:subscriptions

        Returns True when subscribed, False otherwise.
        """
        ticker = ticker.upper()

        async with self._lock:
            if ticker in self._tasks and not self._tasks[ticker].done():
                logger.debug(f"[{ticker}] Already subscribed")
                return True

        tv_exchange, tv_symbol = self._resolve_tv_symbol(ticker)

        # Check that the asset exists in the database
        asset_id: Optional[int] = None
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(Asset.id).where(Asset.ticker == ticker).limit(1)
                )
                row = result.first()
                if row:
                    asset_id = row[0]
        except SQLAlchemyError as e:
            logger.warning(f"[{ticker}] Cannot check the asset in the database: {e}")

        if asset_id is None:
            logger.warning(
                f"[{ticker}] Asset not found in the database — streaming without storing"
            )

        # Create or activate the subscription in the database
        if persist and asset_id is not None:
            try:
                await self._upsert_subscription(
                    asset_id=asset_id,
                    ticker=ticker,
                    tv_exchange=tv_exchange,
                    tv_symbol=tv_symbol,
                )
            except SQLAlchemyError as e:
                logger.warning(f"[{ticker}] Subscription storage error: {e}")

        # Start the streaming task
        task = asyncio.create_task(
            self._stream_ticker(ticker, tv_exchange, tv_symbol, asset_id),
            name=f"stream_{ticker}",
        )
        async with self._lock:
            self._tasks[ticker] = task

        # Add to the Redis set
        try:
            await self.redis.sadd(REDIS_SUBS_SET, ticker)
        except RedisError as e:
            logger.warning(f"[{ticker}] Redis set update error: {e}")

        logger.info(f"[RealtimeWorker] ✅ Subscribed to {ticker} ({tv_exchange}:{tv_symbol})")
        return True

    async def unsubscribe(self, ticker: str) -> bool:
        """
        Unsubscribe a ticker from streaming.

        1. Cancel its asyncio Task
        2. Set is_active=False in realtime_subscriptions
        3. Remove it from the Redis set
        4. Delete the Redis key quote:{ticker}

        Returns True when unsubscribed.
        """
        ticker = ticker.upper()

        async with self._lock:
            task = self._tasks.pop(ticker, None)

        if task is None:
            return False

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Deactivate in the database
        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(RealtimeSubscription)
                    .where(RealtimeSubscription.ticker == ticker)
                    .values(is_active=False)
                )
                await session.commit()
        except SQLAlchemyError as e:
            logger.warning(f"[{ticker}] Database deactivation error: {e}")

        # Clean Redis
        try:
            await self.redis.srem(REDIS_SUBS_SET, ticker)
            await self.redis.delete(REDIS_QUOTE_KEY.format(ticker=ticker))
        except RedisError as e:
            logger.warning(f"[{ticker}] Redis cleanup error: {e}")

        logger.info(f"[RealtimeWorker] 🔕 Unsubscribed from {ticker}")
        return True

    async def get_active_tickers(self) -> Set[str]:
        """The tickers streamed now."""
        try:
            members = await self.redis.smembers(REDIS_SUBS_SET)
            return {m.decode() if isinstance(m, bytes) else m for m in members}
        except RedisError:
            # Fallback: look at the local tasks
            async with self._lock:
                return set(self._tasks.keys())

    # ── Streaming per ticker ─────────────────────────────────────────────────

    async def _stream_ticker(
        self,
        ticker: str,
        tv_exchange: str,
        tv_symbol: str,
        asset_id: Optional[int],
    ) -> None:
        """
        Main asyncio task of a ticker.
        Loops forever, reconnecting automatically (exponential backoff).
        """
        delay = RECONNECT_DELAY_INIT

        while self._running:
            try:
                logger.info(f"[{ticker}] Connecting to TradingView ({tv_exchange}:{tv_symbol})")
                await self._run_streamer(ticker, tv_exchange, tv_symbol, asset_id)
                delay = RECONNECT_DELAY_INIT  # reset after a clean run
            except asyncio.CancelledError:
                logger.info(f"[{ticker}] Streaming stopped cleanly")
                return
            except Exception as e:
                logger.warning(f"[{ticker}] Streaming error: {e!r}. Reconnecting in {delay}s")
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _run_streamer(
        self,
        ticker: str,
        tv_exchange: str,
        tv_symbol: str,
        asset_id: Optional[int],
    ) -> None:
        """
        Run the TradingView Streamer in continuous streaming mode.

        tradingview-scraper is synchronous → `run_in_executor`.
        The Streamer returns an endless generator (continuous streaming).
        timeframe="1" → 1-minute candles
        numb_price_candles=1 → latest tick only
        """
        from tradingview_scraper.symbols.stream import Streamer

        loop = asyncio.get_event_loop()

        def _blocking_stream():
            """Runs in the thread pool. Returns a generator."""
            streamer = Streamer(export_result=False, export_type="json")
            return streamer.stream(
                exchange=tv_exchange,
                symbol=tv_symbol,
                timeframe="1",
                numb_price_candles=1,
            )

        async with self._semaphore:
            # Create the generator in the executor
            gen = await loop.run_in_executor(_executor, _blocking_stream)

            # Iterate over the generator asynchronously
            while self._running:
                try:
                    raw_data = await loop.run_in_executor(_executor, next, gen)
                    if raw_data:
                        await self._process_tick(ticker, raw_data, asset_id)
                except StopIteration:
                    logger.info(f"[{ticker}] TradingView generator exhausted — reconnecting")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[{ticker}] Tick read error: {e}")
                    raise

    async def _process_tick(
        self,
        ticker: str,
        raw_data: dict,
        asset_id: Optional[int],
    ) -> None:
        """
        Process a tick received from TradingView.

        1. Validate and normalise the data → RealtimeTick
        2. Store it in Redis (SET quote:{ticker} EX QUOTE_TTL + PUBLISH price:{ticker})
        3. Known asset → upsert prices_intraday
        4. Update last_tick_at and tick_count in realtime_subscriptions
        """
        # ── Validation ───────────────────────────────────────────────────────
        try:
            close_raw = raw_data.get("close") or raw_data.get("Close")
            if not close_raw:
                return

            close_val = Decimal(str(close_raw))
            if close_val <= 0:
                logger.debug(f"[{ticker}] Tick rejected: close={close_val}")
                return

            # Timestamp
            ts_raw = raw_data.get("timestamp") or raw_data.get("time")
            if ts_raw is None:
                tick_ts = datetime.now(timezone.utc)
            elif isinstance(ts_raw, (int, float)):
                tick_ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
            else:
                tick_ts = datetime.fromisoformat(str(ts_raw))
                if tick_ts.tzinfo is None:
                    tick_ts = tick_ts.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            # Ignore ticks that are too old (>5min) or in the future
            if tick_ts > now + timedelta(minutes=1):
                logger.debug(f"[{ticker}] Tick in the future ignored: {tick_ts}")
                return
            if tick_ts < now - timedelta(minutes=5):
                logger.debug(f"[{ticker}] Tick too old ignored: {tick_ts}")
                return

            volume_raw = raw_data.get("volume") or raw_data.get("Volume")
            volume = int(volume_raw) if volume_raw is not None else None
            if volume is not None and volume < 0:
                volume = None

            def _to_dec(v) -> Optional[Decimal]:
                try:
                    return Decimal(str(v)) if v is not None else None
                except InvalidOperation:
                    return None

            tick = RealtimeTick(
                ticker=ticker,
                timestamp=tick_ts,
                open=_to_dec(raw_data.get("open") or raw_data.get("Open")),
                high=_to_dec(raw_data.get("high") or raw_data.get("High")),
                low=_to_dec(raw_data.get("low") or raw_data.get("Low")),
                close=close_val,
                volume=volume,
                source="tradingview",
            )

        except (ValueError, InvalidOperation, TypeError) as e:
            logger.warning(f"[{ticker}] Invalid tick ignored: {e} — {raw_data}")
            return

        # ── Redis ────────────────────────────────────────────────────────────
        tick_json = tick.model_dump(mode="json")
        tick_str = json.dumps(tick_json, default=str)
        try:
            pipe = self.redis.pipeline()
            pipe.set(
                REDIS_QUOTE_KEY.format(ticker=ticker),
                tick_str,
                ex=QUOTE_TTL,
            )
            pipe.publish(
                REDIS_PUBSUB_CHAN.format(ticker=ticker),
                tick_str,
            )
            await pipe.execute()
        except RedisError as e:
            logger.warning(f"[{ticker}] Redis publish/set error: {e}")

        # ── Database ─────────────────────────────────────────────────────────
        if asset_id is not None:
            try:
                await self._upsert_intraday(asset_id, tick)
            except SQLAlchemyError as e:
                logger.warning(f"[{ticker}] prices_intraday upsert error: {e}")

            # Update last_tick_at + tick_count
            try:
                async with self._session_factory() as session:
                    await session.execute(
                        update(RealtimeSubscription)
                        .where(RealtimeSubscription.asset_id == asset_id)
                        .values(
                            last_tick_at=datetime.now(timezone.utc),
                            tick_count=RealtimeSubscription.tick_count + 1,
                        )
                    )
                    await session.commit()
            except SQLAlchemyError as e:
                logger.debug(f"[{ticker}] Subscription stats update error: {e}")

        logger.debug(f"[{ticker}] Tick processed: close={tick.close} ts={tick.timestamp}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _resolve_tv_symbol(self, ticker: str) -> tuple[str, str]:
        """
        Turn a Yahoo Finance ticker into (tv_exchange, tv_symbol).

        Examples:
        "AIR.PA" → ("EURONEXT", "AIR")
        "BNP.PA" → ("EURONEXT", "BNP")
        "BMW.DE" → ("XETRA", "BMW")
        "AAPL"   → ("NASDAQ", "AAPL")
        "TSLA"   → ("NASDAQ", "TSLA")
        """
        ticker_up = ticker.upper()
        for suffix, exchange in TV_EXCHANGE_MAP.items():
            if ticker_up.endswith(suffix.upper()):
                symbol = ticker[: -(len(suffix))]
                return exchange, symbol.upper()
        return "NASDAQ", ticker_up

    async def _load_active_subscriptions(self) -> list[RealtimeSubscription]:
        """Load the active subscriptions stored in realtime_subscriptions."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(RealtimeSubscription).where(
                        RealtimeSubscription.is_active == True  # noqa: E712
                    )
                )
                return list(result.scalars().all())
        except SQLAlchemyError as e:
            logger.warning(f"[RealtimeWorker] Cannot load the subscriptions: {e}")
            return []

    async def _upsert_subscription(
        self,
        asset_id: int,
        ticker: str,
        tv_exchange: str,
        tv_symbol: str,
    ) -> None:
        """Upsert a RealtimeSubscription (insert, or update is_active=True)."""
        async with self._session_factory() as session:
            stmt = (
                pg_insert(RealtimeSubscription)
                .values(
                    asset_id=asset_id,
                    ticker=ticker,
                    tv_exchange=tv_exchange,
                    tv_symbol=tv_symbol,
                    is_active=True,
                    subscribed_at=datetime.now(timezone.utc),
                    tick_count=0,
                )
                .on_conflict_do_update(
                    constraint="uq_realtime_sub_asset",
                    set_={
                        "ticker": ticker,
                        "tv_exchange": tv_exchange,
                        "tv_symbol": tv_symbol,
                        "is_active": True,
                        "subscribed_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def _upsert_intraday(self, asset_id: int, tick: RealtimeTick) -> None:
        """
        Upsert a tick into prices_intraday with SQLAlchemy Core.
        ON CONFLICT (timestamp, asset_id) DO UPDATE — atomic.
        """
        async with self._session_factory() as session:
            stmt = (
                pg_insert(PriceIntraday)
                .values(
                    timestamp=tick.timestamp,
                    asset_id=asset_id,
                    open=tick.open,
                    high=tick.high,
                    low=tick.low,
                    close=tick.close,
                    volume=tick.volume,
                    resolution="1min",
                    source="tradingview",
                )
                .on_conflict_do_update(
                    index_elements=["timestamp", "asset_id"],
                    set_={
                        "open": tick.open,
                        "high": tick.high,
                        "low": tick.low,
                        "close": tick.close,
                        "volume": tick.volume,
                        "source": "tradingview",
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def get_quote_from_cache(self, ticker: str) -> Optional[dict]:
        """
        The last price stored in Redis.
        None when there is no data or the TTL has expired.
        """
        try:
            raw = await self.redis.get(REDIS_QUOTE_KEY.format(ticker=ticker.upper()))
            if raw:
                return json.loads(raw)
        except (RedisError, json.JSONDecodeError, TypeError, UnicodeError) as e:
            logger.warning(f"[{ticker}] Redis quote read error: {e}")
        return None
