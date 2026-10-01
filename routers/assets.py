"""HTTP routes for asset identity, listings and EOD prices."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

import json
import logging
import urllib.request
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

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

SEARCH_TOP_RESULTS = [
    {"ticker": "AAPL", "name": "Apple Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "AAPL", "value": "AAPL"},
    {"ticker": "ADBE", "name": "Adobe Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "ADBE", "value": "ADBE"},
    {"ticker": "AGG", "name": "iShares Core U.S. Aggregate Bond ETF", "quote_type": "ETF", "exchange": "AMEX", "label": "AGG", "value": "AGG"},
    {"ticker": "AMZN", "name": "Amazon.com, Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "AMZN", "value": "AMZN"},
    {"ticker": "BAC", "name": "Bank of America Corporation", "quote_type": "STOCK", "exchange": "NYSE", "label": "BAC", "value": "BAC"},
    {"ticker": "COMP", "name": "Compass, Inc.", "quote_type": "STOCK", "exchange": "NYSE", "label": "COMP", "value": "COMP"},
    {"ticker": "DIA", "name": "SPDR Dow Jones Industrial Average ETF Trust", "quote_type": "ETF", "exchange": "AMEX", "label": "DIA", "value": "DIA"},
    {"ticker": "DIS", "name": "The Walt Disney Company", "quote_type": "STOCK", "exchange": "NYSE", "label": "DIS", "value": "DIS"},
    {"ticker": "AIR.PA", "name": "Airbus SE", "quote_type": "STOCK", "exchange": "EURONEXT", "label": "AIR.PA", "value": "AIR.PA"},
    {"ticker": "TTE", "name": "TotalEnergies SE", "quote_type": "STOCK", "exchange": "NYSE", "label": "TTE", "value": "TTE"},
    {"ticker": "GOOG", "name": "Alphabet Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "GOOG", "value": "GOOG"},
    {"ticker": "MSFT", "name": "Microsoft Corporation", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "MSFT", "value": "MSFT"},
    {"ticker": "NVDA", "name": "NVIDIA Corporation", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "NVDA", "value": "NVDA"},
    {"ticker": "TSLA", "name": "Tesla, Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "TSLA", "value": "TSLA"},
]


import unicodedata


def strip_accents(text: str) -> str:
    """Normalize text by stripping diacritics/accents (e.g. Crédit -> Credit)."""
    if not text:
        return ""
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def fetch_yahoo_search_quotes(query: str, limit: int = 10) -> list[dict]:
    """Search Yahoo Finance for quotes matching query with accent normalization."""
    if not query:
        return []
    clean_query = strip_accents(query.strip())
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={quote_plus(clean_query)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            quotes = payload.get("quotes") or []
            return quotes[:limit]
    except Exception:
        pass
    return []


def fetch_yahoo_search_quote(query: str) -> Optional[dict]:
    """Backward-compatible helper returning first matching quote."""
    quotes = fetch_yahoo_search_quotes(query, limit=1)
    return quotes[0] if quotes else None


@router.get("/api/search")
@router.get("/search")
@router.get("/openbb/search")
async def search_tickers(
    q: Optional[str] = Query(None),
    db: DatabaseService = Depends(get_database_service),
):
    if not q or not q.strip():
        return {"results": SEARCH_TOP_RESULTS}

    query_raw = q.strip()
    query_clean = strip_accents(query_raw).lower()

    filtered = []
    seen_tickers = set()

    # 1. Base locale Fonrex en priorité (recherche par nom d'entreprise, ticker ou ISIN)
    try:
        db_results = await run_sync(db.search_assets_by_text, query_raw, limit=10)
        for item in db_results:
            sym = item.get("ticker")
            if sym and sym not in seen_tickers:
                seen_tickers.add(sym)
                filtered.append(item)
    except Exception as exc:
        logger.warning("Erreur recherche assets text db: %s", exc)

    # 2. Résultats statiques prédéfinis
    for r in SEARCH_TOP_RESULTS:
        r_ticker = r["ticker"]
        if r_ticker in seen_tickers:
            continue
        if query_clean in strip_accents(r_ticker).lower() or query_clean in strip_accents(r["name"]).lower():
            seen_tickers.add(r_ticker)
            item = dict(r)
            item["source"] = item.get("source", "catalog")
            filtered.append(item)

    # 3. Fallback externe Yahoo Finance si moins de 5 résultats trouvés localement
    if len(filtered) < 5:
        try:
            quotes = await run_sync(fetch_yahoo_search_quotes, query_raw, 10)
            exchange_names = {
                "PAR": "EURONEXT PARIS",
                "EPA": "EURONEXT PARIS",
                "AMS": "EURONEXT AMSTERDAM",
                "BRU": "EURONEXT BRUSSELS",
                "LIS": "EURONEXT LISBON",
                "GER": "XETRA",
                "FRA": "XETRA",
                "ETR": "XETRA",
                "LSE": "LSE",
                "MC": "BME",
                "MIL": "BORSA ITALIANA",
                "SWX": "SIX SWISS",
                "NMS": "NASDAQ",
                "NGS": "NASDAQ",
                "NCM": "NASDAQ",
                "NYQ": "NYSE",
            }
            for quote in quotes:
                sym = quote.get("symbol")
                if not sym or sym in seen_tickers:
                    continue
                seen_tickers.add(sym)

                raw_type = (quote.get("quoteType") or "STOCK").upper()
                if raw_type == "EQUITY":
                    raw_type = "STOCK"

                raw_exch = quote.get("exchange") or "UNKNOWN"
                exch_name = exchange_names.get(raw_exch.upper(), raw_exch)

                filtered.append({
                    "ticker": sym,
                    "name": quote.get("shortname") or quote.get("longname") or sym,
                    "quote_type": raw_type,
                    "exchange": exch_name,
                    "label": sym,
                    "value": sym,
                    "source": "yahoo",
                })
        except Exception as exc:
            logger.debug("Erreur fallback Yahoo quotes: %s", exc)

    return {"results": filtered}



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
            timestamp.strftime("%Y-%m-%d")
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
