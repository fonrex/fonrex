"""FastAPI router providing OpenBB Workspace adapter endpoints.

Adapts Fonrex domain responses to the schema contracts required by OpenBB Workspace widgets:
- type: "metric"  -> [ { "label": ..., "value": ..., "delta": ... }, ... ]
- type: "chart"   -> Plotly Figure JSON { "data": [...], "layout": {...} }
- type: "table"   -> Flat list of row records [ { ... }, ... ]
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import JSONResponse

from concurrency import run_sync
from database.query import QueryService
from database.service import DatabaseService
from historical.ingestion_service import HistoricalIngestionService
from integrations.openbb.adapters import (
    format_batch_quotes_table,
    format_candlestick_chart,
    format_dcf_compare_table,
    format_dcf_sensitivity_table,
    format_dcf_table,
    format_dividends_table,
    format_earnings_history_table,
    format_etf_details_table,
    format_fundamentals_deep_table,
    format_fundamentals_table,
    format_indicator_chart,
    format_macro_rates_metric,
    format_quote_metric,
    format_revenue_geography_chart,
    format_stock_splits_table,
    format_technical_chart_overlay,
    format_technical_multi_chart,
    format_valuation_multiples_chart,
)
from routers.assets import fetch_yahoo_search_quotes, get_eod, strip_accents
from routers.dependencies import (
    get_cache_service,
    get_database_service,
    get_dividend_provider,
    get_earnings_provider,
    get_ingestion_service,
    get_query_service,
    get_redis_client,
    get_split_provider,
    get_technical_service,
    get_valuation_multiples_service,
)
from routers.fundamentals import (
    get_all_information,
    get_fundamental_deep,
    get_optional_database_service,
    get_provider_registry,
    get_sec_edgar_provider,
    get_validation_layer,
)
from routers.historical import get_ticker_history
from routers.macro import get_fred_service, get_macro_rates
from routers.news import get_news_feed, get_news_service, get_ticker_news
from routers.realtime import (
    get_quote,
    get_quotes_batch,
    get_realtime_worker,
)
from routers.specialized import (
    get_etf_details,
    get_geographic_revenue,
    get_index_constituents,
    get_index_name_enum,
    get_index_provider,
    get_insider_transactions,
    get_justetf_provider,
)
from routers.technical import (
    get_chart_data,
    get_multi_indicators,
    get_technical_indicator,
    screen_by_indicator,
)
from routers.valuation import (
    compare_dcf_models,
    get_dcf_sensitivity,
    get_dcf_service,
    get_dcf_valuation,
)

router = APIRouter(prefix="/openbb", tags=["OpenBB Adapters"])


# ──────────────────────────────────────────────────────────────────────────────
# Metric Endpoints (type: "metric")
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/quote/{ticker}")
async def get_openbb_quote(
    ticker: str,
    worker=Depends(get_realtime_worker),
) -> List[Dict[str, Any]]:
    """Return quote snapshot formatted as OpenBB metric tiles."""
    quote_snapshot = await get_quote(ticker=ticker, subscribe_if_missing=True, worker=worker)
    return format_quote_metric(quote_snapshot)


@router.get("/macro/rates")
async def get_openbb_macro_rates(
    service=Depends(get_fred_service),
) -> List[Dict[str, Any]]:
    """Return current macro rates formatted as OpenBB metric tiles."""
    rates_res = await get_macro_rates(service=service)
    return format_macro_rates_metric(rates_res)


# ──────────────────────────────────────────────────────────────────────────────
# Chart Endpoints (type: "chart" -> Plotly JSON)
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/eod/{ticker}")
async def get_openbb_eod(
    ticker: str,
    request: Request,
    period: Optional[str] = Query("1y"),
    order: str = Query("a"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    query_service: QueryService = Depends(get_query_service),
    ingestion_service: HistoricalIngestionService = Depends(get_ingestion_service),
    cache=Depends(get_cache_service),
) -> Dict[str, Any]:
    """Return EOD price history as a Plotly Candlestick chart."""
    response = await get_eod(
        ticker=ticker,
        request=request,
        period=period,
        fmt="json",
        order=order,
        from_date=from_date,
        to_date=to_date,
        query_service=query_service,
        ingestion_service=ingestion_service,
        cache=cache,
    )
    if isinstance(response, JSONResponse):
        payload = json.loads(response.body)
        if response.status_code >= 400:
            raise HTTPException(
                status_code=response.status_code,
                detail=payload.get("message") or payload.get("error") or "EOD error",
            )
        records = payload.get("data", [])
        return format_candlestick_chart(ticker.upper(), records, title=f"{ticker.upper()} EOD")
    return format_candlestick_chart(ticker.upper(), [])


@router.get("/ticker/{symbol}/history")
async def get_openbb_history(
    symbol: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    interval: str = Query("1D", pattern="^(daily|weekly|monthly|1D|1W|1M)$"),
    query_service: QueryService = Depends(get_query_service),
    redis_client=Depends(get_redis_client),
    cache_service=Depends(get_cache_service),
) -> Dict[str, Any]:
    """Return historical candles as a Plotly Candlestick chart."""
    res = await get_ticker_history(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval=interval,
        query_service=query_service,
        redis_client=redis_client,
        cache_service=cache_service,
    )
    records = res.get("data", [])
    return format_candlestick_chart(symbol.upper(), records, title=f"{symbol.upper()} History ({interval})")


@router.get("/technical/screen")
async def get_openbb_screener(
    indicator: str = "rsi",
    operator: str = "lt",
    value: float = 30,
    resolution: str = "1D",
    period: int = 14,
    limit: int = 50,
    db_service=Depends(get_database_service),
    technical_service=Depends(get_technical_service),
    redis_client=Depends(get_redis_client),
) -> List[Dict[str, Any]]:
    """Return technical screener matches directly as an AgGrid table."""
    res = await screen_by_indicator(
        indicator=indicator,
        operator=operator,
        value=value,
        resolution=resolution,
        period=period,
        limit=limit,
        db_service=db_service,
        technical_service=technical_service,
        redis_client=redis_client,
    )
    return res.get("matches", []) if isinstance(res, dict) else []


@router.get("/technical/{ticker}")
async def get_openbb_technical(
    ticker: str,
    indicator: str = "rsi",
    period: Optional[int] = None,
    fast: Optional[int] = None,
    slow: Optional[int] = None,
    signal: Optional[int] = None,
    std: Optional[float] = None,
    resolution: str = "1D",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 500,
    service=Depends(get_technical_service),
) -> Dict[str, Any]:
    """Return single technical indicator formatted as a Plotly line chart."""
    res = await get_technical_indicator(
        ticker=ticker,
        indicator=indicator,
        period=period,
        fast=fast,
        slow=slow,
        signal=signal,
        std=std,
        resolution=resolution,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        service=service,
    )
    return format_indicator_chart(ticker.upper(), indicator, res.series)


@router.get("/technical/{ticker}/multi")
async def get_openbb_technical_multi(
    ticker: str,
    indicators: str = "sma_20,ema_50,rsi_14",
    resolution: str = "1D",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 500,
    service=Depends(get_technical_service),
) -> Dict[str, Any]:
    """Return multiple technical indicators formatted as a Plotly line chart."""
    res = await get_multi_indicators(
        ticker=ticker,
        indicators=indicators,
        resolution=resolution,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        include_ohlcv=False,
        service=service,
    )
    return format_technical_multi_chart(ticker.upper(), res)


@router.get("/technical/{ticker}/chart")
async def get_openbb_technical_chart(
    ticker: str,
    indicators: str = "sma_20,rsi_14",
    resolution: str = "1D",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 200,
    service=Depends(get_technical_service),
) -> Dict[str, Any]:
    """Return candlesticks with overlaid indicators as a Plotly figure."""
    chart_payload = await get_chart_data(
        ticker=ticker,
        indicators=indicators,
        resolution=resolution,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        service=service,
    )
    return format_technical_chart_overlay(ticker.upper(), chart_payload)


@router.get("/tickers")
@router.get("/ticker-options")
async def get_openbb_tickers(
    q: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    db: DatabaseService = Depends(get_database_service),
) -> List[Dict[str, Any]]:
    """Return searchable ticker options with symbol, name, and exchange for OpenBB Workspace."""
    all_tickers = [
        {"ticker": "AAPL", "name": "Apple Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "AAPL", "value": "AAPL"},
        {"ticker": "ADBE", "name": "Adobe Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "ADBE", "value": "ADBE"},
        {"ticker": "AMZN", "name": "Amazon.com, Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "AMZN", "value": "AMZN"},
        {"ticker": "BAC", "name": "Bank of America Corporation", "quote_type": "STOCK", "exchange": "NYSE", "label": "BAC", "value": "BAC"},
        {"ticker": "DIS", "name": "The Walt Disney Company", "quote_type": "STOCK", "exchange": "NYSE", "label": "DIS", "value": "DIS"},
        {"ticker": "GOOG", "name": "Alphabet Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "GOOG", "value": "GOOG"},
        {"ticker": "HD", "name": "The Home Depot, Inc.", "quote_type": "STOCK", "exchange": "NYSE", "label": "HD", "value": "HD"},
        {"ticker": "JNJ", "name": "Johnson & Johnson", "quote_type": "STOCK", "exchange": "NYSE", "label": "JNJ", "value": "JNJ"},
        {"ticker": "MSFT", "name": "Microsoft Corporation", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "MSFT", "value": "MSFT"},
        {"ticker": "NVDA", "name": "NVIDIA Corporation", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "NVDA", "value": "NVDA"},
        {"ticker": "META", "name": "Meta Platforms, Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "META", "value": "META"},
        {"ticker": "TSLA", "name": "Tesla, Inc.", "quote_type": "STOCK", "exchange": "NASDAQ", "label": "TSLA", "value": "TSLA"},
        {"ticker": "AIR.PA", "name": "Airbus SE", "quote_type": "STOCK", "exchange": "EURONEXT", "label": "AIR.PA", "value": "AIR.PA"},
        {"ticker": "MC.PA", "name": "LVMH Moët Hennessy", "quote_type": "STOCK", "exchange": "EURONEXT", "label": "MC.PA", "value": "MC.PA"},
        {"ticker": "TTE.PA", "name": "TotalEnergies SE", "quote_type": "STOCK", "exchange": "EURONEXT", "label": "TTE.PA", "value": "TTE.PA"},
        {"ticker": "BNP.PA", "name": "BNP Paribas", "quote_type": "STOCK", "exchange": "EURONEXT", "label": "BNP.PA", "value": "BNP.PA"},
        {"ticker": "SAN.PA", "name": "Sanofi", "quote_type": "STOCK", "exchange": "EURONEXT", "label": "SAN.PA", "value": "SAN.PA"},
    ]

    search_raw = (q or query or "").strip()
    if not search_raw:
        return all_tickers

    search_clean = strip_accents(search_raw).lower()
    search_upper = search_raw.upper()
    filtered = []
    seen = set()

    # 1. Base locale Fonrex en priorité (database-first)
    try:
        db_results = await run_sync(db.search_assets_by_text, search_raw, limit=10)
        for item in db_results:
            sym = item.get("ticker")
            if sym and sym not in seen:
                seen.add(sym)
                filtered.append({
                    "ticker": sym,
                    "name": item.get("name") or sym,
                    "quote_type": item.get("quote_type") or "STOCK",
                    "exchange": item.get("exchange") or "UNKNOWN",
                    "label": sym,
                    "value": sym,
                })
    except Exception:
        pass

    # 2. Tickers prédéfinis du catalogue
    for item in all_tickers:
        sym = item["ticker"]
        if sym in seen:
            continue
        if search_clean in strip_accents(sym).lower() or search_clean in strip_accents(item["name"]).lower():
            seen.add(sym)
            filtered.append(dict(item))

    # 3. Fallback externe Yahoo si moins de 5 résultats
    if len(filtered) < 5:
        try:
            quotes = await run_sync(fetch_yahoo_search_quotes, search_raw, limit=8)
            for quote in quotes:
                sym = quote.get("symbol")
                if sym and sym not in seen:
                    seen.add(sym)
                    q_type = quote.get("quoteType") or "STOCK"
                    if q_type == "EQUITY":
                        q_type = "STOCK"
                    filtered.append({
                        "ticker": sym,
                        "name": quote.get("shortname") or quote.get("longname") or sym,
                        "quote_type": q_type,
                        "exchange": quote.get("exchange") or "UNKNOWN",
                        "label": sym,
                        "value": sym,
                    })
        except Exception:
            pass

    if search_upper not in seen:
        filtered.append({
            "ticker": search_upper,
            "name": search_upper,
            "quote_type": "STOCK",
            "exchange": "CUSTOM",
            "label": search_upper,
            "value": search_upper,
        })
    return filtered


@router.get("/fundamental/revenue-geography/period-options")
async def get_revenue_geography_period_options() -> List[Dict[str, str]]:
    """Return available period options for Revenue Per Geography widget."""
    return [
        {"label": "FY", "value": "FY"},
        {"label": "QTR", "value": "QTR"},
    ]


@router.get("/fundamental/{ticker}/revenue-geography")
async def get_openbb_revenue_geography(
    ticker: str,
    period: str = "FY",
    refresh: bool = False,
    provider=Depends(get_sec_edgar_provider),
    cache=Depends(get_cache_service),
) -> Dict[str, Any]:
    """Return geographic revenue segmentation formatted as a Plotly stacked bar chart."""
    clean_ticker = (ticker or "").strip()
    if clean_ticker.startswith("{") or clean_ticker.lower() in ("", "undefined", "none"):
        clean_ticker = "AAPL"
    data = await get_geographic_revenue(
        ticker=clean_ticker,
        refresh=refresh,
        period=period,
        provider=provider,
        cache=cache,
    )
    return format_revenue_geography_chart(clean_ticker.upper(), data)


@router.get("/fundamental/revenue-geography")
async def get_openbb_revenue_geography_query(
    ticker: str = "AAPL",
    period: str = "FY",
    refresh: bool = False,
    provider=Depends(get_sec_edgar_provider),
    cache=Depends(get_cache_service),
) -> Dict[str, Any]:
    """Fallback query-parameter route for OpenBB test requests."""
    return await get_openbb_revenue_geography(
        ticker=ticker,
        period=period,
        refresh=refresh,
        provider=provider,
        cache=cache,
    )


@router.get("/valuation/{ticker}/multiples")
async def get_openbb_valuation_multiples(
    ticker: str,
    period: str = "FY",
    refresh: bool = False,
    service=Depends(get_valuation_multiples_service),
) -> Dict[str, Any]:
    """Return historical valuation multiples (P/E, P/S, P/B, EV/Sales, EV/EBITDA) formatted as a Plotly line chart."""
    clean_ticker = (ticker or "").strip()
    if clean_ticker.startswith("{") or clean_ticker.lower() in ("", "undefined", "none"):
        clean_ticker = "AAPL"
    data = await service.get_multiples(ticker=clean_ticker, period=period, refresh=refresh)
    return format_valuation_multiples_chart(clean_ticker.upper(), data, period=period)


@router.get("/valuation/multiples")
async def get_openbb_valuation_multiples_query(
    ticker: str = "AAPL",
    period: str = "FY",
    refresh: bool = False,
    service=Depends(get_valuation_multiples_service),
) -> Dict[str, Any]:
    """Fallback query-parameter route for OpenBB test requests."""
    return await get_openbb_valuation_multiples(
        ticker=ticker, period=period, refresh=refresh, service=service
    )


# ──────────────────────────────────────────────────────────────────────────────
# Table Endpoints (type: "table" -> list[dict])
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/fundamental")
async def get_openbb_fundamentals(
    request: Request,
    ticker: Optional[str] = None,
    isin: Optional[str] = None,
    exchange: Optional[str] = None,
    currency: Optional[str] = None,
    provider: Optional[str] = None,
    nocache: bool = False,
    database=Depends(get_optional_database_service),
    redis_client=Depends(get_redis_client),
    providers=Depends(get_provider_registry),
    validation_layer=Depends(get_validation_layer),
    sec_edgar_provider=Depends(get_sec_edgar_provider),
) -> List[Dict[str, Any]]:
    """Return fundamentals flattened into an AgGrid table."""
    raw_data = await get_all_information(
        request=request,
        ticker=ticker,
        isin=isin,
        exchange=exchange,
        currency=currency,
        provider=provider,
        fmt="eodhd",
        nocache=nocache,
        database=database,
        redis_client=redis_client,
        providers=providers,
        validation_layer=validation_layer,
        sec_edgar_provider=sec_edgar_provider,
    )
    return format_fundamentals_table(raw_data)


@router.get("/fundamental/deep")
async def get_openbb_fundamental_deep(
    ticker: Optional[str] = None,
    isin: Optional[str] = None,
    refresh: bool = False,
    sections: str = "all",
    database=Depends(get_database_service),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Return deep fundamentals flattened into an AgGrid table."""
    raw_data = await get_fundamental_deep(
        ticker=ticker,
        isin=isin,
        refresh=refresh,
        sections=sections,
        database=database,
        cache=cache,
    )
    return format_fundamentals_deep_table(raw_data)


@router.get("/quotes")
async def get_openbb_quotes_batch(
    tickers: str,
    worker=Depends(get_realtime_worker),
    redis_client=Depends(get_redis_client),
) -> List[Dict[str, Any]]:
    """Return batch quotes as an AgGrid table."""
    batch_res = await get_quotes_batch(tickers=tickers, worker=worker, redis_client=redis_client)
    quotes_map = (
        batch_res.get("quotes", {})
        if isinstance(batch_res, dict)
        else getattr(batch_res, "quotes", batch_res)
    )
    if not isinstance(quotes_map, dict):
        quotes_map = {}
    return format_batch_quotes_table(quotes_map)


@router.get("/dcf/{ticker}")
async def get_openbb_dcf(
    ticker: str,
    force_refresh: bool = False,
    service=Depends(get_dcf_service),
    redis_client=Depends(get_redis_client),
) -> List[Dict[str, Any]]:
    """Return DCF valuation output flattened into an AgGrid table."""
    res = await get_dcf_valuation(
        ticker=ticker,
        force_refresh=force_refresh,
        service=service,
        redis_client=redis_client,
    )
    return format_dcf_table(res)


@router.get("/dcf/{ticker}/compare")
async def get_openbb_dcf_compare(
    ticker: str,
    force_refresh: bool = False,
    service=Depends(get_dcf_service),
    redis_client=Depends(get_redis_client),
) -> List[Dict[str, Any]]:
    """Return side-by-side DCF models comparison as an AgGrid table."""
    res = await compare_dcf_models(
        ticker=ticker,
        force_refresh=force_refresh,
        service=service,
        redis_client=redis_client,
    )
    return format_dcf_compare_table(res)


@router.get("/dcf/{ticker}/sensitivity")
async def get_openbb_dcf_sensitivity(
    ticker: str,
    model: str = "fcf",
    wacc_min: float = 0.06,
    wacc_max: float = 0.16,
    wacc_step: float = 0.02,
    growth_min: float = 0.01,
    growth_max: float = 0.05,
    growth_step: float = 0.01,
    force_refresh: bool = False,
    service=Depends(get_dcf_service),
    redis_client=Depends(get_redis_client),
) -> List[Dict[str, Any]]:
    """Return DCF sensitivity matrix flattened into an AgGrid table."""
    res = await get_dcf_sensitivity(
        ticker=ticker,
        model=model,  # type: ignore
        wacc_min=wacc_min,
        wacc_max=wacc_max,
        wacc_step=wacc_step,
        growth_min=growth_min,
        growth_max=growth_max,
        growth_step=growth_step,
        force_refresh=force_refresh,
        service=service,
        redis_client=redis_client,
    )
    return format_dcf_sensitivity_table(res)


@router.get("/news/feed")
async def get_openbb_news_feed(
    limit: int = Query(default=20, ge=1, le=100),
    language: Optional[str] = None,
    tickers: Optional[str] = None,
    service=Depends(get_news_service),
) -> List[Dict[str, Any]]:
    """Return news feed articles directly as an AgGrid table."""
    res = await get_news_feed(
        limit=limit,
        language=language,
        tickers=tickers,
        service=service,
    )
    articles = getattr(res, "articles", []) or []
    return [a.model_dump(mode="json") if hasattr(a, "model_dump") else a for a in articles]


@router.get("/news/{ticker}")
async def get_openbb_news(
    ticker: str,
    limit: int = Query(default=20, ge=1, le=100),
    language: Optional[str] = None,
    force_refresh: bool = False,
    service=Depends(get_news_service),
) -> List[Dict[str, Any]]:
    """Return ticker news articles directly as an AgGrid table."""
    res = await get_ticker_news(
        ticker=ticker,
        limit=limit,
        language=language,
        force_refresh=force_refresh,
        service=service,
    )
    articles = getattr(res, "articles", []) or []
    return [a.model_dump(mode="json") if hasattr(a, "model_dump") else a for a in articles]


@router.get("/insider-transactions/{ticker}")
async def get_openbb_insider_transactions(
    ticker: str,
    limit: int = 20,
    refresh: bool = False,
    provider=Depends(get_sec_edgar_provider),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Return insider transactions directly as an AgGrid table."""
    res = await get_insider_transactions(
        ticker=ticker,
        limit=limit,
        refresh=refresh,
        provider=provider,
        cache=cache,
    )
    if isinstance(res, dict):
        return res.get("transactions", [])
    return getattr(res, "transactions", []) or []


@router.get("/etf/{isin}/details")
async def get_openbb_etf_details(
    isin: str,
    refresh: bool = False,
    provider=Depends(get_justetf_provider),
    database=Depends(get_optional_database_service),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Return ETF details formatted as an AgGrid table."""
    res = await get_etf_details(
        isin=isin,
        refresh=refresh,
        provider=provider,
        database=database,
        cache=cache,
    )
    return format_etf_details_table(res)


@router.get("/index/{index_name}/constituents")
async def get_openbb_index_constituents(
    index_name: str,
    refresh: bool = False,
    provider=Depends(get_index_provider),
    index_name_enum=Depends(get_index_name_enum),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Return index constituents directly as an AgGrid table."""
    res = await get_index_constituents(
        index_name=index_name,
        refresh=refresh,
        provider=provider,
        index_name_enum=index_name_enum,
        cache=cache,
    )
    if isinstance(res, dict):
        return res.get("constituents", [])
    return getattr(res, "constituents", []) or []


@router.get("/dividends/{ticker}")
async def get_openbb_dividends(
    ticker: str,
    limit: int = 50,
    refresh: bool = False,
    provider=Depends(get_dividend_provider),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Return historical dividend payments formatted as an OpenBB AgGrid table."""
    clean_ticker = (ticker or "").strip()
    if clean_ticker.startswith("{") or clean_ticker.lower() in ("", "undefined", "none"):
        clean_ticker = "AAPL"
    records = await provider.get_dividends(
        ticker=clean_ticker, limit=limit, refresh=refresh, cache=cache
    )
    return format_dividends_table(records)


@router.get("/calendar/dividends")
async def get_openbb_calendar_dividends_query(
    ticker: str = "AAPL",
    limit: int = 50,
    refresh: bool = False,
    provider=Depends(get_dividend_provider),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Fallback query-parameter route for OpenBB test requests."""
    return await get_openbb_dividends(
        ticker=ticker, limit=limit, refresh=refresh, provider=provider, cache=cache
    )


@router.get("/earnings/{ticker}")
async def get_openbb_earnings(
    ticker: str,
    limit: int = 50,
    refresh: bool = False,
    provider=Depends(get_earnings_provider),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Return historical and upcoming earnings data formatted as an OpenBB AgGrid table."""
    clean_ticker = (ticker or "").strip()
    if clean_ticker.startswith("{") or clean_ticker.lower() in ("", "undefined", "none"):
        clean_ticker = "AAPL"
    records = await provider.get_earnings_history(
        ticker=clean_ticker, limit=limit, refresh=refresh, cache=cache
    )
    return format_earnings_history_table(records)


@router.get("/calendar/earnings")
async def get_openbb_calendar_earnings_query(
    ticker: str = "AAPL",
    limit: int = 50,
    refresh: bool = False,
    provider=Depends(get_earnings_provider),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Fallback query-parameter route for OpenBB test requests."""
    return await get_openbb_earnings(
        ticker=ticker, limit=limit, refresh=refresh, provider=provider, cache=cache
    )


@router.get("/splits/{ticker}")
async def get_openbb_splits(
    ticker: str,
    limit: int = 50,
    refresh: bool = False,
    provider=Depends(get_split_provider),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Return historical stock splits formatted as an OpenBB AgGrid table."""
    clean_ticker = (ticker or "").strip()
    if clean_ticker.startswith("{") or clean_ticker.lower() in ("", "undefined", "none"):
        clean_ticker = "AAPL"
    records = await provider.get_stock_splits(
        ticker=clean_ticker, limit=limit, refresh=refresh, cache=cache
    )
    return format_stock_splits_table(records)


@router.get("/calendar/splits")
async def get_openbb_calendar_splits_query(
    ticker: str = "AAPL",
    limit: int = 50,
    refresh: bool = False,
    provider=Depends(get_split_provider),
    cache=Depends(get_cache_service),
) -> List[Dict[str, Any]]:
    """Fallback query-parameter route for OpenBB test requests."""
    return await get_openbb_splits(
        ticker=ticker, limit=limit, refresh=refresh, provider=provider, cache=cache
    )

