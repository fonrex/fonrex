"""HTTP routes for historical ingestion and price queries."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from redis.exceptions import RedisError

from cache.service import CacheService
from database.query import QueryService
from historical.ingestion_service import HistoricalIngestionService
from routers.dependencies import (
    get_cache_service,
    get_ingestion_service,
    get_query_service,
    get_redis_client,
)
from schemas import historical as historical_schemas

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Historical Data"])

# An ISIN: two letters of country, nine alphanumerics and a check character.
ISIN_QUERY_PATTERN = "^[A-Za-z]{2}[A-Za-z0-9]{10}$"


class PricePoint(BaseModel):
    time: datetime
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    adj_close: Optional[float] = None
    volume: Optional[int]


class ListingIdentity(BaseModel):
    """The listing a ticker designated: a client checks it is the instrument it wanted."""

    ticker: str
    isin: Optional[str] = None
    currency: Optional[str] = None
    exchange: Optional[str] = None


class HistoryResponse(BaseModel):
    ticker: str
    listing: Optional[ListingIdentity] = None
    interval: str
    count: int
    data: List[PricePoint]


class BulkIngestResponse(BaseModel):
    status: str
    results: List[historical_schemas.IngestResult]


@router.post("/historical/ingest", response_model=historical_schemas.IngestResult)
async def post_historical_ingest(
    ticker: str = Query(..., description="Ticker de l'actif"),
    resolution: str = Query("1D", pattern="^(1D|1W|1M)$"),
    source: str = Query("auto", pattern="^(auto|yfinance|tradingview)$"),
    force_refresh: bool = False,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    currency: Optional[str] = Query(None, max_length=10, description="Currency of the listing"),
    exchange: Optional[str] = Query(None, max_length=50, description="Exchange of the listing"),
    isin: Optional[str] = Query(
        None, pattern=ISIN_QUERY_PATTERN, description="ISIN of the instrument"
    ),
    service: HistoricalIngestionService = Depends(get_ingestion_service),
):
    return await service.ingest(
        ticker=ticker,
        resolution=resolution,
        source=source,
        force_refresh=force_refresh,
        from_date=from_date,
        to_date=to_date,
        currency=currency,
        exchange=exchange,
        isin=isin.upper() if isin else None,
    )


@router.post("/historical/ingest/bulk", response_model=BulkIngestResponse)
async def post_historical_ingest_bulk(
    payload: historical_schemas.BulkIngestRequest,
    service: HistoricalIngestionService = Depends(get_ingestion_service),
):
    results = await service.ingest_bulk(
        tickers=payload.tickers,
        resolution=payload.resolution.value,
        source=payload.source.value,
        force_refresh=payload.force_refresh,
        concurrency=payload.concurrency,
    )
    return BulkIngestResponse(status="completed", results=results)


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Type {type(value)} not serializable")


@router.get("/ticker/{symbol}/history", response_model=HistoryResponse)
async def get_ticker_history(
    symbol: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    interval: str = Query("1D", pattern="^(daily|weekly|monthly|1D|1W|1M)$"),
    currency: Optional[str] = Query(None, max_length=10, description="Currency of the listing"),
    exchange: Optional[str] = Query(None, max_length=50, description="Exchange of the listing"),
    isin: Optional[str] = Query(
        None, pattern=ISIN_QUERY_PATTERN, description="ISIN of the instrument"
    ),
    query_service: QueryService = Depends(get_query_service),
    redis_client=Depends(get_redis_client),
    cache_service: Optional[CacheService] = Depends(get_cache_service),
):
    aliases = {
        "daily": "1D",
        "weekly": "1W",
        "monthly": "1M",
        "1d": "1D",
        "1w": "1W",
        "1m": "1M",
        "1D": "1D",
        "1W": "1W",
        "1M": "1M",
    }
    normalized = aliases.get(interval.strip(), "1D")
    isin = isin.upper() if isin else None
    listing_choice = f"{(currency or '').upper()}:{(exchange or '').upper()}"
    if isin:
        listing_choice += f":{isin}"
    # Upper case, as the ingestion looks the entries up to drop them.
    cache_key = f"history:{symbol.upper()}:{normalized}:{start_date}:{end_date}:{listing_choice}"

    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except (RedisError, json.JSONDecodeError, TypeError, UnicodeError) as exc:
            logger.warning("History cache read failed for %s: %s", cache_key, exc)

    data = await query_service.get_history(
        symbol, start_date, end_date, interval, currency=currency, exchange=exchange, isin=isin
    )
    # Which instrument and listing the ticker designated, so that the client can check it.
    listing = await query_service.get_listing(symbol, currency, exchange, isin)

    response = {
        "ticker": symbol,
        "listing": listing,
        "interval": interval,
        "count": len(data),
        "data": data,
    }
    if redis_client and data:
        try:
            ttl = cache_service.get_ttl("history") if cache_service else 86400
            await redis_client.setex(cache_key, ttl, json.dumps(response, default=_json_default))
        except (RedisError, TypeError, ValueError) as exc:
            logger.warning("History cache write failed for %s: %s", cache_key, exc)
    return response
