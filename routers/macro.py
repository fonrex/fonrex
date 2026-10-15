"""HTTP routes for macro-economic data.

- ``/macro/rates``: the rates of FRED (USD) and of the ECB (EUR).
- ``/macro/indicators``: the indicators of Eurostat (inflation, unemployment, GDP
  growth, surveys) by country, stored period by period and read again from
  Eurostat when they are older than ``EUROSTAT_REFRESH_HOURS``.
"""

import asyncio
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from macro.eurostat_service import (
    INDICATORS,
    EurostatIndicator,
    EurostatService,
    SeriesLoad,
    normalize_geo,
)
from schemas.macro import (
    IndicatorGeoSeries,
    IndicatorLoad,
    IndicatorObservation,
    IndicatorRefreshItem,
    IndicatorRefreshResponse,
    IndicatorSeriesResponse,
    IndicatorsResponse,
    IndicatorValue,
    MacroRatesResponse,
)

router = APIRouter(prefix="/macro", tags=["Macro"])

# Currency -> service of app.state that publishes its rates.
MACRO_SOURCES = {"USD": "fred_service", "EUR": "ecb_service"}


def _service(request: Request, currency: str):
    return getattr(request.app.state, MACRO_SOURCES[currency], None)


@router.get("/rates", response_model=MacroRatesResponse)
async def get_macro_rates(
    request: Request,
    currency: Optional[str] = Query(
        None,
        pattern="^([A-Za-z]{3})?$",
        description="USD (FRED) or EUR (ECB); every currency when left out.",
    ),
) -> MacroRatesResponse:
    """Current macro-economic rates: the 10-year risk-free rate of each currency, and more.

    Without ``currency`` the answer holds the series of every source, and
    ``risk_free_rate`` is the US one, as before the ECB was a source.
    """
    if not currency:  # left out, or empty (the "both" choice of the OpenBB widget)
        currency = None
        currencies = list(MACRO_SOURCES)
    else:
        currency = currency.upper()
        if currency not in MACRO_SOURCES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No source of macro rates for {currency}: "
                    f"{', '.join(sorted(MACRO_SOURCES))} are known."
                ),
            )
        currencies = [currency]

    services = {code: _service(request, code) for code in currencies}
    available = {code: service for code, service in services.items() if service is not None}
    if not available:
        names = ", ".join(MACRO_SOURCES[code] for code in currencies)
        raise HTTPException(
            status_code=503, detail=f"Macro rates unavailable ({names} not started)"
        )

    results = await asyncio.gather(*(service.get_rates() for service in available.values()))
    answers = {
        code: MacroRatesResponse.model_validate(result)
        for code, result in zip(available, results, strict=True)
    }
    first = currencies[0]
    return MacroRatesResponse(
        currency=currency,
        risk_free_rate=answers[first].risk_free_rate if first in answers else None,
        rates=[rate for code in currencies if code in answers for rate in answers[code].rates],
    )


# ── Eurostat indicators ──────────────────────────────────────────────────────

# Countries or areas of one request: each one is a series read from Eurostat.
MAX_GEOS = 10


def get_eurostat_service(request: Request) -> EurostatService:
    service = getattr(request.app.state, "eurostat_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Eurostat indicators unavailable (no database)")
    return service


def _known_indicator(name: str) -> EurostatIndicator:
    item = INDICATORS.get(name.strip().lower())
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown indicator {name!r}: {', '.join(INDICATORS)}",
        )
    return item


def _geos(raw: str) -> List[str]:
    """Countries or areas of a comma-separated list, in order, without repeats."""
    names = [part for part in (piece.strip() for piece in raw.split(",")) if part]
    if not names:
        raise HTTPException(status_code=422, detail="geo names no country or area")
    try:
        codes = list(dict.fromkeys(normalize_geo(name) for name in names))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if len(codes) > MAX_GEOS:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_GEOS} countries or areas per request"
        )
    return codes


def _load(load: SeriesLoad) -> IndicatorLoad:
    return IndicatorLoad(
        status=load.status,
        fetched_at=load.fetched_at,
        first_period=load.first_period,
        last_period=load.last_period,
        reason=load.reason,
    )


GeoParameter = Query(
    "EA",
    description=(
        "Country or area as Eurostat names it: EA (the euro area, read as EA21), "
        "EU27_2020, DE, FR, IT, ES, NL...; several separated by commas for a series"
    ),
)


@router.get("/indicators", response_model=IndicatorsResponse)
async def get_macro_indicators(
    geo: str = Query("EA", description="Country or area: EA (euro area), EU27_2020, DE, FR..."),
    indicator: Optional[List[str]] = Query(
        None, description="Indicators to give (every indicator when left out)"
    ),
    service: EurostatService = Depends(get_eurostat_service),
) -> IndicatorsResponse:
    """The latest value of each indicator of Eurostat for one country or area.

    Each value comes with the previous period and the change. A series never
    read, or read more than ``EUROSTAT_REFRESH_HOURS`` hours ago, is read from
    Eurostat first; when that fails, the stored values are answered and
    ``load.status`` is ``failed``.
    """
    codes = _geos(geo)
    if len(codes) != 1:
        raise HTTPException(status_code=422, detail="geo names one country or area here")
    code = codes[0]
    items = (
        list(dict.fromkeys(_known_indicator(name) for name in indicator))
        if indicator
        else list(INDICATORS.values())
    )

    async def one(item: EurostatIndicator) -> IndicatorValue:
        load = await service.refresh(item.name, code)
        latest = await service.latest(item.name, code)
        current = latest[0] if latest else None
        previous = latest[1] if len(latest) > 1 else None
        return IndicatorValue(
            indicator=item.name,
            label=item.label,
            frequency=item.frequency,
            unit=item.unit,
            period=current.period if current else None,
            value=float(current.value) if current else None,
            status=current.status if current else None,
            previous_period=previous.period if previous else None,
            previous_value=float(previous.value) if previous else None,
            change=float(current.value - previous.value) if current and previous else None,
            load=_load(load),
        )

    values = await asyncio.gather(*(one(item) for item in items))
    return IndicatorsResponse(geo=code, indicators=list(values))


@router.post("/indicators/refresh", response_model=IndicatorRefreshResponse)
async def refresh_macro_indicators(
    geo: str = GeoParameter,
    indicator: Optional[List[str]] = Query(
        None, description="Indicators to read (every indicator when left out)"
    ),
    force: bool = Query(False, description="Read even a series read recently"),
    service: EurostatService = Depends(get_eurostat_service),
) -> IndicatorRefreshResponse:
    """Read series of Eurostat into the database.

    A series read less than ``EUROSTAT_REFRESH_HOURS`` hours ago is skipped
    unless ``force`` is set; a failed reading keeps what was stored and says why.
    """
    codes = _geos(geo)
    items = (
        list(dict.fromkeys(_known_indicator(name) for name in indicator))
        if indicator
        else list(INDICATORS.values())
    )
    loads = []
    for item in items:
        for code in codes:
            load = await service.refresh(item.name, code, force=force)
            loads.append(
                IndicatorRefreshItem(
                    indicator=item.name,
                    geo=code,
                    received=load.received,
                    **_load(load).model_dump(),
                )
            )
    return IndicatorRefreshResponse(loads=loads)


@router.get("/indicators/{indicator}", response_model=IndicatorSeriesResponse)
async def get_macro_indicator_series(
    indicator: str,
    geo: str = GeoParameter,
    start: Optional[date] = Query(None, description="First period included"),
    end: Optional[date] = Query(None, description="Last period included"),
    service: EurostatService = Depends(get_eurostat_service),
) -> IndicatorSeriesResponse:
    """The values of one indicator over time, one series per country or area.

    A period is dated by its first day (``2026-04-01`` for the second quarter);
    a rate is a ratio. Series are read from Eurostat first when needed, as for
    ``GET /macro/indicators``.
    """
    item = _known_indicator(indicator)
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="start is after end")
    codes = _geos(geo)

    async def one(code: str) -> IndicatorGeoSeries:
        load = await service.refresh(item.name, code)
        observations = await service.observations(item.name, code, start, end)
        return IndicatorGeoSeries(
            geo=code,
            load=_load(load),
            observations=[
                IndicatorObservation(period=o.period, value=float(o.value), status=o.status)
                for o in observations
            ],
        )

    series = await asyncio.gather(*(one(code) for code in codes))
    return IndicatorSeriesResponse(
        indicator=item.name,
        label=item.label,
        frequency=item.frequency,
        unit=item.unit,
        series=list(series),
    )
