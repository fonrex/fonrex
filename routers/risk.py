"""HTTP route of the risk of a listing (``/risk``)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from cache.adapters import ResilientAsyncJsonCache
from risk.metrics import DEFAULT_CONFIDENCE
from risk.service import DEFAULT_YEARS, RiskError, RiskResult, RiskService
from schemas.risk import DrawdownInfo, RiskFreeRateInfo, RiskResponse

router = APIRouter(prefix="/risk", tags=["Risk"])

# Prices are daily: an answer is kept 6 hours.
RISK_CACHE_TTL = 6 * 3600


def get_risk_service(request: Request) -> RiskService:
    # The service is built even when the database is down or not at the Alembic head
    # (rule 24): it must not read a schema the API refused.
    if getattr(request.app.state, "db_available", True) is False:
        raise HTTPException(status_code=503, detail="Database schema unavailable or outdated")
    service = getattr(request.app.state, "risk_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Risk metrics unavailable (no database, or OECD or Treasury not started)",
        )
    return service


def get_risk_cache(request: Request) -> Optional[ResilientAsyncJsonCache]:
    redis_client = getattr(request.app.state, "redis_client", None)
    return ResilientAsyncJsonCache(redis_client) if redis_client is not None else None


def risk_cache_key(
    ticker: str,
    years: int,
    confidence: float,
    currency: Optional[str],
    exchange: Optional[str],
    isin: Optional[str],
) -> str:
    """Every parameter that changes the answer (rule 19). The confidence is written in
    full (``0.95``, ``0.9500001``): two confidences never share an answer."""
    listing = ":".join((value or "").upper() for value in (currency, exchange, isin))
    return f"risk:{ticker.upper()}:{years}y:{confidence!r}:{listing}"


def worth_caching(result: RiskResult) -> bool:
    """An answer without the rate of a currency that has a source is not kept: the
    source may answer again (timeout, quota, rates not loaded yet), and the cached
    answer would hide Sharpe and Sortino for ``RISK_CACHE_TTL``. A currency without
    any source gives the same answer every time: it is kept."""
    rates = result.risk_free
    return rates.rates is not None or rates.source is None


def response_of(result: RiskResult) -> RiskResponse:
    metrics, rates = result.metrics, result.risk_free
    drawdown = metrics.max_drawdown
    return RiskResponse(
        ticker=result.ticker,
        listing=result.listing,
        years=result.years,
        start=metrics.start,
        end=metrics.end,
        returns=metrics.returns,
        confidence=metrics.confidence,
        annualized_return=metrics.annualized_return,
        annualized_volatility=metrics.annualized_volatility,
        max_drawdown=DrawdownInfo(
            value=drawdown.value,
            peak=drawdown.peak,
            trough=drawdown.trough,
            recovery=drawdown.recovery,
        )
        if drawdown is not None
        else None,
        calmar_ratio=metrics.calmar_ratio,
        historical_var=metrics.historical_var,
        historical_cvar=metrics.historical_cvar,
        gaussian_var=metrics.gaussian_var,
        gaussian_cvar=metrics.gaussian_cvar,
        sharpe_ratio=metrics.sharpe_ratio,
        sortino_ratio=metrics.sortino_ratio,
        risk_free_rate=RiskFreeRateInfo(
            currency=rates.currency,
            source=rates.source,
            mean=metrics.mean_risk_free_rate,
            first_observation=rates.first_observation,
            last_observation=rates.last_observation,
            reason=rates.reason,
        ),
        warnings=result.warnings,
    )


@router.get("/{ticker}", response_model=RiskResponse)
async def get_risk_of_listing(
    ticker: str,
    years: int = Query(DEFAULT_YEARS, ge=1, le=10, description="Years of daily closes measured"),
    confidence: float = Query(
        DEFAULT_CONFIDENCE, ge=0.5, le=0.999, description="Confidence of the value at risk"
    ),
    currency: Optional[str] = Query(None, description="Currency of the listing to read"),
    exchange: Optional[str] = Query(None, description="Exchange of the listing to read"),
    isin: Optional[str] = Query(None, description="Instrument of the listing to read"),
    force_refresh: bool = Query(False, description="Measure again instead of the cached answer"),
    service: RiskService = Depends(get_risk_service),
    cache: Optional[ResilientAsyncJsonCache] = Depends(get_risk_cache),
):
    """Risk and risk-adjusted performance of a listing over its last years of daily closes.

    Volatility, maximum drawdown, historical and Gaussian value at risk and expected
    shortfall, Calmar, and Sharpe and Sortino on the 3-month rate of the currency of
    the listing (US Treasury for USD, OECD otherwise). Every figure is a ratio;
    losses are negative.
    """
    key = risk_cache_key(ticker, years, confidence, currency, exchange, isin)
    if cache is not None and not force_refresh:
        cached = await cache.get(key)
        if cached:
            try:
                return RiskResponse.model_validate_json(cached)
            except ValidationError:
                pass  # an entry of an older shape is measured again
    try:
        result = await service.measure(
            ticker,
            years=years,
            confidence=confidence,
            currency=currency,
            exchange=exchange,
            isin=isin,
        )
    except RiskError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error
    response = response_of(result)
    if cache is not None and worth_caching(result):
        await cache.setex(key, RISK_CACHE_TTL, response.model_dump_json())
    return response
