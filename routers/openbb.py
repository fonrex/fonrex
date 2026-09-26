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

from database.query import QueryService
from historical.ingestion_service import HistoricalIngestionService
from integrations.openbb.adapters import (
    format_batch_quotes_table,
    format_candlestick_chart,
    format_dcf_compare_table,
    format_dcf_sensitivity_table,
    format_dcf_table,
    format_etf_details_table,
    format_fundamentals_deep_table,
    format_fundamentals_table,
    format_indicator_chart,
    format_macro_rates_metric,
    format_quote_metric,
    format_technical_chart_overlay,
    format_technical_multi_chart,
)
from routers.assets import get_eod
from routers.dependencies import (
    get_cache_service,
    get_database_service,
    get_ingestion_service,
    get_query_service,
    get_redis_client,
    get_technical_service,
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
    return format_batch_quotes_table(batch_res)


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
