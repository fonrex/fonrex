"""Application use cases for realtime quotes and subscriptions."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from redis.exceptions import RedisError

from concurrency import run_sync
from realtime.worker import REDIS_QUOTE_KEY
from schemas.realtime import QuoteSnapshot, SubscriptionStatus
from use_cases.errors import DependencyUnavailable, InvalidInput, ResourceNotFound

logger = logging.getLogger(__name__)


def _load_yfinance_fast_info(ticker):
    import yfinance as yf

    return yf.Ticker(ticker).fast_info


def require_worker(worker):
    if worker is None:
        raise DependencyUnavailable(
            "RealtimePriceWorker non disponible (DATABASE_URL manquant ou erreur démarrage)"
        )
    return worker


class GetQuote:
    def __init__(self, worker=None):
        self.worker = worker

    async def execute(self, ticker: str, subscribe_if_missing: bool = True):
        ticker = ticker.upper()
        if self.worker:
            cached = await self.worker.get_quote_from_cache(ticker)
            if cached:
                close = Decimal(str(cached.get("close", 0) or 0))
                previous = Decimal(str(cached.get("previous_close", close) or close))
                change = close - previous if previous else None
                change_pct = change / previous * 100 if previous and previous != 0 else None
                return QuoteSnapshot(
                    ticker=ticker,
                    price=close,
                    open=(Decimal(str(cached["open"])) if cached.get("open") else None),
                    high=(Decimal(str(cached["high"])) if cached.get("high") else None),
                    low=(Decimal(str(cached["low"])) if cached.get("low") else None),
                    close=close,
                    volume=cached.get("volume"),
                    change=change,
                    change_pct=change_pct,
                    previous_close=previous if previous != close else None,
                    timestamp=(
                        datetime.fromisoformat(cached["timestamp"])
                        if cached.get("timestamp")
                        else None
                    ),
                    is_realtime=True,
                    source="tradingview",
                    delay_seconds=0,
                )

        if subscribe_if_missing and self.worker:
            active = await self.worker.get_active_tickers()
            if ticker not in active:
                asyncio.create_task(self.worker.subscribe(ticker))

        try:
            info = await run_sync(_load_yfinance_fast_info, ticker)
            price = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
            previous_close = getattr(info, "previous_close", None)
            if price:
                price = Decimal(str(price))
                previous_close = Decimal(str(previous_close)) if previous_close else None
                change = price - previous_close if previous_close else None
                change_pct = (
                    change / previous_close * 100
                    if previous_close and previous_close != 0
                    else None
                )
                return QuoteSnapshot(
                    ticker=ticker,
                    price=price,
                    close=price,
                    change=change,
                    change_pct=change_pct,
                    previous_close=previous_close,
                    is_realtime=False,
                    source="yfinance",
                    delay_seconds=900,
                    timestamp=datetime.now(timezone.utc),
                )
        except Exception as exc:
            logger.warning("Fallback yfinance échoué pour %s: %s", ticker, exc)

        raise ResourceNotFound(f"Aucun prix disponible pour {ticker}")


class SubscribeTickers:
    def __init__(self, worker):
        self.worker = worker

    async def execute(self, tickers: list[str]):
        worker = require_worker(self.worker)
        if len(tickers) > 50:
            raise InvalidInput("Maximum 50 tickers par requête")

        results = []
        for raw_ticker in tickers:
            ticker = raw_ticker.upper()
            try:
                await worker.subscribe(ticker)
                tv_exchange, tv_symbol = worker._resolve_tv_symbol(ticker)
                active_tickers = await worker.get_active_tickers()
                results.append(
                    SubscriptionStatus(
                        ticker=ticker,
                        tv_exchange=tv_exchange,
                        tv_symbol=tv_symbol,
                        is_active=True,
                        subscribed_at=datetime.now(timezone.utc),
                        last_tick_at=None,
                        tick_count=0,
                        is_streaming=ticker in active_tickers,
                    )
                )
            except Exception as exc:
                logger.warning("Erreur abonnement %s: %s", ticker, exc)
        return results


class UnsubscribeTicker:
    def __init__(self, worker):
        self.worker = worker

    async def execute(self, ticker: str):
        worker = require_worker(self.worker)
        ticker = ticker.upper()
        if not await worker.unsubscribe(ticker):
            raise ResourceNotFound(f"Ticker {ticker} non trouvé dans les abonnements actifs")
        return {"status": "unsubscribed", "ticker": ticker}


class GetRealtimeStatus:
    def __init__(self, worker, connection_manager):
        self.worker = worker
        self.connection_manager = connection_manager

    async def execute(self):
        if self.worker is None:
            return {
                "streaming_count": 0,
                "active_tickers": [],
                "ws_connections": {},
                "worker_running": False,
                "error": "RealtimePriceWorker non initialisé",
            }

        active_tickers = await self.worker.get_active_tickers()
        stale_tickers = []
        for ticker in active_tickers:
            if await self.worker.get_quote_from_cache(ticker) is None:
                stale_tickers.append(ticker)

        subscriber_count = self.connection_manager.get_subscriber_count
        return {
            "streaming_count": len(active_tickers),
            "active_tickers": sorted(active_tickers),
            "ws_connections": {ticker: subscriber_count(ticker) for ticker in active_tickers},
            "total_ws_clients": sum(subscriber_count(ticker) for ticker in active_tickers),
            "stale_tickers": stale_tickers,
            "worker_running": self.worker._running,
        }


class GetQuotesBatch:
    def __init__(self, worker=None, redis=None):
        self.worker = worker
        self.redis = redis

    async def execute(self, tickers: str):
        ticker_list = [ticker.strip().upper() for ticker in tickers.split(",") if ticker.strip()][
            :20
        ]
        results = {}

        if self.worker and self.redis:
            try:
                pipe = self.redis.pipeline()
                for ticker in ticker_list:
                    pipe.get(REDIS_QUOTE_KEY.format(ticker=ticker))
                raw_values = await pipe.execute()
                for ticker, raw in zip(ticker_list, raw_values, strict=True):
                    if raw:
                        try:
                            results[ticker] = {
                                "data": json.loads(raw),
                                "is_realtime": True,
                                "source": "tradingview",
                            }
                        except (json.JSONDecodeError, TypeError, UnicodeError):
                            results[ticker] = None
                    else:
                        results[ticker] = None
            except RedisError as exc:
                logger.warning("Erreur pipeline Redis multi-quotes: %s", exc)

        missing = [ticker for ticker in ticker_list if not results.get(ticker)]
        if missing:
            try:
                for ticker in missing:
                    try:
                        info = await run_sync(_load_yfinance_fast_info, ticker)
                        price = getattr(info, "last_price", None) or getattr(
                            info, "regularMarketPrice", None
                        )
                        results[ticker] = (
                            {
                                "data": {"close": float(price), "ticker": ticker},
                                "is_realtime": False,
                                "source": "yfinance",
                                "delay_seconds": 900,
                            }
                            if price
                            else None
                        )
                    except Exception:
                        results[ticker] = None
            except ImportError:
                pass

        return {
            "count": len(ticker_list),
            "tickers": ticker_list,
            "quotes": results,
        }
