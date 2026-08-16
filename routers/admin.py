"""Operational endpoints for cache and database administration."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from cache.service import CacheService
from concurrency import run_sync
from database.service import DatabaseService

router = APIRouter(tags=["Operations"])


class CleanupRequest(BaseModel):
    days_to_keep: int = 730


def get_cache_service(request: Request) -> CacheService:
    service = getattr(request.app.state, "cache_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Cache non initialisé")
    return service


def get_database_service(request: Request) -> DatabaseService:
    service = getattr(request.app.state, "db_service", None)
    if service is None or getattr(request.app.state, "db_available", True) is False:
        raise HTTPException(status_code=503, detail="Base de données indisponible")
    return service


@router.get("/health")
async def health_check(cache: CacheService = Depends(get_cache_service)):
    redis_status = await run_sync(cache.get_status)
    return {
        "status": "healthy",
        "service": "FonRex API",
        "timestamp": datetime.now().isoformat(),
        "yfinance_available": True,
        "cache": {
            "enabled": cache.enabled,
            "status": redis_status,
            "ttl_seconds": cache.ttl_by_type if cache.enabled else None,
        },
    }


@router.post("/cache/clear")
async def clear_cache(cache: CacheService = Depends(get_cache_service)):
    if not cache.enabled:
        raise HTTPException(status_code=400, detail="Le cache Redis n'est pas activé")
    success, deleted_count, error = await run_sync(cache.clear, "eod:*")
    if not success:
        raise HTTPException(status_code=500, detail=error)
    return {
        "status": "success",
        "message": "Cache vidé avec succès" if deleted_count else "Cache déjà vide",
        "deleted_keys": deleted_count,
    }


@router.post("/cache/clear/{ticker}")
async def clear_ticker_cache(
    ticker: str,
    period: str | None = Query(None),
    cache: CacheService = Depends(get_cache_service),
):
    if not cache.enabled:
        raise HTTPException(status_code=400, detail="Le cache Redis n'est pas activé")
    success, deleted_count, error = await run_sync(cache.clear_ticker_cache, ticker, period)
    if not success:
        raise HTTPException(status_code=500, detail=error)
    return {
        "status": "success",
        "ticker": ticker,
        "period": period or "all",
        "message": "Cache vidé avec succès" if deleted_count else "Cache déjà vide",
        "deleted_keys": deleted_count,
    }


@router.get("/cache/stats")
async def cache_stats(cache: CacheService = Depends(get_cache_service)):
    if not cache.enabled:
        raise HTTPException(status_code=400, detail="Le cache Redis n'est pas activé")
    success, stats, error = await run_sync(cache.get_stats)
    if not success:
        raise HTTPException(status_code=500, detail=error)
    return stats


@router.get("/database/stats")
async def database_stats(db: DatabaseService = Depends(get_database_service)):
    success, stats, error = await run_sync(db.get_database_stats)
    if not success:
        raise HTTPException(status_code=500, detail=error)
    return stats


@router.get("/database/tickers")
async def database_tickers(db: DatabaseService = Depends(get_database_service)):
    success, tickers, error = await run_sync(db.get_cached_tickers)
    if not success:
        raise HTTPException(status_code=500, detail=error)
    return tickers


@router.post("/database/cleanup")
async def database_cleanup(
    payload: CleanupRequest,
    db: DatabaseService = Depends(get_database_service),
):
    success, result, error = await run_sync(db.cleanup_old_data, payload.days_to_keep)
    if not success:
        raise HTTPException(status_code=500, detail=error)
    return result


@router.get("/database/ticker/{ticker}")
async def database_ticker_stats(
    ticker: str,
    db: DatabaseService = Depends(get_database_service),
):
    success, stats, error = await run_sync(db.get_ticker_stats, ticker)
    if not success:
        raise HTTPException(status_code=404, detail=error)
    return stats
