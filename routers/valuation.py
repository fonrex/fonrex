"""HTTP routes for DCF valuation and sensitivity analysis."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from redis.exceptions import RedisError

from concurrency import run_sync
from schemas.dcf import (
    DCFModelResult,
    DCFRequest,
    DCFResult,
    SensitivityResult,
)
from valuation.dcf_service import DCFService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dcf", tags=["Valuation"])
DCF_CACHE_TTL = 21600


def get_dcf_service(request: Request) -> DCFService:
    service = getattr(request.app.state, "dcf_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="DCFService indisponible")
    return service


def get_redis_client(request: Request):
    return getattr(request.app.state, "redis_client", None)


async def _read_cache(redis_client, key: str):
    if redis_client is None:
        return None
    try:
        payload = await redis_client.get(key)
        return json.loads(payload) if payload else None
    except (RedisError, json.JSONDecodeError, TypeError, UnicodeError) as exc:
        logger.warning("Erreur lecture cache DCF (%s): %s", key, exc)
        return None


async def _write_cache(redis_client, key: str, result) -> None:
    if redis_client is None:
        return
    try:
        await redis_client.setex(key, DCF_CACHE_TTL, result.model_dump_json())
    except (RedisError, TypeError, ValueError) as exc:
        logger.warning("Erreur écriture cache DCF (%s): %s", key, exc)


def _decimal_range(start: float, stop: float, step: float) -> list[Decimal]:
    """Build a stable inclusive Decimal range from HTTP float parameters."""
    current = Decimal(str(start))
    maximum = Decimal(str(stop))
    increment = Decimal(str(step))
    values = []
    while current <= maximum:
        values.append(current.quantize(Decimal("0.0001")))
        current += increment
    return values


@router.get("/{ticker}", response_model=DCFResult)
async def get_dcf_valuation(
    ticker: str,
    force_refresh: bool = False,
    service: DCFService = Depends(get_dcf_service),
    redis_client=Depends(get_redis_client),
):
    """Compute the default free-cash-flow valuation for a ticker."""
    cache_key = f"dcf:{ticker.upper()}:fcf:default"
    if not force_refresh:
        cached = await _read_cache(redis_client, cache_key)
        if cached is not None:
            return cached

    try:
        result = await service.compute_dcf(ticker, DCFRequest(models=["fcf"]))
        await _write_cache(redis_client, cache_key, result)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{ticker}", response_model=DCFResult)
async def post_custom_dcf_valuation(
    ticker: str,
    payload: DCFRequest,
    service: DCFService = Depends(get_dcf_service),
):
    """Compute a custom, non-cached DCF valuation."""
    try:
        return await service.compute_dcf(ticker, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{ticker}/compare", response_model=DCFResult)
async def compare_dcf_models(
    ticker: str,
    force_refresh: bool = False,
    service: DCFService = Depends(get_dcf_service),
    redis_client=Depends(get_redis_client),
):
    """Compare FCF, EPS and DDM valuation models."""
    cache_key = f"dcf:{ticker.upper()}:compare"
    if not force_refresh:
        cached = await _read_cache(redis_client, cache_key)
        if cached is not None:
            return cached

    try:
        try:
            result = await service.compute_dcf(ticker, DCFRequest(models=["fcf", "eps", "ddm"]))
        except ValueError as exc:
            if "dividende" not in str(exc).lower() and "ddm" not in str(exc).lower():
                raise
            result = await service.compute_dcf(ticker, DCFRequest(models=["fcf", "eps"]))
            result.models["ddm"] = DCFModelResult(
                model_name="Dividend Discount Model (Gordon)",
                intrinsic_value_per_share=Decimal("0"),
                upside_pct=Decimal("0"),
                projected_values=[],
                terminal_value=Decimal("0"),
                present_values=[],
                pv_terminal=Decimal("0"),
                warnings=["Modèle DDM impossible : aucun dividende distribué."],
            )

        await _write_cache(redis_client, cache_key, result)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{ticker}/sensitivity", response_model=SensitivityResult)
async def get_dcf_sensitivity(
    ticker: str,
    model: Literal["fcf", "eps", "ddm"] = "fcf",
    wacc_min: float = 0.06,
    wacc_max: float = 0.16,
    wacc_step: float = 0.02,
    growth_min: float = 0.01,
    growth_max: float = 0.05,
    growth_step: float = 0.01,
    force_refresh: bool = False,
    service: DCFService = Depends(get_dcf_service),
    redis_client=Depends(get_redis_client),
):
    """Compute the intrinsic-value sensitivity matrix."""
    if wacc_step <= 0 or growth_step <= 0:
        raise HTTPException(status_code=400, detail="Les pas doivent être strictement positifs.")
    if wacc_min >= wacc_max or growth_min >= growth_max:
        raise HTTPException(
            status_code=400, detail="Les minima doivent être inférieurs aux maxima."
        )

    cache_key = (
        f"dcf:{ticker.upper()}:sensitivity:{model}:"
        f"{wacc_min}:{wacc_max}:{wacc_step}:{growth_min}:{growth_max}:{growth_step}"
    )
    if not force_refresh:
        cached = await _read_cache(redis_client, cache_key)
        if cached is not None:
            return cached

    try:
        result = await run_sync(
            service.compute_sensitivity,
            ticker,
            model,
            _decimal_range(wacc_min, wacc_max, wacc_step),
            _decimal_range(growth_min, growth_max, growth_step),
        )
        await _write_cache(redis_client, cache_key, result)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
