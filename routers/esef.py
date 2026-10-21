"""HTTP routes of the ESEF filings of European listed companies.

The ticker designates an instrument of the catalogue; its company is found through
a verified LEI (``financials/esef_lei.py``), asked to GLEIF with the ISIN of the
instrument, never with the ticker. An instrument without ISIN, a fund, or an
issuer GLEIF does not confirm has none: ``404`` with the reason.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from database.price_series import resolve_price_series_async
from financials.esef_statements import EsefLoad, EsefStatements
from financials.sec_check import MISSING_SEC, NOT_COMPARED, SecCheck
from schemas.esef import (
    EsefCheckResponse,
    EsefFigure,
    EsefFigureCheck,
    EsefFilingInfo,
    EsefFilingsResponse,
    EsefFiscalYear,
    EsefLoadInfo,
    EsefRefreshResponse,
    EsefStatementsResponse,
    EsefYearCheck,
)

router = APIRouter(prefix="/esef", tags=["ESEF filings"])

YearsParameter = Query(
    None, ge=1, le=30, description="Most recent fiscal years given (every year when left out)"
)
IsinParameter = Query(None, description="Instrument of the ticker, when several share it")


def get_esef_statements(request: Request) -> EsefStatements:
    service = getattr(request.app.state, "esef_statements", None)
    if service is None:
        raise HTTPException(status_code=503, detail="ESEF filings unavailable (no database)")
    return service


def get_esef_check(request: Request) -> SecCheck:
    service = getattr(request.app.state, "esef_check", None)
    if service is None:
        raise HTTPException(status_code=503, detail="ESEF filings unavailable (no database)")
    return service


async def _asset_id(service: EsefStatements, ticker: str, isin: Optional[str]) -> int:
    async with service.session_factory() as session:
        series = await resolve_price_series_async(session, ticker, isin=isin)
    if series is None:
        raise HTTPException(status_code=404, detail=f"No instrument found for {ticker}")
    return series.asset_id


def _load_info(load: EsefLoad) -> EsefLoadInfo:
    return EsefLoadInfo(
        status=load.status,
        fetched_at=load.fetched_at,
        filings=load.filings,
        last_report_end=load.last_report_end,
        facts=load.facts,
        read=list(load.read),
        refused=dict(load.refused),
        reason=load.reason,
    )


def _usable(load: EsefLoad, has_content: bool) -> None:
    """Refuse an answer that has nothing to give: no LEI, or nothing ever read."""
    if load.status == "no_lei":
        raise HTTPException(
            status_code=404, detail=f"No ESEF filer for this instrument: {load.reason}"
        )
    if load.status == "failed" and not has_content:
        raise HTTPException(
            status_code=502, detail=f"filings.xbrl.org could not be read: {load.reason}"
        )


async def _viewers(service: EsefStatements, lei: str) -> dict[str, Optional[str]]:
    """The link of each filing of a company in the viewer of filings.xbrl.org."""
    return {filing.fxo_id: filing.viewer_url for filing in await service.filings(lei)}


@router.get("/{ticker}/statements", response_model=EsefStatementsResponse)
async def get_esef_statements_of_instrument(
    ticker: str,
    years: Optional[int] = YearsParameter,
    isin: Optional[str] = IsinParameter,
    service: EsefStatements = Depends(get_esef_statements),
) -> EsefStatementsResponse:
    """The annual statements the company filed in ESEF, most recent year first.

    Each figure names the IFRS concept it was read from and its filing, with the link
    to the filing in the viewer of filings.xbrl.org. The filings are read when missing
    or older than ``ESEF_REFRESH_HOURS``; when that fails, the stored ones are given
    and ``load.status`` is ``failed``.
    """
    asset_id = await _asset_id(service, ticker, isin)
    load, fiscal_years = await service.annual(asset_id)
    _usable(load, bool(fiscal_years))
    viewers = await _viewers(service, load.lei) if fiscal_years else {}
    kept = fiscal_years if years is None else fiscal_years[:years]
    return EsefStatementsResponse(
        ticker=ticker.upper(),
        lei=load.lei,
        entity_name=load.entity_name,
        load=_load_info(load),
        years=[
            EsefFiscalYear(
                period_end=year.period_end,
                currency=year.currency,
                figures={
                    column: EsefFigure(
                        value=float(figure.value),
                        concept=figure.concept,
                        taxonomy=figure.taxonomy,
                        unit=figure.unit,
                        fxo_id=figure.accession,
                        report_end=figure.filed,
                        viewer_url=viewers.get(figure.accession),
                    )
                    for column, figure in year.figures.items()
                },
            )
            for year in kept
        ],
    )


@router.get("/{ticker}/check", response_model=EsefCheckResponse)
async def check_statements_against_esef(
    ticker: str,
    years: Optional[int] = YearsParameter,
    isin: Optional[str] = IsinParameter,
    statements: EsefStatements = Depends(get_esef_statements),
    checker: SecCheck = Depends(get_esef_check),
) -> EsefCheckResponse:
    """The annual statements stored from Yahoo checked against the ESEF filings.

    For each fiscal year filed and each figure both sources define alike, the
    status is ``match`` (within ``SEC_CHECK_TOLERANCE``), ``mismatch``,
    ``missing_yahoo`` or ``missing_esef``.
    """
    asset_id = await _asset_id(statements, ticker, isin)
    result = await checker.check(asset_id)
    _usable(result.load, bool(result.years))
    viewers = await _viewers(statements, result.load.lei) if result.years else {}
    kept = result.years if years is None else result.years[:years]
    return EsefCheckResponse(
        ticker=ticker.upper(),
        lei=result.load.lei,
        entity_name=result.load.entity_name,
        tolerance=float(result.tolerance),
        not_compared=dict(NOT_COMPARED),
        load=_load_info(result.load),
        years=[
            EsefYearCheck(
                period_end=year.period_end,
                yahoo_period_end=year.yahoo_period_end,
                currency=year.currency,
                yahoo_currency=year.yahoo_currency,
                note=year.note,
                match=year.count("match"),
                mismatch=year.count("mismatch"),
                missing_yahoo=year.count("missing_yahoo"),
                missing_esef=year.count(MISSING_SEC),
                figures=[
                    EsefFigureCheck(
                        column=figure.column,
                        statement=figure.statement,
                        status="missing_esef" if figure.status == MISSING_SEC else figure.status,
                        esef=None if figure.sec is None else float(figure.sec),
                        yahoo=None if figure.yahoo is None else float(figure.yahoo),
                        difference=None if figure.difference is None else float(figure.difference),
                        concept=figure.concept,
                        viewer_url=viewers.get(figure.accession) if figure.accession else None,
                    )
                    for figure in year.figures
                ],
            )
            for year in kept
        ],
    )


@router.get("/{ticker}/filings", response_model=EsefFilingsResponse)
async def get_esef_filings_of_instrument(
    ticker: str,
    isin: Optional[str] = IsinParameter,
    service: EsefStatements = Depends(get_esef_statements),
) -> EsefFilingsResponse:
    """The filings of the company in the index of filings.xbrl.org, latest period first.

    Each filing comes with its links (viewer, report, xBRL-JSON, package), its counts
    of errors, and what its reading found: the true period of its facts, its counts of
    values, breakdowns left out and conflicts dropped, or why it was refused.
    """
    asset_id = await _asset_id(service, ticker, isin)
    load = await service.refresh(asset_id)
    filings = await service.filings(load.lei) if load.lei else []
    _usable(load, bool(filings))
    return EsefFilingsResponse(
        ticker=ticker.upper(),
        lei=load.lei,
        entity_name=load.entity_name,
        load=_load_info(load),
        filings=[
            EsefFilingInfo(
                fxo_id=filing.fxo_id,
                listed_period_end=filing.listed_period_end,
                report_end=filing.report_end,
                country=filing.country,
                read_status=filing.read_status,
                read_reason=filing.read_reason,
                read_at=filing.read_at,
                facts=filing.facts,
                breakdowns=filing.breakdowns,
                conflicts=filing.conflicts,
                error_count=filing.error_count,
                warning_count=filing.warning_count,
                inconsistency_count=filing.inconsistency_count,
                processed=filing.processed,
                viewer_url=filing.viewer_url,
                report_url=filing.report_url,
                json_url=filing.json_url,
                package_url=filing.package_url,
            )
            for filing in filings
        ],
    )


@router.post("/{ticker}/refresh", response_model=EsefRefreshResponse)
async def refresh_esef_filings(
    ticker: str,
    isin: Optional[str] = IsinParameter,
    service: EsefStatements = Depends(get_esef_statements),
) -> EsefRefreshResponse:
    """Ask GLEIF for the LEI again, list the filings and read every period again now."""
    asset_id = await _asset_id(service, ticker, isin)
    load = await service.refresh(asset_id, force=True, recheck_lei=True)
    if load.status == "no_lei":
        raise HTTPException(
            status_code=404, detail=f"No ESEF filer for this instrument: {load.reason}"
        )
    return EsefRefreshResponse(
        ticker=ticker.upper(), lei=load.lei, entity_name=load.entity_name, load=_load_info(load)
    )
