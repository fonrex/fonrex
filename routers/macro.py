"""HTTP routes for macro-economic data.

- ``/macro/rates``: the rates of FRED (USD) and of the ECB (EUR).
- ``/macro/indicators``: the indicators of Eurostat (inflation, unemployment, GDP
  growth, surveys) by country, stored period by period and read again from
  Eurostat when they are older than ``EUROSTAT_REFRESH_HOURS``.
- ``/macro/treasury``: the daily yield curves of the US Treasury (par curve since
  1990, real curve since 2003), stored day by day; a year is read from the
  Treasury only when a request needs it.
"""

import asyncio
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from financials.providers.treasury import CURVES, TENORS_BY_CODE
from macro.eurostat_service import (
    INDICATORS,
    EurostatIndicator,
    EurostatService,
    SeriesLoad,
    normalize_geo,
)
from macro.treasury_curves import CurveLoad, StoredDay, TreasuryCurves
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
    TreasuryChange,
    TreasuryComparison,
    TreasuryCurveResponse,
    TreasuryLoad,
    TreasuryObservation,
    TreasuryPoint,
    TreasuryRefreshItem,
    TreasuryRefreshResponse,
    TreasurySeries,
    TreasurySeriesResponse,
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


# ── US Treasury yield curves ─────────────────────────────────────────────────

# Maturities or spreads of one request, and dates compared with the curve of a day.
MAX_TREASURY_SERIES = 6
MAX_COMPARISONS = 5
# Years of history given by GET /macro/treasury/series when ``start`` is left out.
DEFAULT_SERIES_YEARS = 10
_OFFSET = re.compile(r"^(\d{1,3})([DWMY])$")

CurveParameter = Query(
    "nominal",
    pattern="^(nominal|real)$",
    description="nominal (par yield curve, since 1990) or real (TIPS, since 2003)",
)


def get_treasury_curves(request: Request) -> TreasuryCurves:
    service = getattr(request.app.state, "treasury_curves", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="Treasury yield curves unavailable (no database)"
        )
    return service


def _treasury_load(loads: List[CurveLoad]) -> TreasuryLoad:
    """One load for the readings of a request: failed if one failed, fetched if one read."""
    statuses = {load.status for load in loads}
    status = next(s for s in ("failed", "fetched", "fresh") if s in statuses or s == "fresh")
    readings = [load.fetched_at for load in loads if load.fetched_at]
    last = loads[-1]
    return TreasuryLoad(
        status=status,
        fetched_at=max(readings, default=None),
        first_day=last.first_day,
        last_day=last.last_day,
        years_read=sorted({year for load in loads for year in load.years_read}),
        years_failed=sorted({year for load in loads for year in load.years_failed}),
        reason=next((load.reason for load in loads if load.reason), None),
    )


async def _read_years(service: TreasuryCurves, curve: str, days: List[date]) -> List[CurveLoad]:
    """Read the years of some days that need it. A day of early January may give the last
    business day of the year before: that year is read too."""
    years = set()
    for day in days:
        years.add(day.year)
        if day.month == 1 and day.day <= 10:
            years.add(day.year - 1)
    return [await service.refresh(curve, year, year) for year in sorted(years)]


def _points(day: StoredDay) -> List[TreasuryPoint]:
    return [
        TreasuryPoint(tenor=tenor, months=float(TENORS_BY_CODE[tenor].months), value=float(value))
        for tenor, value in day.points.items()
    ]


def _before(day: date, requested: str) -> date:
    """The day of a comparison: a date, or an offset before ``day`` (1W, 1M, 3M, 1Y)."""
    match = _OFFSET.match(requested.strip().upper())
    if match is None:
        try:
            return date.fromisoformat(requested.strip())
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"compare takes a date YYYY-MM-DD or 1W, 1M, 3M, 1Y...: {requested!r}",
            ) from None
    count, unit = int(match.group(1)), match.group(2)
    if unit == "D":
        return day - timedelta(days=count)
    if unit == "W":
        return day - timedelta(weeks=count)
    months = count * (12 if unit == "Y" else 1)
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    for last in (31, 30, 29, 28):  # 31 March minus a month is the last day of February
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise AssertionError("unreachable")  # pragma: no cover


def _nothing_stored(curve: str, requested: Optional[date], load: TreasuryLoad) -> HTTPException:
    if load.status == "failed" and load.first_day is None:
        return HTTPException(
            status_code=502, detail=f"The Treasury could not be read: {load.reason}"
        )
    first_year = CURVES[curve].first_year
    when = f"on or before {requested}" if requested else "stored"
    return HTTPException(
        status_code=404,
        detail=f"No {curve} Treasury curve {when} (the curve starts in {first_year})",
    )


@router.get("/treasury/curve", response_model=TreasuryCurveResponse)
async def get_treasury_curve(
    curve: str = CurveParameter,
    on: Optional[date] = Query(
        None,
        alias="date",
        description="Day of the curve: the business day on or before it (the latest when empty)",
    ),
    compare: Optional[List[str]] = Query(
        None,
        description=(
            "Curves to compare with, repeated: a date, or an offset before the day "
            "(1W, 1M, 3M, 1Y); at most 5"
        ),
    ),
    service: TreasuryCurves = Depends(get_treasury_curves),
) -> TreasuryCurveResponse:
    """The yield curve of one day, shortest maturity first, with the breakeven inflation.

    Each comparison gives the curve of another day and, for each maturity, the
    change from it to the day asked. A year missing, or the current year read
    more than ``TREASURY_REFRESH_HOURS`` hours ago, is read from the Treasury
    first; when that fails, the stored rates are answered and ``load.status`` is
    ``failed``.
    """
    wanted = list(dict.fromkeys(compare or []))
    if len(wanted) > MAX_COMPARISONS:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_COMPARISONS} curves to compare with"
        )
    other = "real" if curve == "nominal" else "nominal"
    day_asked = on or service.today()
    loads = await _read_years(service, curve, [day_asked])
    stored = await service.curve_on(curve, on)
    if stored is None:
        raise _nothing_stored(curve, on, _treasury_load(loads))

    comparisons = []
    if wanted:
        days = {requested: _before(stored.day, requested) for requested in wanted}
        loads += await _read_years(service, curve, list(days.values()))
        for requested, day in days.items():
            past = await service.curve_on(curve, day)
            comparisons.append(
                TreasuryComparison(
                    requested=requested,
                    date=past.day if past else None,
                    points=[
                        TreasuryChange(
                            **point.model_dump(),
                            change=(
                                float(stored.points[point.tenor] - past.points[point.tenor])
                                if point.tenor in stored.points
                                else None
                            ),
                        )
                        for point in _points(past)
                    ]
                    if past
                    else [],
                )
            )

    await _read_years(service, other, [stored.day])
    other_day = await service.curve_on(other, stored.day)
    breakeven = []
    if other_day is not None and other_day.day == stored.day:
        nominal, real = (stored, other_day) if curve == "nominal" else (other_day, stored)
        breakeven = [
            TreasuryPoint(
                tenor=tenor,
                months=float(TENORS_BY_CODE[tenor].months),
                value=float(nominal.points[tenor] - value),
            )
            for tenor, value in real.points.items()
            if tenor in nominal.points
        ]
    return TreasuryCurveResponse(
        curve=curve,
        label=CURVES[curve].label,
        requested_date=on,
        date=stored.day,
        points=_points(stored),
        comparisons=comparisons,
        breakeven=breakeven,
        load=_treasury_load(loads),
    )


def _series_names(curve: str, raw: List[str]) -> List[str]:
    """Maturities (10Y) or spreads (10Y-2Y) of a request, in order, without repeats."""
    names = []
    for piece in raw:
        names += [name.strip().upper() for name in piece.split(",") if name.strip()]
    names = list(dict.fromkeys(names))
    if not names:
        raise HTTPException(status_code=422, detail="tenor names no maturity")
    if len(names) > MAX_TREASURY_SERIES:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_TREASURY_SERIES} maturities or spreads"
        )
    known = CURVES[curve].tenors
    for name in names:
        for tenor in name.split("-"):
            if tenor not in known or name.count("-") > 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{name!r} is not a maturity of the {curve} curve nor a spread of two "
                        f"(10Y-2Y): {', '.join(known)}"
                    ),
                )
    return names


@router.get("/treasury/series", response_model=TreasurySeriesResponse)
async def get_treasury_series(
    tenor: List[str] = Query(
        ["10Y"],
        description=(
            "Maturities (10Y) or spreads of two maturities (10Y-2Y), repeated or separated "
            "by commas; at most 6"
        ),
    ),
    curve: str = CurveParameter,
    start: Optional[date] = Query(
        None, description=f"First day included ({DEFAULT_SERIES_YEARS} years before end by default)"
    ),
    end: Optional[date] = Query(None, description="Last day included (today by default)"),
    service: TreasuryCurves = Depends(get_treasury_curves),
) -> TreasurySeriesResponse:
    """Maturities of a curve and spreads between two of them, day by day, oldest first.

    A spread is given only on the days both maturities are published. The years
    of the period are read from the Treasury first when needed, as for
    ``GET /macro/treasury/curve``.
    """
    names = _series_names(curve, tenor)
    end = end or service.today()
    start = start or _before(end, f"{DEFAULT_SERIES_YEARS}Y")
    if start > end:
        raise HTTPException(status_code=422, detail="start is after end")
    load = await service.refresh(curve, start.year, end.year)
    tenors = list(dict.fromkeys(t for name in names for t in name.split("-")))
    history = await service.history(curve, tenors, start, end)

    def observations(name: str) -> List[TreasuryObservation]:
        if "-" not in name:
            return [TreasuryObservation(date=d, value=float(v)) for d, v in history[name]]
        long, short = name.split("-")
        shorts: Dict[date, Decimal] = dict(history[short])
        return [
            TreasuryObservation(date=d, value=float(v - shorts[d]))
            for d, v in history[long]
            if d in shorts
        ]

    result = _treasury_load([load])
    if result.status == "failed" and result.first_day is None:
        raise _nothing_stored(curve, None, result)
    return TreasurySeriesResponse(
        curve=curve,
        label=CURVES[curve].label,
        start=start,
        end=end,
        series=[TreasurySeries(tenor=name, observations=observations(name)) for name in names],
        load=result,
    )


@router.post("/treasury/refresh", response_model=TreasuryRefreshResponse)
async def refresh_treasury_curves(
    curve: Optional[List[str]] = Query(
        None, description="nominal, real, or both when left out (repeated)"
    ),
    start_year: Optional[int] = Query(
        None, ge=1990, le=2100, description="First year read (the current year by default)"
    ),
    end_year: Optional[int] = Query(
        None, ge=1990, le=2100, description="Last year read (the current year by default)"
    ),
    force: bool = Query(False, description="Read even the years that need no reading"),
    service: TreasuryCurves = Depends(get_treasury_curves),
) -> TreasuryRefreshResponse:
    """Read years of the Treasury curves into the database.

    A past year read after its end, and the current year read less than
    ``TREASURY_REFRESH_HOURS`` hours ago, are skipped unless ``force`` is set. The
    whole history is ``start_year=1990``: 37 files of a few kilobytes.
    """
    names = list(dict.fromkeys(name.strip().lower() for name in curve or CURVES))
    unknown = [name for name in names if name not in CURVES]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unknown curve {unknown[0]!r}: {', '.join(CURVES)}"
        )
    current = service.today().year
    first, last = start_year or current, end_year or current
    if first > last:
        raise HTTPException(status_code=422, detail="start_year is after end_year")
    loads = []
    for name in names:
        load = _treasury_load([await service.refresh(name, first, last, force=force)])
        loads.append(TreasuryRefreshItem(curve=name, **load.model_dump()))
    return TreasuryRefreshResponse(loads=loads)
