"""FastAPI router providing OpenBB Workspace adapter endpoints.

Adapts Fonrex domain responses to the schema contracts required by OpenBB Workspace widgets:
- type: "metric"  -> [ { "label": ..., "value": ..., "delta": ... }, ... ]
- type: "chart"   -> Plotly Figure JSON { "data": [...], "layout": {...} }
- type: "table"   -> Flat list of row records [ { ... }, ... ]
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import JSONResponse

from database.query import QueryService
from historical.ingestion_service import HistoricalIngestionService
from integrations.openbb.adapters import (
    format_batch_quotes_table,
    format_candlestick_chart,
    format_cot_extremes_table,
    format_cot_signals_chart,
    format_dcf_compare_table,
    format_dcf_sensitivity_table,
    format_dcf_table,
    format_esef_check_table,
    format_etf_details_table,
    format_factor_exposure_table,
    format_factor_returns_chart,
    format_fundamentals_deep_table,
    format_fundamentals_table,
    format_indicator_chart,
    format_macro_indicator_chart,
    format_macro_indicators_metric,
    format_macro_rates_metric,
    format_quote_metric,
    format_sec_check_table,
    format_technical_chart_overlay,
    format_technical_multi_chart,
    format_treasury_curve_chart,
    format_treasury_series_chart,
)
from routers.assets import get_eod
from routers.cot import get_cot_extremes, get_cot_reports, get_cot_signals
from routers.dependencies import (
    get_cache_service,
    get_database_service,
    get_ingestion_service,
    get_query_service,
    get_redis_client,
    get_technical_service,
)
from routers.esef import check_statements_against_esef, get_esef_check, get_esef_statements
from routers.factors import (
    get_factor_exposure,
    get_factor_exposure_of_listing,
    get_factor_library,
    get_factor_series,
)
from routers.fundamentals import (
    get_all_information,
    get_fundamental_deep,
    get_optional_database_service,
    get_provider_registry,
    get_sec_edgar_provider,
    get_validation_layer,
)
from routers.historical import ISIN_QUERY_PATTERN, get_ticker_history
from routers.macro import (
    get_eurostat_service,
    get_macro_indicator_series,
    get_macro_indicators,
    get_macro_rates,
    get_treasury_curve,
    get_treasury_curves,
    get_treasury_series,
)
from routers.news import (
    NEWS_DEFAULT_LIMIT,
    NEWS_MAX_LIMIT,
    get_news_feed,
    get_news_service,
    get_ticker_news,
)
from routers.realtime import (
    get_quote,
    get_quotes_batch,
    get_realtime_worker,
)
from routers.sec import check_statements_against_sec, get_sec_check, get_sec_statements
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
from schemas.technical import TECHNICAL_DEFAULT_LIMIT

router = APIRouter(prefix="/openbb", tags=["OpenBB Adapters"])


# ──────────────────────────────────────────────────────────────────────────────
# Metric Endpoints (type: "metric")
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/quote/{ticker}")
async def get_openbb_quote(
    ticker: str,
    request: Request,
    worker=Depends(get_realtime_worker),
) -> List[Dict[str, Any]]:
    """Return quote snapshot formatted as OpenBB metric tiles.

    Like ``GET /quote``, it changes nothing (AGENTS.md, rule 7), whatever the key:
    the realtime stream of a ticker is started by ``POST /realtime/subscribe``.
    """
    quote_snapshot = await get_quote(
        ticker=ticker, request=request, subscribe_if_missing=False, worker=worker
    )
    return format_quote_metric(quote_snapshot)


@router.get("/macro/rates")
async def get_openbb_macro_rates(
    request: Request,
    currency: Optional[str] = Query(None, pattern="^([A-Za-z]{3})?$"),
) -> List[Dict[str, Any]]:
    """Return current macro rates (USD and EUR by default) formatted as OpenBB metric tiles."""
    rates_res = await get_macro_rates(request=request, currency=currency)
    return format_macro_rates_metric(rates_res)


@router.get("/macro/indicators")
async def get_openbb_macro_indicators(
    geo: str = Query("EA", description="Country or area: EA (euro area), DE, FR..."),
    service=Depends(get_eurostat_service),
) -> List[Dict[str, Any]]:
    """Return the latest Eurostat indicators of a country as OpenBB metric tiles."""
    res = await get_macro_indicators(geo=geo, indicator=None, service=service)
    return format_macro_indicators_metric(res)


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
    currency: Optional[str] = Query(None, max_length=10),
    exchange: Optional[str] = Query(None, max_length=50),
    isin: Optional[str] = Query(None, description="ISIN of the instrument"),
    query_service: QueryService = Depends(get_query_service),
    ingestion_service: HistoricalIngestionService = Depends(get_ingestion_service),
    cache=Depends(get_cache_service),
) -> Dict[str, Any]:
    """Return EOD price history as a Plotly Candlestick chart."""
    # The route function is called directly: every parameter is passed, a
    # missing one would receive its `Query(...)` declaration instead of a value.
    response = await get_eod(
        ticker=ticker,
        request=request,
        period=period,
        fmt="json",
        order=order,
        from_date=from_date,
        to_date=to_date,
        currency=currency,
        exchange=exchange,
        isin=isin,
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
    currency: Optional[str] = Query(None, max_length=10),
    exchange: Optional[str] = Query(None, max_length=50),
    isin: Optional[str] = Query(
        None, pattern=ISIN_QUERY_PATTERN, description="ISIN of the instrument"
    ),
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
        currency=currency,
        exchange=exchange,
        isin=isin,
        query_service=query_service,
        redis_client=redis_client,
        cache_service=cache_service,
    )
    records = res.get("data", [])
    return format_candlestick_chart(
        symbol.upper(), records, title=f"{symbol.upper()} History ({interval})"
    )


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
    limit: int = TECHNICAL_DEFAULT_LIMIT,
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
    limit: int = TECHNICAL_DEFAULT_LIMIT,
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
    quotes_map = (
        batch_res.get("quotes", {})
        if isinstance(batch_res, dict)
        else getattr(batch_res, "quotes", batch_res)
    )
    if not isinstance(quotes_map, dict):
        quotes_map = {}
    return format_batch_quotes_table(quotes_map)


@router.get("/factors/exposure/{ticker}")
async def get_openbb_factor_exposure(
    ticker: str,
    model: Literal["ff3", "ff5", "carhart"] = "ff3",
    frequency: Literal["monthly", "daily"] = "monthly",
    window: Optional[int] = Query(None, ge=24, le=10000),
    exposure=Depends(get_factor_exposure),
) -> List[Dict[str, Any]]:
    """Return the factor exposure of a listing as an AgGrid table."""
    res = await get_factor_exposure_of_listing(
        ticker=ticker,
        model=model,
        frequency=frequency,
        window=window,
        end=None,
        region=None,
        currency=None,
        exchange=None,
        isin=None,
        exposure=exposure,
    )
    return format_factor_exposure_table(res)


@router.get("/sec/{ticker}/check")
async def get_openbb_sec_check(
    ticker: str,
    years: Optional[int] = Query(3, ge=1, le=30),
    statements=Depends(get_sec_statements),
    checker=Depends(get_sec_check),
) -> List[Dict[str, Any]]:
    """Return the check of the Yahoo statements against the SEC filings as an AgGrid table."""
    res = await check_statements_against_sec(
        ticker=ticker, years=years, isin=None, statements=statements, checker=checker
    )
    return format_sec_check_table(res)


@router.get("/esef/{ticker}/check")
async def get_openbb_esef_check(
    ticker: str,
    years: Optional[int] = Query(3, ge=1, le=30),
    statements=Depends(get_esef_statements),
    checker=Depends(get_esef_check),
) -> List[Dict[str, Any]]:
    """Return the check of the Yahoo statements against the ESEF filings as an AgGrid table."""
    res = await check_statements_against_esef(
        ticker=ticker, years=years, isin=None, statements=statements, checker=checker
    )
    return format_esef_check_table(res)


@router.get("/factors/{dataset}/chart")
async def get_openbb_factor_returns_chart(
    dataset: str,
    frequency: Literal["monthly", "daily"] = "monthly",
    start: Optional[date] = None,
    library=Depends(get_factor_library),
) -> Dict[str, Any]:
    """Return the cumulative returns of the factors of a dataset as a Plotly chart (10 years by default)."""
    if start is None:
        today = date.today()
        start = today.replace(year=today.year - 10, day=1)
    res = await get_factor_series(
        dataset=dataset, frequency=frequency, start=start, end=None, library=library
    )
    return format_factor_returns_chart(res)


def _widget_date(value: Optional[str], name: str) -> Optional[date]:
    """A date field of a widget: OpenBB sends an empty field as ``name=``, which is no date."""
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"{name} is not a date YYYY-MM-DD: {value!r}"
        ) from None


@router.get("/macro/indicators/{indicator}/chart")
async def get_openbb_macro_indicator_chart(
    indicator: str,
    geo: str = Query("EA,DE,FR,IT,ES", description="Countries or areas, separated by commas"),
    start: Optional[str] = Query(None, description="First period (10 years ago when empty)"),
    service=Depends(get_eurostat_service),
) -> Dict[str, Any]:
    """Return one Eurostat indicator of several countries as a Plotly chart (10 years by default)."""
    start = _widget_date(start, "start")
    if start is None:
        today = date.today()
        start = today.replace(year=today.year - 10, month=1, day=1)
    res = await get_macro_indicator_series(
        indicator=indicator, geo=geo, start=start, end=None, service=service
    )
    return format_macro_indicator_chart(res)


@router.get("/macro/treasury/curve/chart")
async def get_openbb_treasury_curve_chart(
    curve: str = Query("nominal", pattern="^(nominal|real)$"),
    on: Optional[str] = Query(
        None, alias="date", description="Day of the curve (latest when empty)"
    ),
    compare: str = Query(
        "1M,1Y", description="Days to compare with, separated by commas: 1W, 1M, 1Y or dates"
    ),
    service=Depends(get_treasury_curves),
) -> Dict[str, Any]:
    """Return the Treasury yield curve of a day, and of the days compared, as a Plotly chart."""
    wanted = [part.strip() for part in compare.split(",") if part.strip()]
    res = await get_treasury_curve(
        curve=curve, on=_widget_date(on, "date"), compare=wanted, service=service
    )
    return format_treasury_curve_chart(res)


@router.get("/macro/treasury/series/chart")
async def get_openbb_treasury_series_chart(
    tenor: str = Query(
        "10Y-2Y", description="Maturities (10Y) or spreads (10Y-2Y), separated by commas"
    ),
    curve: str = Query("nominal", pattern="^(nominal|real)$"),
    start: Optional[str] = Query(None, description="First day (10 years ago when empty)"),
    service=Depends(get_treasury_curves),
) -> Dict[str, Any]:
    """Return Treasury maturities or spreads over time as a Plotly chart (10 years by default)."""
    res = await get_treasury_series(
        tenor=[tenor], curve=curve, start=_widget_date(start, "start"), end=None, service=service
    )
    return format_treasury_series_chart(res)


@router.get("/cot/signals/chart")
async def get_openbb_cot_signals_chart(
    market: str = Query("13874A", description="CFTC contract market code"),
    report: str = Query("tff", pattern="^(legacy|tff|disaggregated)$"),
    category: Optional[str] = Query(
        None, description="Type of trader (the speculators of the report when empty)"
    ),
    window: int = Query(156, ge=1, le=520, description="Weeks of the COT index window"),
    start: Optional[str] = Query(None, description="First report date (3 years ago when empty)"),
    service=Depends(get_cot_reports),
) -> Dict[str, Any]:
    """Return the net position and the COT index of one market as a Plotly chart."""
    res = await get_cot_signals(
        market=market,
        report=report,
        category=category.strip() if category and category.strip() else None,
        window=window,
        start=_widget_date(start, "start"),
        end=None,
        service=service,
    )
    return format_cot_signals_chart(res)


@router.get("/cot/extremes")
async def get_openbb_cot_extremes(
    report: str = Query("tff", pattern="^(legacy|tff|disaggregated)$"),
    market: Optional[str] = Query(
        None, description="CFTC contract market codes, separated by commas (key markets when empty)"
    ),
    category: Optional[str] = Query(
        None, description="Type of trader (the speculators of the report when empty)"
    ),
    window: int = Query(156, ge=1, le=520, description="Weeks of the COT index window"),
    service=Depends(get_cot_reports),
) -> List[Dict[str, Any]]:
    """Return the latest positioning of several markets as an AgGrid table, most extreme first."""
    res = await get_cot_extremes(
        report=report,
        market=[market] if market and market.strip() else None,
        category=category.strip() if category and category.strip() else None,
        window=window,
        service=service,
    )
    return format_cot_extremes_table(res)


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
    limit: int = Query(default=NEWS_DEFAULT_LIMIT, ge=1, le=NEWS_MAX_LIMIT),
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
    limit: int = Query(default=NEWS_DEFAULT_LIMIT, ge=1, le=NEWS_MAX_LIMIT),
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
