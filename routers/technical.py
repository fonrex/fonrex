"""HTTP routes for technical indicators, charts, batches and screening."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from redis.exceptions import RedisError

from concurrency import run_sync
from models import Asset, AssetListing
from routers.dependencies import (
    get_database_service,
    get_redis_client,
    get_technical_service,
)
from schemas.technical import (
    IndicatorCategory,
    IndicatorInfo,
    IndicatorResult,
    MultiIndicatorResult,
    TechnicalRequest,
)
from technical.catalog import INDICATOR_DEFAULTS, INDICATOR_REGISTRY
from technical.errors import (
    IndicatorCalculationFailed,
    InsufficientHistoricalData,
    InvalidIndicator,
    TechnicalAnalysisError,
    TechnicalDataNotFound,
    UnsupportedIndicatorResolution,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/technical", tags=["Technical Indicators"])


def _raise_technical_http_error(error: TechnicalAnalysisError):
    """Translate domain failures at the HTTP boundary."""
    status_code = {
        InvalidIndicator: 400,
        UnsupportedIndicatorResolution: 400,
        TechnicalDataNotFound: 404,
        InsufficientHistoricalData: 422,
        IndicatorCalculationFailed: 422,
    }.get(type(error), 500)
    raise HTTPException(status_code=status_code, detail=error.detail) from error


INDICATOR_DESCRIPTIONS = {
    "sma": "Simple Moving Average — moyenne arithmétique des prix.",
    "ema": "Exponential Moving Average — moyenne favorisant les prix récents.",
    "wma": "Weighted Moving Average — moyenne mobile pondérée.",
    "dema": "Double Exponential Moving Average — moyenne à retard réduit.",
    "tema": "Triple Exponential Moving Average.",
    "vwap": "Volume Weighted Average Price — prix moyen pondéré par le volume.",
    "rsi": "Relative Strength Index — oscillateur de momentum.",
    "macd": "Moving Average Convergence Divergence — tendance et momentum.",
    "stoch": "Stochastic Oscillator — position de la clôture dans sa fourchette.",
    "cci": "Commodity Channel Index — écart du prix à sa moyenne.",
    "roc": "Rate of Change — pourcentage de variation du prix.",
    "mom": "Momentum — vitesse de variation des prix.",
    "bbands": "Bollinger Bands — bandes de volatilité.",
    "atr": "Average True Range — volatilité historique.",
    "kc": "Keltner Channel — enveloppes basées sur l’ATR.",
    "obv": "On-Balance Volume — momentum des volumes.",
    "ad": "Accumulation/Distribution Line — flux cumulatif.",
    "mfi": "Money Flow Index — momentum basé sur prix et volume.",
}


def _cache_enabled() -> bool:
    return os.environ.get("TECHNICAL_CACHE_ENABLED", "true").lower() == "true"


async def _cache_get(redis_client, key: str):
    if not redis_client or not _cache_enabled():
        return None
    try:
        payload = await redis_client.get(key)
        return json.loads(payload) if payload else None
    except (RedisError, json.JSONDecodeError, TypeError, UnicodeError) as exc:
        logger.warning("Technical cache read failed for %s: %s", key, exc)
        return None


async def _cache_set(redis_client, key: str, ttl: int, value) -> None:
    if not redis_client or not _cache_enabled():
        return
    try:
        await redis_client.setex(key, ttl, json.dumps(value, default=str))
    except (RedisError, TypeError, ValueError) as exc:
        logger.warning("Technical cache write failed for %s: %s", key, exc)


def _indicator_catalog() -> list[IndicatorInfo]:
    result = []
    for name, info in INDICATOR_REGISTRY.items():
        defaults = INDICATOR_DEFAULTS.get(name, {})
        outputs = []
        for column in info["cols"]:
            for parameter, value in defaults.items():
                column = column.replace(f"{{{parameter}}}", str(value))
            outputs.append(column)

        if name in {
            "sma",
            "ema",
            "wma",
            "dema",
            "tema",
            "cci",
            "roc",
            "mom",
            "atr",
            "kc",
            "mfi",
            "bbands",
        }:
            minimum = defaults.get("length", 20)
        elif name == "rsi":
            minimum = defaults.get("length", 14) + 1
        elif name == "macd":
            minimum = defaults.get("slow", 26) + defaults.get("signal", 9)
        elif name == "stoch":
            minimum = defaults.get("k", 14) + defaults.get("d", 3)
        else:
            minimum = 1

        example = f"/technical/AIR.PA?indicator={name}"
        if "length" in defaults:
            example += f"&period={defaults['length']}"
        result.append(
            IndicatorInfo(
                name=name,
                description=INDICATOR_DESCRIPTIONS.get(name, ""),
                category=IndicatorCategory(info["category"]),
                params=defaults,
                min_periods=minimum,
                outputs=outputs,
                example=example,
            )
        )
    return result


@router.get("/list", response_model=List[IndicatorInfo])
async def list_indicators(redis_client=Depends(get_redis_client)):
    cached = await _cache_get(redis_client, "technical:list")
    if cached is not None:
        return cached
    indicators = _indicator_catalog()
    await _cache_set(
        redis_client,
        "technical:list",
        86400,
        [indicator.model_dump() for indicator in indicators],
    )
    return indicators


async def run_indicator_screen(
    *,
    db_service,
    technical_service,
    redis_client=None,
    indicator: str = "rsi",
    operator: str = "lt",
    value: float = 30,
    resolution: str = "1D",
    period: int = 14,
    limit: int = 50,
):
    if operator not in {"lt", "gt", "lte", "gte"}:
        raise HTTPException(400, "Opérateur non valide : lt, gt, lte ou gte attendu")

    cache_key = f"technical:screen:{indicator}:{operator}:{value}:{resolution}:{period}:{limit}"
    cached = await _cache_get(redis_client, cache_key)
    if cached is not None:
        return cached

    def load_tickers():
        session = db_service.get_session()
        try:
            listings = (
                session.query(AssetListing)
                .join(Asset)
                .filter(AssetListing.is_active.is_(True), AssetListing.is_primary.is_(True))
                .limit(limit)
                .all()
            )
            return [
                {"ticker": listing.ticker, "name": listing.asset.name, "isin": listing.asset.isin}
                for listing in listings
            ]
        finally:
            session.close()

    tickers = await run_sync(load_tickers)
    semaphore = asyncio.Semaphore(10)

    async def evaluate(asset):
        async with semaphore:
            try:
                response = await technical_service.calculate(
                    ticker=asset["ticker"],
                    indicator=indicator,
                    params={"length": period},
                    resolution=resolution,
                    limit=max(50, period * 3),
                )
                points = response.series[0].values if response.series else []
                last = next((point for point in reversed(points) if point.v is not None), None)
                if last is None:
                    return None
                measured = float(last.v)
                predicates = {
                    "lt": measured < value,
                    "gt": measured > value,
                    "lte": measured <= value,
                    "gte": measured >= value,
                }
                if predicates[operator]:
                    return {**asset, "value": str(round(measured, 2))}
            except TechnicalAnalysisError as exc:
                logger.debug("Technical screening failed for %s: %s", asset["ticker"], exc)
            return None

    matches = [
        match for match in await asyncio.gather(*(evaluate(asset) for asset in tickers)) if match
    ]
    response = {
        "indicator": indicator,
        "params": {"length": period},
        "operator": operator,
        "value": value,
        "resolution": resolution,
        "matches": matches,
        "total": len(matches),
        "calculated_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set(redis_client, cache_key, 900, response)
    return response


@router.get("/screen")
async def screen_by_indicator(
    indicator: str = "rsi",
    operator: str = "lt",
    value: float = 30,
    resolution: str = "1D",
    period: int = 14,
    limit: int = 50,
    db_service=Depends(get_database_service),
    technical_service=Depends(get_technical_service),
    redis_client=Depends(get_redis_client),
):
    return await run_indicator_screen(
        db_service=db_service,
        technical_service=technical_service,
        redis_client=redis_client,
        indicator=indicator,
        operator=operator,
        value=value,
        resolution=resolution,
        period=period,
        limit=limit,
    )


@router.post("/batch", response_model=Dict[str, MultiIndicatorResult])
async def get_technical_batch(
    payload: TechnicalRequest,
    service=Depends(get_technical_service),
):
    if len(payload.tickers) > 20:
        raise HTTPException(400, "Maximum 20 tickers par requête batch")
    if len(payload.indicators) > 10:
        raise HTTPException(400, "Maximum 10 indicateurs par requête batch")
    semaphore = asyncio.Semaphore(5)

    async def calculate(ticker):
        async with semaphore:
            try:
                start = (
                    datetime.strptime(payload.from_date, "%Y-%m-%d").date()
                    if payload.from_date
                    else None
                )
                end = (
                    datetime.strptime(payload.to_date, "%Y-%m-%d").date()
                    if payload.to_date
                    else None
                )
                result = await service.calculate_multi(
                    ticker=ticker,
                    indicators=payload.indicators,
                    resolution=payload.resolution,
                    from_date=start,
                    to_date=end,
                    limit=payload.limit,
                    include_ohlcv=payload.include_ohlcv,
                )
            except (TechnicalAnalysisError, ValueError) as exc:
                result = MultiIndicatorResult(
                    ticker=ticker,
                    resolution=payload.resolution,
                    count=0,
                    indicators={},
                    errors={"global": str(exc)},
                    calculated_at=datetime.now(timezone.utc),
                )
            return ticker, result

    return dict(await asyncio.gather(*(calculate(ticker) for ticker in payload.tickers)))


@router.get("/{ticker}", response_model=IndicatorResult)
async def get_technical_indicator(
    ticker: str,
    indicator: str = "rsi",
    period: int = None,
    fast: int = None,
    slow: int = None,
    signal: int = None,
    std: float = None,
    resolution: str = "1D",
    from_date: date = None,
    to_date: date = None,
    limit: int = 500,
    service=Depends(get_technical_service),
):
    params = {
        key: value
        for key, value in {
            "length": period,
            "fast": fast,
            "slow": slow,
            "signal": signal,
            "std": std,
        }.items()
        if value is not None
    }
    try:
        return await service.calculate(
            ticker=ticker,
            indicator=indicator,
            params=params or None,
            resolution=resolution,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
    except TechnicalAnalysisError as error:
        _raise_technical_http_error(error)


@router.get("/{ticker}/multi", response_model=MultiIndicatorResult)
async def get_multi_indicators(
    ticker: str,
    indicators: str = "sma_20,ema_50,rsi_14,macd",
    resolution: str = "1D",
    from_date: date = None,
    to_date: date = None,
    limit: int = 500,
    include_ohlcv: bool = False,
    service=Depends(get_technical_service),
):
    try:
        return await service.calculate_multi(
            ticker=ticker,
            indicators=[item.strip() for item in indicators.split(",") if item.strip()],
            resolution=resolution,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            include_ohlcv=include_ohlcv,
        )
    except TechnicalAnalysisError as error:
        _raise_technical_http_error(error)


@router.get("/{ticker}/chart")
async def get_chart_data(
    ticker: str,
    indicators: str = "sma_20,ema_50,volume",
    resolution: str = "1D",
    from_date: date = None,
    to_date: date = None,
    limit: int = 200,
    service=Depends(get_technical_service),
):
    names = [item.strip() for item in indicators.split(",") if item.strip().lower() != "volume"]
    try:
        result = await service.calculate_multi(
            ticker=ticker,
            indicators=names,
            resolution=resolution,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            include_ohlcv=True,
        )
    except TechnicalAnalysisError as error:
        _raise_technical_http_error(error)
    bars = result.ohlcv or []
    return {
        "ticker": ticker,
        "resolution": resolution,
        "timestamps": [
            bar.t.strftime("%Y-%m-%d") if resolution in {"1D", "1W", "1M"} else bar.t.isoformat()
            for bar in bars
        ],
        "ohlcv": {
            "open": [bar.o for bar in bars],
            "high": [bar.h for bar in bars],
            "low": [bar.l for bar in bars],
            "close": [bar.c for bar in bars],
            "volume": [bar.v for bar in bars],
        },
        "indicators": {
            series.name: [point.v for point in series.values]
            for indicator_result in result.indicators.values()
            for series in indicator_result.series
        },
    }
