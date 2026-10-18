"""CftcProvider — the Commitments of Traders reports of the CFTC.

Source: https://www.cftc.gov (official, free, no key). Every week the CFTC
publishes the positions held on the Tuesday in the US futures markets, by type of
trader. It also publishes one zip per report and year, each holding one
comma-separated text file::

    https://www.cftc.gov/files/dea/history/{file}

Three reports are read, futures only:

* ``legacy``: every market since 1986 (``deacot{year}.zip``); non-commercial
  (the speculators), commercial and non-reportable positions;
* ``tff`` (Traders in Financial Futures): the financial markets since June 2006
  (``fut_fin_txt_{year}.zip``); dealers, asset managers, leveraged funds, other
  reportables and non-reportable positions;
* ``disaggregated``: the commodity markets since June 2006
  (``fut_disagg_txt_{year}.zip``); producers and merchants, swap dealers,
  managed money, other reportables and non-reportable positions.

The years up to 2016 are in one file per report (``deacot1986_2016.zip``,
``fin_fut_txt_2006_2016.zip``, ``fut_disagg_txt_hist_2006_2016.zip``).

One line per market and report date. A market is keyed by its CFTC contract
market code (six characters, ``13874A``, ``20974+``, ``001602``), which stays when
the name changes. Positions are numbers of contracts. The files differ in their
details (see ``tests/fixtures/cftc/README.md``): the TFF history writes its dates
``M/D/YYYY 12:00:00 AM`` and its positions with six decimals; Legacy reports came
twice a month until September 1992; non-reportable positions are negative on a
few dates of the Legacy history.

A file is read whole or not at all: an unknown layout, a date outside the file, a
line given twice, a position that is not a whole number, or positions that do not
add up to the open interest raise ``CftcError``.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from concurrency import run_sync
from financials.numbers import parse_number
from financials.providers.base import BaseFinancialProvider

logger = logging.getLogger(__name__)

CFTC_HISTORY_URL = "https://www.cftc.gov/files/dea/history/{file}"
SOURCE = "CFTC, Commitments of Traders (https://www.cftc.gov)"

LEGACY = "legacy"
TFF = "tff"
DISAGGREGATED = "disaggregated"

# The last year of the files that hold several years.
HISTORY_LAST_YEAR = 2016

# Consolidated markets (code ending with "+") add up within a few contracts:
# their positions are rounded. Every other market adds up exactly.
CONSOLIDATED_TOLERANCE = 10

_CODE = re.compile(r"[0-9A-Z+]{6}")


class CftcError(ValueError):
    """A file of the CFTC could not be read: nothing of it is kept."""


@dataclass(frozen=True)
class Category:
    """A type of trader of a report and the columns of its positions."""

    code: str
    label: str
    long: str
    short: str
    spreading: str | None = None
    # Only the non-reportable positions (the rest of the open interest) may be negative.
    may_be_negative: bool = False


@dataclass(frozen=True)
class Report:
    """A report of the CFTC: its files, its first report date and its layout."""

    name: str
    label: str
    first_day: date
    year_file: str
    history_file: str
    columns: int
    name_column: str
    yymmdd_column: str
    date_column: str
    code_column: str
    open_interest_column: str
    units_column: str
    categories: tuple[Category, ...]

    @property
    def first_year(self) -> int:
        return self.first_day.year

    def file_name(self, year: int) -> str:
        """The file holding a year: the history file up to 2016, the file of the year after."""
        if not self.first_year <= year:
            raise CftcError(f"the {self.name} report starts in {self.first_year}, not {year}")
        if year <= HISTORY_LAST_YEAR:
            return self.history_file
        return self.year_file.format(year=year)

    def years_of_file(self, year: int) -> tuple[int, int]:
        """The first and last year held by the file of a year."""
        if year <= HISTORY_LAST_YEAR:
            return self.first_year, HISTORY_LAST_YEAR
        return year, year

    def url(self, year: int) -> str:
        return CFTC_HISTORY_URL.format(file=self.file_name(year))


def _nonreportable(long: str, short: str) -> Category:
    return Category("nonreportable", "Non-reportable", long, short, may_be_negative=True)


REPORTS: dict[str, Report] = {
    report.name: report
    for report in (
        Report(
            name=LEGACY,
            label="Legacy (futures only)",
            first_day=date(1986, 1, 15),
            year_file="deacot{year}.zip",
            history_file="deacot1986_2016.zip",
            columns=129,
            name_column="Market and Exchange Names",
            yymmdd_column="As of Date in Form YYMMDD",
            date_column="As of Date in Form YYYY-MM-DD",
            code_column="CFTC Contract Market Code",
            open_interest_column="Open Interest (All)",
            units_column="Contract Units",
            categories=(
                Category(
                    "noncommercial",
                    "Non-commercial",
                    "Noncommercial Positions-Long (All)",
                    "Noncommercial Positions-Short (All)",
                    "Noncommercial Positions-Spreading (All)",
                ),
                Category(
                    "commercial",
                    "Commercial",
                    "Commercial Positions-Long (All)",
                    "Commercial Positions-Short (All)",
                ),
                _nonreportable(
                    "Nonreportable Positions-Long (All)", "Nonreportable Positions-Short (All)"
                ),
            ),
        ),
        Report(
            name=TFF,
            label="Traders in Financial Futures (futures only)",
            first_day=date(2006, 6, 13),
            year_file="fut_fin_txt_{year}.zip",
            history_file="fin_fut_txt_2006_2016.zip",
            columns=87,
            name_column="Market_and_Exchange_Names",
            yymmdd_column="As_of_Date_In_Form_YYMMDD",
            date_column="Report_Date_as_YYYY-MM-DD",
            code_column="CFTC_Contract_Market_Code",
            open_interest_column="Open_Interest_All",
            units_column="Contract_Units",
            categories=(
                Category(
                    "dealer",
                    "Dealer / intermediary",
                    "Dealer_Positions_Long_All",
                    "Dealer_Positions_Short_All",
                    "Dealer_Positions_Spread_All",
                ),
                Category(
                    "asset_manager",
                    "Asset manager / institutional",
                    "Asset_Mgr_Positions_Long_All",
                    "Asset_Mgr_Positions_Short_All",
                    "Asset_Mgr_Positions_Spread_All",
                ),
                Category(
                    "leveraged_funds",
                    "Leveraged funds",
                    "Lev_Money_Positions_Long_All",
                    "Lev_Money_Positions_Short_All",
                    "Lev_Money_Positions_Spread_All",
                ),
                Category(
                    "other_reportable",
                    "Other reportables",
                    "Other_Rept_Positions_Long_All",
                    "Other_Rept_Positions_Short_All",
                    "Other_Rept_Positions_Spread_All",
                ),
                _nonreportable("NonRept_Positions_Long_All", "NonRept_Positions_Short_All"),
            ),
        ),
        Report(
            name=DISAGGREGATED,
            label="Disaggregated (futures only)",
            first_day=date(2006, 6, 13),
            year_file="fut_disagg_txt_{year}.zip",
            history_file="fut_disagg_txt_hist_2006_2016.zip",
            columns=191,
            name_column="Market_and_Exchange_Names",
            yymmdd_column="As_of_Date_In_Form_YYMMDD",
            date_column="Report_Date_as_YYYY-MM-DD",
            code_column="CFTC_Contract_Market_Code",
            open_interest_column="Open_Interest_All",
            units_column="Contract_Units",
            categories=(
                Category(
                    "producer_merchant",
                    "Producer / merchant / processor / user",
                    "Prod_Merc_Positions_Long_All",
                    "Prod_Merc_Positions_Short_All",
                ),
                Category(
                    "swap_dealer",
                    "Swap dealers",
                    "Swap_Positions_Long_All",
                    # The CFTC writes these two with a double underscore.
                    "Swap__Positions_Short_All",
                    "Swap__Positions_Spread_All",
                ),
                Category(
                    "managed_money",
                    "Managed money",
                    "M_Money_Positions_Long_All",
                    "M_Money_Positions_Short_All",
                    "M_Money_Positions_Spread_All",
                ),
                Category(
                    "other_reportable",
                    "Other reportables",
                    "Other_Rept_Positions_Long_All",
                    "Other_Rept_Positions_Short_All",
                    "Other_Rept_Positions_Spread_All",
                ),
                _nonreportable("NonRept_Positions_Long_All", "NonRept_Positions_Short_All"),
            ),
        ),
    )
}


@dataclass(frozen=True)
class Position:
    """The positions of one type of trader, in contracts. ``spreading`` is absent for some."""

    category: str
    long: int
    short: int
    spreading: int | None

    @property
    def net(self) -> int:
        return self.long - self.short


@dataclass(frozen=True)
class MarketWeek:
    """One line of a report: a market on a report date."""

    code: str
    report_date: date
    name: str
    contract_units: str | None
    open_interest: int
    positions: tuple[Position, ...]


@dataclass(frozen=True)
class CotFile:
    """A file of the CFTC, read: its lines ordered by report date, then market code."""

    report: str
    file: str
    first_year: int
    last_year: int
    weeks: tuple[MarketWeek, ...]

    @property
    def report_dates(self) -> tuple[date, ...]:
        return tuple(sorted({week.report_date for week in self.weeks}))


def _contracts(cell: str, where: str, *, may_be_negative: bool = False) -> int:
    """A number of contracts: ``"  506774"``, or ``"22364.000000"`` in the TFF history."""
    number = parse_number(cell, decimal=".")
    if number is None or number != int(number):
        raise CftcError(f"{where}: {cell.strip()!r} is not a number of contracts")
    if number < 0 and not may_be_negative:
        raise CftcError(f"{where}: {cell.strip()!r} is negative")
    return int(number)


def _report_date(cell: str, yymmdd: str, where: str) -> date:
    text = cell.strip()
    try:
        if "/" in text:
            day = datetime.strptime(text, "%m/%d/%Y %I:%M:%S %p").date()
        else:
            day = date.fromisoformat(text)
    except ValueError as error:
        raise CftcError(f"{where}: {text!r} is not a report date") from error
    if day.strftime("%y%m%d") != yymmdd.strip():
        raise CftcError(f"{where}: {text!r} and {yymmdd.strip()!r} are not the same day")
    return day


def _member(content: bytes) -> tuple[zipfile.ZipFile, str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise CftcError("the answer is not a zip file") from error
    names = archive.namelist()
    if len(names) != 1 or not names[0].lower().endswith(".txt"):
        archive.close()
        raise CftcError(f"expected one .txt file in the zip, found {names}")
    return archive, names[0]


def _columns(report: Report, header: list[str]) -> dict[str, int]:
    if len(header) != report.columns:
        raise CftcError(
            f"the {report.name} file has {len(header)} columns, not {report.columns}: "
            "its layout changed"
        )
    index = {}
    for position, column in enumerate(header):
        if column in index:
            raise CftcError(f"the column {column!r} is given twice")
        index[column] = position
    needed = [
        report.name_column,
        report.yymmdd_column,
        report.date_column,
        report.code_column,
        report.open_interest_column,
        report.units_column,
    ]
    for category in report.categories:
        needed += [category.long, category.short]
        if category.spreading:
            needed.append(category.spreading)
    missing = [column for column in needed if column not in index]
    if missing:
        raise CftcError(f"the {report.name} file has no column {missing}")
    return index


def _check_totals(report: Report, week: MarketWeek, where: str) -> None:
    """Long positions, and short ones, add up to the open interest."""
    spreading = sum(position.spreading or 0 for position in week.positions)
    longs = sum(position.long for position in week.positions) + spreading
    shorts = sum(position.short for position in week.positions) + spreading
    tolerance = CONSOLIDATED_TOLERANCE if week.code.endswith("+") else 0
    for side, total in (("long", longs), ("short", shorts)):
        if abs(total - week.open_interest) > tolerance:
            raise CftcError(
                f"{where}: the {side} positions of {week.code} add up to {total}, "
                f"not to the open interest {week.open_interest}"
            )


def parse_cot_zip(content: bytes, report_name: str, year: int) -> CotFile:
    """Read the zip of a report holding ``year`` (the history file up to 2016)."""
    report = REPORTS.get(report_name)
    if report is None:
        raise CftcError(f"unknown report {report_name!r}")
    first_year, last_year = report.years_of_file(year)
    file_name = report.file_name(year)
    archive, member = _member(content)
    weeks: dict[tuple[date, str], MarketWeek] = {}
    with archive, archive.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        reader = csv.reader(text)
        try:
            header = next(reader)
        except StopIteration as error:
            raise CftcError(f"{member} is empty") from error
        except UnicodeDecodeError as error:
            raise CftcError(f"{member} is not UTF-8 text") from error
        index = _columns(report, header)
        try:
            for line, row in enumerate(reader, start=2):
                if not row:
                    continue
                where = f"{member}, line {line}"
                if len(row) != len(header):
                    raise CftcError(f"{where}: {len(row)} cells for {len(header)} columns")
                week = _week(report, index, row, where)
                if not first_year <= week.report_date.year <= last_year:
                    raise CftcError(
                        f"{where}: {week.report_date} is not in the file of "
                        f"{first_year}–{last_year}"
                    )
                key = (week.report_date, week.code)
                if key in weeks:
                    raise CftcError(f"{where}: {week.code} on {week.report_date} is given twice")
                _check_totals(report, week, where)
                weeks[key] = week
        except UnicodeDecodeError as error:
            raise CftcError(f"{member} is not UTF-8 text") from error
    return CotFile(
        report.name, file_name, first_year, last_year, tuple(weeks[key] for key in sorted(weeks))
    )


def _week(report: Report, index: dict[str, int], row: list[str], where: str) -> MarketWeek:
    def cell(column: str) -> str:
        return row[index[column]]

    code = cell(report.code_column).strip()
    if not _CODE.fullmatch(code):
        raise CftcError(f"{where}: {code!r} is not a CFTC contract market code")
    positions = []
    for category in report.categories:
        here = f"{where}, {category.code}"
        negative = category.may_be_negative
        positions.append(
            Position(
                category.code,
                _contracts(cell(category.long), here, may_be_negative=negative),
                _contracts(cell(category.short), here, may_be_negative=negative),
                _contracts(cell(category.spreading), here) if category.spreading else None,
            )
        )
    name = " ".join(cell(report.name_column).split())
    if not name:
        raise CftcError(f"{where}: the market {code} has no name")
    units = " ".join(cell(report.units_column).split()) or None
    return MarketWeek(
        code=code,
        report_date=_report_date(cell(report.date_column), cell(report.yymmdd_column), where),
        name=name,
        contract_units=units,
        open_interest=_contracts(cell(report.open_interest_column), f"{where}, open interest"),
        positions=tuple(positions),
    )


class CftcProvider(BaseFinancialProvider):
    """The Commitments of Traders files of the CFTC, one file at a time."""

    name = "CFTC"
    # The Legacy history is a zip of 23 MB.
    timeout = 120.0

    async def report_file(self, report_name: str, year: int) -> CotFile | None:
        """The file holding a year of a report, read; ``None`` when the CFTC did not give it.

        A year up to 2016 gives the whole history file (``first_year`` to 2016). A file
        that cannot be read raises ``CftcError``: nothing of it is used.
        """
        report = REPORTS.get(report_name)
        if report is None:
            raise CftcError(f"unknown report {report_name!r}")
        url = report.url(year)
        try:
            async with self._session() as client:
                response = await client.get(
                    url, headers=lambda: self._get_headers({"Accept": "application/zip,*/*"})
                )
        except httpx.HTTPError as error:
            logger.warning("[%s] GET %s failed: %s", self.name, url, error)
            return None
        if response.status_code != 200:
            logger.warning("[%s] GET %s → %d", self.name, url, response.status_code)
            return None
        # Reading the Legacy history takes seconds: not on the event loop.
        return await run_sync(parse_cot_zip, response.content, report.name, year)
