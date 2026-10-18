"""Routes of the Commitments of Traders reports of the CFTC (``/cot``).

The reports are kept by ``macro/cot_reports.py``: a year is read from the CFTC
only when a request needs it (see that module). A market is its CFTC contract
market code (``13874A`` for the E-mini S&P 500), listed by ``GET /cot/markets``.
"""

import re
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from financials.providers.cftc import REPORTS
from macro.cot_reports import (
    DEFAULT_WINDOW_WEEKS,
    KEY_MARKETS,
    CotReports,
    ReportLoad,
    Signal,
    StoredWeek,
    category_of,
    published_on,
    signals,
)
from schemas.cot import (
    CotCategory,
    CotExtreme,
    CotExtremesResponse,
    CotLoad,
    CotMarket,
    CotMarketsResponse,
    CotPosition,
    CotPositionsResponse,
    CotRefreshResponse,
    CotSignal,
    CotSignalsResponse,
    CotWeek,
)

router = APIRouter(prefix="/cot", tags=["Commitments of Traders"])

# Years of reports given when ``start`` is left out.
DEFAULT_YEARS = 3
MAX_WINDOW_WEEKS = 520
MAX_MARKETS = 20
_CODE = re.compile(r"^[0-9A-Z+]{6}$")

ReportParameter = Query(
    "legacy",
    pattern="^(legacy|tff|disaggregated)$",
    description=(
        "legacy (every market since 1986), tff (financial markets since 2006) or "
        "disaggregated (commodities since 2006)"
    ),
)
WindowParameter = Query(
    DEFAULT_WINDOW_WEEKS,
    ge=1,
    le=MAX_WINDOW_WEEKS,
    description="Weeks of the COT index window (156: three years)",
)
CategoryParameter = Query(
    None,
    description=(
        "Type of trader; by default the speculators of the report: noncommercial (legacy), "
        "leveraged_funds (tff), managed_money (disaggregated)"
    ),
)


def get_cot_reports(request: Request) -> CotReports:
    service = getattr(request.app.state, "cot_reports", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="Commitments of Traders unavailable (no database)"
        )
    return service


def _load(load: ReportLoad) -> CotLoad:
    return CotLoad(
        report=load.report,
        status=load.status,
        fetched_at=load.fetched_at,
        first_day=load.first_day,
        last_day=load.last_day,
        years_read=load.years_read,
        years_failed=load.years_failed,
        reason=load.reason,
    )


def _market(raw: str) -> str:
    code = raw.strip().upper()
    if not _CODE.match(code):
        raise HTTPException(
            status_code=422,
            detail=f"{raw!r} is not a CFTC contract market code (13874A): see GET /cot/markets",
        )
    return code


def _category(report: str, raw: Optional[str]) -> CotCategory:
    try:
        code = category_of(report, raw.strip().lower() if raw else None)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    label = next(known.label for known in REPORTS[report].categories if known.code == code)
    return CotCategory(code=code, label=label)


def _period(service: CotReports, start: Optional[date], end: Optional[date]) -> tuple[date, date]:
    end = end or service.today()
    start = start or end - timedelta(days=365 * DEFAULT_YEARS)
    if start > end:
        raise HTTPException(status_code=422, detail="start is after end")
    return start, end


def _nothing_stored(report: str, market: str, load: CotLoad) -> HTTPException:
    if load.status == "failed" and load.first_day is None:
        return HTTPException(status_code=502, detail=f"The CFTC could not be read: {load.reason}")
    return HTTPException(
        status_code=404,
        detail=(
            f"No {report} report of the market {market} in this period "
            "(GET /cot/markets lists the markets stored)"
        ),
    )


def _signal(signal: Signal) -> CotSignal:
    return CotSignal(
        date=signal.report_date,
        published_on=signal.published_on,
        open_interest=signal.open_interest,
        long=signal.long,
        short=signal.short,
        spreading=signal.spreading,
        net=signal.net,
        net_share=float(signal.net_share) if signal.net_share is not None else None,
        change=signal.change,
        cot_index=float(signal.cot_index) if signal.cot_index is not None else None,
    )


@router.get("/markets", response_model=CotMarketsResponse)
async def get_cot_markets(
    report: Optional[str] = Query(
        None,
        pattern="^(legacy|tff|disaggregated)$",
        description="Markets of one report (all three when empty)",
    ),
    q: Optional[str] = Query(
        None, max_length=60, description="Part of the name or of the code, in any case"
    ),
    key: bool = Query(False, description="Only the markets shown when none is asked"),
    service: CotReports = Depends(get_cot_reports),
) -> CotMarketsResponse:
    """The markets stored, with their latest name and the report dates stored.

    The current year of each report asked is read from the CFTC first when needed, so
    that the markets of the latest reports are listed; the older years are read by
    the routes of a market or by ``POST /cot/refresh``.
    """
    reports = [report] if report else list(REPORTS)
    today = service.today()
    loads = [_load(await service.ensure(name, today)) for name in reports]
    wanted = (q or "").strip().upper()
    markets = []
    for name in reports:
        for market in await service.markets(name):
            is_key = market.code in KEY_MARKETS[name]
            if key and not is_key:
                continue
            if wanted and wanted not in market.code and wanted not in market.name.upper():
                continue
            markets.append(
                CotMarket(
                    report=name,
                    market=market.code,
                    name=market.name,
                    contract_units=market.contract_units,
                    first_date=market.first_date,
                    last_date=market.last_date,
                    weeks=market.weeks,
                    key=is_key,
                )
            )
    return CotMarketsResponse(markets=markets, loads=loads)


@router.get("/positions", response_model=CotPositionsResponse)
async def get_cot_positions(
    market: str = Query(..., description="CFTC contract market code (13874A)"),
    report: str = ReportParameter,
    start: Optional[date] = Query(
        None,
        description=f"First report date included ({DEFAULT_YEARS} years before end by default)",
    ),
    end: Optional[date] = Query(None, description="Last report date included (today by default)"),
    service: CotReports = Depends(get_cot_reports),
) -> CotPositionsResponse:
    """The positions of every type of trader in one market, report by report, oldest first.

    Positions are numbers of contracts. The years of the period are read from the
    CFTC first when needed; when that fails, the stored reports are answered and
    ``load.status`` is ``failed``.
    """
    code = _market(market)
    start, end = _period(service, start, end)
    load = _load(await service.ensure(report, start, end))
    weeks = await service.history(report, code, start, end)
    if not weeks:
        raise _nothing_stored(report, code, load)
    return CotPositionsResponse(
        report=report,
        market=code,
        name=weeks[-1].name,
        categories=[
            CotCategory(code=known.code, label=known.label) for known in REPORTS[report].categories
        ],
        start=start,
        end=end,
        weeks=[
            CotWeek(
                date=week.report_date,
                published_on=published_on(week.report_date),
                open_interest=week.open_interest,
                positions=[
                    CotPosition(
                        category=category,
                        long=position.long,
                        short=position.short,
                        spreading=position.spreading,
                        net=position.net,
                    )
                    for category, position in week.positions.items()
                ],
            )
            for week in weeks
        ],
        load=load,
    )


async def _signals(
    service: CotReports, report: str, code: str, category: str, window: int, start: date, end: date
) -> tuple[List[StoredWeek], List[Signal], ReportLoad]:
    """The signals of a market from ``start`` to ``end``: the reports of the window before
    ``start`` are read too, so that the COT index of ``start`` is computed."""
    since = start - timedelta(weeks=window)
    load = await service.ensure(report, since, end)
    weeks = await service.history(report, code, since, end)
    found = [signal for signal in signals(weeks, category, window) if signal.report_date >= start]
    return weeks, found, load


@router.get("/signals", response_model=CotSignalsResponse)
async def get_cot_signals(
    market: str = Query(..., description="CFTC contract market code (13874A)"),
    report: str = ReportParameter,
    category: Optional[str] = CategoryParameter,
    window: int = WindowParameter,
    start: Optional[date] = Query(
        None, description=f"First report date given ({DEFAULT_YEARS} years before end by default)"
    ),
    end: Optional[date] = Query(None, description="Last report date given (today by default)"),
    service: CotReports = Depends(get_cot_reports),
) -> CotSignalsResponse:
    """The positioning of one type of trader in one market, report by report, oldest first.

    For each report: the net position (long minus short), its share of the open
    interest, its change since the previous report, and the COT index over the
    window (the reports of the window before ``start`` are read too). Each report
    gives its estimated day of publication: a backtest must use it, not the report
    date.
    """
    code = _market(market)
    kind = _category(report, category)
    start, end = _period(service, start, end)
    weeks, found, load = await _signals(service, report, code, kind.code, window, start, end)
    result = _load(load)
    if not found:
        raise _nothing_stored(report, code, result)
    return CotSignalsResponse(
        report=report,
        market=code,
        name=weeks[-1].name,
        category=kind,
        window_weeks=window,
        start=start,
        end=end,
        signals=[_signal(signal) for signal in found],
        load=result,
    )


def _extremity(item: CotExtreme) -> tuple[int, float]:
    index = item.signal.cot_index
    return (1, 0.0) if index is None else (0, -abs(index - 50))


@router.get("/extremes", response_model=CotExtremesResponse)
async def get_cot_extremes(
    report: str = ReportParameter,
    market: Optional[List[str]] = Query(
        None,
        description=(
            "CFTC contract market codes, repeated or separated by commas (the key markets "
            f"of the report when empty); at most {MAX_MARKETS}"
        ),
    ),
    category: Optional[str] = CategoryParameter,
    window: int = WindowParameter,
    service: CotReports = Depends(get_cot_reports),
) -> CotExtremesResponse:
    """The latest report of several markets, the most extreme positioning first.

    A market whose COT index is 0 or 100 has its type of trader at the lowest or the
    highest net position of the window. A market without index comes last.
    """
    kind = _category(report, category)
    asked: List[str] = []
    for piece in market or []:
        asked += [_market(code) for code in piece.split(",") if code.strip()]
    codes = list(dict.fromkeys(asked)) or list(KEY_MARKETS[report])
    if len(codes) > MAX_MARKETS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_MARKETS} markets")
    end = service.today()
    # The window before the latest report, and a few weeks more for a late report.
    since = end - timedelta(weeks=window + 4)
    load = await service.ensure(report, since, end)
    found, missing = [], []
    for code in codes:
        weeks = await service.history(report, code, since, end)
        latest = signals(weeks, kind.code, window)[-1:] if weeks else []
        if not latest:
            missing.append(code)
            continue
        found.append(CotExtreme(market=code, name=weeks[-1].name, signal=_signal(latest[0])))
    result = _load(load)
    if not found and result.status == "failed" and result.first_day is None:
        raise HTTPException(status_code=502, detail=f"The CFTC could not be read: {result.reason}")
    return CotExtremesResponse(
        report=report,
        category=kind,
        window_weeks=window,
        markets=sorted(found, key=_extremity),
        missing=missing,
        load=result,
    )


@router.post("/refresh", response_model=CotRefreshResponse)
async def refresh_cot_reports(
    report: Optional[List[str]] = Query(
        None, description="legacy, tff, disaggregated, or all three when left out (repeated)"
    ),
    start_year: Optional[int] = Query(
        None, ge=1986, le=2100, description="First year read (the current year by default)"
    ),
    end_year: Optional[int] = Query(
        None, ge=1986, le=2100, description="Last year read (the current year by default)"
    ),
    force: bool = Query(False, description="Read even the years that need no reading"),
    service: CotReports = Depends(get_cot_reports),
) -> CotRefreshResponse:
    """Read years of the reports into the database.

    The years up to 2016 come from one file per report (Legacy: 23 MB) and are read
    once; a later past year read from 15 January of the next year on, and the current
    year read less than ``COT_REFRESH_HOURS`` hours ago, are skipped unless ``force``
    is set. The whole history is ``start_year=1986``.
    """
    names = list(dict.fromkeys(name.strip().lower() for name in report or REPORTS))
    unknown = [name for name in names if name not in REPORTS]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unknown report {unknown[0]!r}: {', '.join(REPORTS)}"
        )
    current = service.today().year
    first, last = start_year or current, end_year or current
    if first > last:
        raise HTTPException(status_code=422, detail="start_year is after end_year")
    loads = [_load(await service.refresh(name, first, last, force=force)) for name in names]
    return CotRefreshResponse(loads=loads)
