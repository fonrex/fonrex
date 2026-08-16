"""HTTP routes for asset identity, listings and EOD prices."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from cache.service import CacheService
from concurrency import run_sync
from database.query import QueryService
from database.service import DatabaseService
from historical.ingestion_service import HistoricalIngestionService
from routers.dependencies import (
    get_cache_service,
    get_database_service,
    get_ingestion_service,
    get_query_service,
)

router = APIRouter(tags=["Assets"])
VALID_PERIODS = {
    "1d",
    "5d",
    "1mo",
    "3mo",
    "6mo",
    "1y",
    "2y",
    "5y",
    "10y",
    "ytd",
    "max",
    "daily",
    "weekly",
    "monthly",
}


@router.get("/assets/by-isin/{isin}")
async def get_asset_by_isin(
    isin: str,
    db: DatabaseService = Depends(get_database_service),
):
    context = await run_sync(db.get_asset_context, isin=isin)
    if not context:
        raise HTTPException(status_code=404, detail=f"ISIN {isin} introuvable")
    return context["details"]


@router.get("/listings")
async def search_listings(
    ticker: Optional[str] = None,
    isin: Optional[str] = None,
    exchange: Optional[str] = None,
    currency: Optional[str] = None,
    db: DatabaseService = Depends(get_database_service),
):
    if not any((ticker, isin, exchange, currency)):
        raise HTTPException(
            status_code=400,
            detail="Au moins un filtre ticker, isin, exchange ou currency est requis",
        )

    listings = await run_sync(
        db.find_listings,
        ticker=ticker,
        isin=isin,
        exchange=exchange,
        currency=currency,
        active_only=True,
    )
    return {
        "count": len(listings),
        "listings": [db._listing_to_dict(listing) for listing in listings],
    }


def _invalid(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "Invalid request", "message": message},
    )


def _validate_eod_request(ticker, period, fmt, order, from_date, to_date):
    if not ticker or len(ticker.strip()) == 0 or len(ticker) > 10:
        return _invalid(f"Le symbole '{ticker}' n'est pas valide")
    if not ticker.replace("-", "").replace(".", "").isalnum():
        return _invalid(f"Le symbole '{ticker}' n'est pas valide")
    if not period and not (from_date and to_date):
        return _invalid("Le paramètre 'period' est requis")
    if period and period not in VALID_PERIODS:
        return _invalid(f"La période '{period}' n'est pas valide")
    if fmt and fmt not in {"json", "csv"}:
        return _invalid(f"Le format '{fmt}' n'est pas valide")
    if order not in {"a", "d"}:
        return _invalid("Le paramètre 'order' doit être 'a' ou 'd'")
    if bool(from_date) != bool(to_date):
        return _invalid("Les paramètres 'from' et 'to' doivent être fournis ensemble")
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d") if from_date else None
        end = datetime.strptime(to_date, "%Y-%m-%d") if to_date else None
    except ValueError:
        return _invalid("Les dates doivent être au format YYYY-MM-DD")
    if start and end and start > end:
        return _invalid("La date 'from' doit être antérieure ou égale à la date 'to'")
    return None


def _period_bounds(period: Optional[str], from_date: Optional[str], to_date: Optional[str]):
    if from_date and to_date:
        return (
            datetime.strptime(from_date, "%Y-%m-%d").date(),
            datetime.strptime(to_date, "%Y-%m-%d").date(),
        )
    today = date.today()
    if period == "ytd":
        return date(today.year, 1, 1), today
    days = {
        "1d": 1,
        "5d": 5,
        "1mo": 30,
        "3mo": 90,
        "6mo": 180,
        "1y": 365,
        "2y": 730,
        "5y": 1825,
        "10y": 3650,
        "max": 7300,
        "daily": 365,
        "weekly": 365,
        "monthly": 730,
    }.get(period, 365)
    return today - timedelta(days=days), today


def _resolution(period: Optional[str]) -> str:
    normalized = (period or "").lower()
    if normalized in {"weekly", "1wk", "1w"}:
        return "1W"
    if normalized == "monthly":
        return "1M"
    return "1D"


def _format_records(rows, descending: bool):
    records = []
    for row in sorted(rows, key=lambda item: item.get("time"), reverse=descending):
        timestamp = row.get("time")
        displayed_date = (
            timestamp.strftime("%m-%d-%Y")
            if isinstance(timestamp, (date, datetime))
            else str(timestamp)
        )
        close = float(row.get("close") or 0)
        adjusted = row.get("adj_close")
        records.append(
            {
                "Date": displayed_date,
                "Open": float(row.get("open") or 0),
                "High": float(row.get("high") or 0),
                "Low": float(row.get("low") or 0),
                "Close": close,
                "Adj Close": float(adjusted) if adjusted is not None else close,
                "Volume": int(row.get("volume") or 0),
            }
        )
    return records


def _records_to_csv(records):
    import pandas as pd

    return pd.DataFrame(records).to_csv(index=False)


@router.get("/eod/{ticker}")
async def get_eod(
    ticker: str,
    request: Request,
    period: Optional[str] = Query(None),
    fmt: Optional[str] = Query(None),
    order: str = Query("a"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    query_service: QueryService = Depends(get_query_service),
    ingestion_service: HistoricalIngestionService = Depends(get_ingestion_service),
    cache: Optional[CacheService] = Depends(get_cache_service),
):
    validation_error = _validate_eod_request(ticker, period, fmt, order, from_date, to_date)
    if validation_error:
        return validation_error

    ticker = ticker.strip().upper()
    cache_key = None
    if cache and cache.enabled:
        cache_key = cache.generate_key(
            ticker,
            period,
            cache_type="eod",
            fmt=fmt or "json",
            order=order,
            from_date=from_date,
            to_date=to_date,
        )
        cached = await run_sync(cache.get, cache_key)
        if cached:
            request.state.cache_hit = True
            request.state.provider_used = "cache"
            if cached.get("media_type") == "text/csv":
                return Response(content=cached["body"], media_type="text/csv")
            return JSONResponse(
                content=cached["body"],
                status_code=cached.get("status_code", 200),
            )

    start_date, end_date = _period_bounds(period, from_date, to_date)
    resolution = _resolution(period)
    rows = await query_service.get_history(
        ticker,
        start_date=start_date,
        end_date=end_date,
        interval=resolution,
    )
    source = "database"
    if not rows:
        result = await ingestion_service.ingest(
            ticker=ticker,
            resolution=resolution,
            source="auto",
            from_date=start_date,
            to_date=end_date,
        )
        if result.status in {"success", "up_to_date"}:
            rows = await query_service.get_history(
                ticker,
                start_date=start_date,
                end_date=end_date,
                interval=resolution,
            )
            source = result.source_used or "yfinance"

    if not rows:
        return JSONResponse(
            status_code=404,
            content={"error": "No data found", "message": f"Aucune donnée trouvée pour {ticker}"},
        )

    records = await run_sync(_format_records, rows, descending=order == "d")
    request.state.cache_hit = False
    request.state.provider_used = source

    if fmt == "csv":
        content = await run_sync(_records_to_csv, records)
        if cache_key and cache and cache.enabled:
            await run_sync(
                cache.set,
                cache_key,
                {"status_code": 200, "media_type": "text/csv", "body": content},
                "eod",
            )
        return Response(content=content, media_type="text/csv")

    payload = {
        "ticker": ticker,
        "period": period,
        "format": "json",
        "count": len(records),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "data_source": source,
        "data": records,
    }
    if cache_key and cache and cache.enabled:
        await run_sync(
            cache.set,
            cache_key,
            {"status_code": 200, "media_type": "application/json", "body": payload},
            "eod",
        )
    return JSONResponse(content=payload)
