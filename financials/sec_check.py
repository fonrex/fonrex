"""Compare the statements stored from Yahoo with the ones filed with the SEC.

The SEC figures are those the company filed (``financials/sec_statements.py``);
the Yahoo figures are those the deep enrichment stored in ``financial_statements``,
read by fiscal year (``fiscal_years``). For each fiscal year and each figure both
sources define the same way, the check says whether they agree within
``SEC_CHECK_TOLERANCE`` (1 % by default, as a ratio).

How the two are put side by side:

* **a year is matched by its end**, within ``YEAR_END_SLACK`` days: Yahoo may date
  a year ending on the last Saturday of September (Apple) at the end of the month;
* **payments are compared as amounts**: Yahoo gives capex and dividends as
  negative cash flows, the SEC as positive payments;
* **only figures defined alike are compared.** Cash (Yahoo may add short-term
  investments) and equity (Yahoo includes minority interests) are left out:
  their gap would be one of definition, not an error;
* **a currency that differs** stops the comparison of the year: a number in
  euros is not compared with a number in dollars.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financials.fiscal_years import fiscal_years
from financials.xbrl_mapping import COLUMNS_BY_NAME
from financials.xbrl_mapping import FiscalYear as SecFiscalYear
from models import FinancialStatement
from settings import env_decimal

# Columns compared, and whether they are compared as amounts (sign left aside).
COMPARED: dict[str, bool] = {
    "revenue": False,
    "gross_profit": False,
    "operating_income": False,
    "net_income": False,
    "eps_basic": False,
    "eps_diluted": False,
    "shares_basic": False,
    "shares_diluted": False,
    "interest_expense": True,
    "tax_provision": False,
    "total_assets": False,
    "total_liabilities": False,
    "operating_cashflow": False,
    "investing_cashflow": False,
    "financing_cashflow": False,
    "capex": True,
    "dividends_paid": True,
}
# Defined differently by the two sources: never compared.
NOT_COMPARED = {
    "cash_and_equivalents": "Yahoo may add the short-term investments",
    "total_equity": "Yahoo includes the minority interests",
}
YEAR_END_SLACK = 14  # days
NO_YAHOO_YEAR = (
    "No Yahoo statements for this fiscal year (deep enrichment not run, "
    "or Yahoo gives fewer years): not compared"
)

MATCH = "match"
MISMATCH = "mismatch"
MISSING_YAHOO = "missing_yahoo"
MISSING_SEC = "missing_sec"


def check_tolerance() -> Decimal:
    """Largest relative gap still counted as an agreement (``SEC_CHECK_TOLERANCE``)."""
    return env_decimal("SEC_CHECK_TOLERANCE", "0.01", minimum="0", maximum="0.5")


@dataclass(frozen=True)
class FigureCheck:
    column: str
    statement: str
    status: str  # match, mismatch, missing_yahoo, missing_sec
    sec: Decimal | None = None
    yahoo: Decimal | None = None
    difference: Decimal | None = None  # (yahoo - sec) / |sec|, on amounts for payments
    concept: str | None = None
    filing_url: str | None = None
    accession: str | None = None  # the filing of the figure (an fxo_id for ESEF)


@dataclass
class YearCheck:
    period_end: date  # as filed with the SEC
    currency: str
    yahoo_period_end: date | None = None
    yahoo_currency: str | None = None
    note: str | None = None
    figures: list[FigureCheck] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for figure in self.figures if figure.status == status)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() else None


def _yahoo_year(sec_end: date, yahoo_years: list[Any], used: set[int]) -> Any | None:
    """The Yahoo year ending closest to ``sec_end``, within the slack."""
    best = None
    for index, year in enumerate(yahoo_years):
        end = getattr(year, "period_end", None)
        if index in used or end is None:
            continue
        gap = abs((end - sec_end).days)
        if gap <= YEAR_END_SLACK and (best is None or gap < best[0]):
            best = (gap, index)
    if best is None:
        return None
    used.add(best[1])
    return yahoo_years[best[1]]


def _compare_figure(
    column: str, sec_year: SecFiscalYear, yahoo_value: Any, tolerance: Decimal
) -> FigureCheck | None:
    figure = sec_year.figures.get(column)
    sec = figure.value if figure else None
    yahoo = _decimal(yahoo_value)
    statement = COLUMNS_BY_NAME[column].statement
    origin = (
        {"concept": figure.concept, "filing_url": figure.filing_url, "accession": figure.accession}
        if figure
        else {}
    )
    if sec is None and yahoo is None:
        return None
    if yahoo is None:
        return FigureCheck(column, statement, MISSING_YAHOO, sec=sec, **origin)
    if sec is None:
        return FigureCheck(column, statement, MISSING_SEC, yahoo=yahoo)
    if COMPARED[column]:
        sec_amount, yahoo_amount = abs(sec), abs(yahoo)
    else:
        sec_amount, yahoo_amount = sec, yahoo
    if sec_amount == 0:
        difference = Decimal(0) if yahoo_amount == 0 else None
        status = MATCH if yahoo_amount == 0 else MISMATCH
    else:
        difference = (yahoo_amount - sec_amount) / abs(sec_amount)
        status = MATCH if abs(difference) <= tolerance else MISMATCH
    return FigureCheck(
        column,
        statement,
        status,
        sec=sec,
        yahoo=yahoo,
        difference=None if difference is None else difference.quantize(Decimal("0.000001")),
        **origin,
    )


def compare(
    sec_years: Iterable[SecFiscalYear],
    yahoo_years: Iterable[Any],
    tolerance: Decimal | None = None,
    *,
    filed_with: str = "the SEC filing",
) -> list[YearCheck]:
    """Check each fiscal year filed with the SEC against the Yahoo year ending with it.

    ``yahoo_years`` are fiscal years as ``fiscal_years()`` gives them (one object
    per year, every column). A SEC year without Yahoo year has no figures and a
    ``note``: the statements of that year were never stored from Yahoo.
    """
    tolerance = check_tolerance() if tolerance is None else tolerance
    yahoo_years = list(yahoo_years)
    used: set[int] = set()
    checks = []
    for sec_year in sec_years:
        check = YearCheck(sec_year.period_end, sec_year.currency)
        yahoo_year = _yahoo_year(sec_year.period_end, yahoo_years, used)
        if yahoo_year is not None:
            check.yahoo_period_end = yahoo_year.period_end
            check.yahoo_currency = getattr(yahoo_year, "currency", None)
        if yahoo_year is None:
            check.note = NO_YAHOO_YEAR
            checks.append(check)
            continue
        if check.yahoo_currency and check.yahoo_currency.upper() != sec_year.currency.upper():
            check.note = (
                f"Yahoo gives this year in {check.yahoo_currency}, {filed_with} in "
                f"{sec_year.currency}: not compared"
            )
            checks.append(check)
            continue
        for column in COMPARED:
            yahoo_value = getattr(yahoo_year, column, None)
            result = _compare_figure(column, sec_year, yahoo_value, tolerance)
            if result is not None:
                check.figures.append(result)
        checks.append(check)
    return checks


@dataclass(frozen=True)
class SecCheckResult:
    load: Any  # SecLoad of financials/sec_statements.py
    tolerance: Decimal
    years: list[YearCheck]


class SecCheck:
    """The check of an instrument: its SEC filings against its stored Yahoo statements."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        statements: Any,
        *,
        filed_with: str = "the SEC filing",
    ) -> None:
        """``statements`` gives the annual statements filed (``SecStatements`` or
        ``EsefStatements``); ``filed_with`` names them in the notes."""
        self.session_factory = session_factory
        self.statements = statements
        self.filed_with = filed_with

    async def yahoo_years(self, asset_id: int) -> list[Any]:
        """The annual statements stored from Yahoo, by fiscal year (three rows a year)."""
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(FinancialStatement).where(
                            FinancialStatement.asset_id == asset_id,
                            FinancialStatement.period_type == "annual",
                        )
                    )
                )
                .scalars()
                .all()
            )
        return fiscal_years(rows)

    async def check(self, asset_id: int, *, refresh: bool = True) -> SecCheckResult:
        tolerance = check_tolerance()
        load, sec_years = await self.statements.annual(asset_id, refresh=refresh)
        if not sec_years:
            return SecCheckResult(load, tolerance, [])
        return SecCheckResult(
            load,
            tolerance,
            compare(
                sec_years,
                await self.yahoo_years(asset_id),
                tolerance,
                filed_with=self.filed_with,
            ),
        )
