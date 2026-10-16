"""HTTP routes of the annual statements filed with the SEC (XBRL).

The ticker designates an instrument of the catalogue; its company at the SEC is
found through a verified CIK (``financials/sec_cik.py``), never from the ticker
itself. An instrument without US listing has none: ``404`` with the reason.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from database.price_series import resolve_price_series_async
from financials.sec_check import NOT_COMPARED, SecCheck
from financials.sec_statements import SecLoad, SecStatements
from schemas.sec import (
    SecCheckResponse,
    SecFigure,
    SecFigureCheck,
    SecFiscalYear,
    SecLoadInfo,
    SecRefreshResponse,
    SecStatementsResponse,
    SecYearCheck,
)

router = APIRouter(prefix="/sec", tags=["SEC filings"])

YearsParameter = Query(
    None, ge=1, le=30, description="Most recent fiscal years given (every year when left out)"
)
IsinParameter = Query(None, description="Instrument of the ticker, when several share it")


def get_sec_statements(request: Request) -> SecStatements:
    service = getattr(request.app.state, "sec_statements", None)
    if service is None:
        raise HTTPException(status_code=503, detail="SEC filings unavailable (no database)")
    return service


def get_sec_check(request: Request) -> SecCheck:
    service = getattr(request.app.state, "sec_check", None)
    if service is None:
        raise HTTPException(status_code=503, detail="SEC filings unavailable (no database)")
    return service


async def _asset_id(service: SecStatements, ticker: str, isin: Optional[str]) -> int:
    async with service.session_factory() as session:
        series = await resolve_price_series_async(session, ticker, isin=isin)
    if series is None:
        raise HTTPException(status_code=404, detail=f"No instrument found for {ticker}")
    return series.asset_id


def _load_info(load: SecLoad) -> SecLoadInfo:
    return SecLoadInfo(
        status=load.status,
        fetched_at=load.fetched_at,
        last_filed=load.last_filed,
        facts=load.facts,
        reason=load.reason,
    )


def _usable(load: SecLoad, has_years: bool) -> None:
    """Refuse an answer that has nothing to give: no CIK, or nothing ever read."""
    if load.status == "no_cik":
        raise HTTPException(
            status_code=404, detail=f"No SEC filer for this instrument: {load.reason}"
        )
    if load.status == "failed" and not has_years:
        raise HTTPException(status_code=502, detail=f"The SEC could not be read: {load.reason}")


@router.get("/{ticker}/statements", response_model=SecStatementsResponse)
async def get_sec_statements_of_instrument(
    ticker: str,
    years: Optional[int] = YearsParameter,
    isin: Optional[str] = IsinParameter,
    service: SecStatements = Depends(get_sec_statements),
) -> SecStatementsResponse:
    """The annual statements the company filed with the SEC, most recent year first.

    Each figure names the XBRL concept it was read from and the filing (form,
    date, link to EDGAR). The facts are read from the SEC when missing or older
    than ``SEC_XBRL_REFRESH_HOURS``; when that fails, the stored ones are given
    and ``load.status`` is ``failed``.
    """
    asset_id = await _asset_id(service, ticker, isin)
    load, fiscal_years = await service.annual(asset_id)
    _usable(load, bool(fiscal_years))
    kept = fiscal_years if years is None else fiscal_years[:years]
    return SecStatementsResponse(
        ticker=ticker.upper(),
        cik=load.cik,
        entity_name=load.entity_name,
        load=_load_info(load),
        years=[
            SecFiscalYear(
                period_end=year.period_end,
                currency=year.currency,
                figures={
                    column: SecFigure(
                        value=float(figure.value),
                        concept=figure.concept,
                        taxonomy=figure.taxonomy,
                        unit=figure.unit,
                        form=figure.form,
                        filed=figure.filed,
                        accession=figure.accession,
                        filing_url=figure.filing_url,
                    )
                    for column, figure in year.figures.items()
                },
            )
            for year in kept
        ],
    )


@router.get("/{ticker}/check", response_model=SecCheckResponse)
async def check_statements_against_sec(
    ticker: str,
    years: Optional[int] = YearsParameter,
    isin: Optional[str] = IsinParameter,
    statements: SecStatements = Depends(get_sec_statements),
    checker: SecCheck = Depends(get_sec_check),
) -> SecCheckResponse:
    """The annual statements stored from Yahoo checked against the SEC filings.

    For each fiscal year filed and each figure both sources define alike, the
    status is ``match`` (within ``SEC_CHECK_TOLERANCE``), ``mismatch``,
    ``missing_yahoo`` or ``missing_sec``.
    """
    asset_id = await _asset_id(statements, ticker, isin)
    result = await checker.check(asset_id)
    _usable(result.load, bool(result.years))
    kept = result.years if years is None else result.years[:years]
    return SecCheckResponse(
        ticker=ticker.upper(),
        cik=result.load.cik,
        entity_name=result.load.entity_name,
        tolerance=float(result.tolerance),
        not_compared=dict(NOT_COMPARED),
        load=_load_info(result.load),
        years=[
            SecYearCheck(
                period_end=year.period_end,
                yahoo_period_end=year.yahoo_period_end,
                currency=year.currency,
                yahoo_currency=year.yahoo_currency,
                note=year.note,
                match=year.count("match"),
                mismatch=year.count("mismatch"),
                missing_yahoo=year.count("missing_yahoo"),
                missing_sec=year.count("missing_sec"),
                figures=[
                    SecFigureCheck(
                        column=figure.column,
                        statement=figure.statement,
                        status=figure.status,
                        sec=None if figure.sec is None else float(figure.sec),
                        yahoo=None if figure.yahoo is None else float(figure.yahoo),
                        difference=None if figure.difference is None else float(figure.difference),
                        concept=figure.concept,
                        filing_url=figure.filing_url,
                    )
                    for figure in year.figures
                ],
            )
            for year in kept
        ],
    )


@router.post("/{ticker}/refresh", response_model=SecRefreshResponse)
async def refresh_sec_statements(
    ticker: str,
    isin: Optional[str] = IsinParameter,
    service: SecStatements = Depends(get_sec_statements),
) -> SecRefreshResponse:
    """Look the CIK up again and read the facts of the company from the SEC now."""
    asset_id = await _asset_id(service, ticker, isin)
    load = await service.refresh(asset_id, force=True, recheck_cik=True)
    if load.status == "no_cik":
        raise HTTPException(
            status_code=404, detail=f"No SEC filer for this instrument: {load.reason}"
        )
    return SecRefreshResponse(
        ticker=ticker.upper(), cik=load.cik, entity_name=load.entity_name, load=_load_info(load)
    )
