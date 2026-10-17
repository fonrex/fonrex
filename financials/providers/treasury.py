"""TreasuryProvider — the daily yield curves of the US Treasury.

Source: https://home.treasury.gov (official, free, no key). The Treasury publishes
one CSV file per year and curve::

    https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
        daily-treasury-rates.csv/{year}/all?type={type}&field_tdr_date_value={year}&page&_format=csv

* ``daily_treasury_yield_curve``: the par yield curve (nominal), since 1990;
* ``daily_treasury_real_yield_curve``: the par real yield curve (TIPS), since 2003.

One row per business day, the latest first: ``Date`` (``MM/DD/YYYY``), then one
column per maturity, in percent with two decimals. The columns change over the
years (``2 Mo`` from October 2018, ``4 Mo`` from October 2022, ``1.5 Month`` from
February 2025; no ``30 Yr`` from 2002 to 2006), and a maturity not published on a
day is an empty cell. Real yields are often negative, nominal ones reach zero: a
zero is a value, an empty cell is not. A year without data answers an empty
body. The file of every year at once is refused (HTTP 403): the history is read
one year at a time.

The rates are kept as ratios (``5.09`` becomes ``0.0509``); a column the Treasury
adds is refused, never ignored, so that a change of the files is seen.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from financials.providers.base import BaseFinancialProvider

logger = logging.getLogger(__name__)

TREASURY_CSV_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type={type}&field_tdr_date_value={year}&page&_format=csv"
)
SOURCE = "US Treasury, Daily Treasury Par Yield Curve Rates (https://home.treasury.gov)"

NOMINAL = "nominal"
REAL = "real"

# A rate of the files is a percent of a few units; beyond this the file is not read.
_LARGEST_PERCENT = Decimal(25)
_HUNDRED = Decimal(100)


class TreasuryError(ValueError):
    """A file of the Treasury could not be read: nothing of it is kept."""


@dataclass(frozen=True)
class Curve:
    """A curve of the Treasury: its ``type`` in the URL, its first year and its maturities."""

    name: str
    label: str
    treasury_type: str
    first_year: int
    tenors: tuple[str, ...]

    def url(self, year: int) -> str:
        return TREASURY_CSV_URL.format(year=year, type=self.treasury_type)


@dataclass(frozen=True)
class Tenor:
    """A maturity: its code in Fonrex, its length in months and the label of the Treasury."""

    code: str
    months: Decimal
    label: str


TENORS: tuple[Tenor, ...] = (
    Tenor("1M", Decimal(1), "1 Mo"),
    Tenor("6W", Decimal("1.5"), "1.5 Month"),
    Tenor("2M", Decimal(2), "2 Mo"),
    Tenor("3M", Decimal(3), "3 Mo"),
    Tenor("4M", Decimal(4), "4 Mo"),
    Tenor("6M", Decimal(6), "6 Mo"),
    Tenor("1Y", Decimal(12), "1 Yr"),
    Tenor("2Y", Decimal(24), "2 Yr"),
    Tenor("3Y", Decimal(36), "3 Yr"),
    Tenor("5Y", Decimal(60), "5 Yr"),
    Tenor("7Y", Decimal(84), "7 Yr"),
    Tenor("10Y", Decimal(120), "10 Yr"),
    Tenor("20Y", Decimal(240), "20 Yr"),
    Tenor("30Y", Decimal(360), "30 Yr"),
)
TENORS_BY_CODE: dict[str, Tenor] = {tenor.code: tenor for tenor in TENORS}

CURVES: dict[str, Curve] = {
    curve.name: curve
    for curve in (
        Curve(
            NOMINAL,
            "Par yield curve (nominal)",
            "daily_treasury_yield_curve",
            1990,
            tuple(tenor.code for tenor in TENORS),
        ),
        Curve(
            REAL,
            "Par real yield curve (TIPS)",
            "daily_treasury_real_yield_curve",
            2003,
            ("5Y", "7Y", "10Y", "20Y", "30Y"),
        ),
    )
}


def _label_key(label: str) -> str:
    # ``10 Yr`` (nominal) and ``10 YR`` (real) name the same maturity.
    return " ".join(label.replace('"', " ").split()).lower()


_TENOR_OF_LABEL: dict[str, str] = {_label_key(tenor.label): tenor.code for tenor in TENORS}


def tenor_of(label: str) -> str:
    """The code of a column of the Treasury (``"10 Yr"`` is ``"10Y"``), or ``TreasuryError``."""
    code = _TENOR_OF_LABEL.get(_label_key(label))
    if code is None:
        raise TreasuryError(f"unknown maturity column {label!r}")
    return code


@dataclass(frozen=True)
class CurveDay:
    """The rates of one day, as ratios, by tenor code. A maturity not published is absent."""

    day: date
    points: dict[str, Decimal]


@dataclass(frozen=True)
class CurveFile:
    """A file of the Treasury, read: its maturities in the order of the file, its days oldest first."""

    curve: str
    year: int
    tenors: tuple[str, ...]
    days: tuple[CurveDay, ...]


def _rate(cell: str, where: str) -> Decimal | None:
    text = cell.strip()
    if not text:
        return None
    try:
        percent = Decimal(text)
    except InvalidOperation as error:
        raise TreasuryError(f"{where}: {text!r} is not a rate") from error
    if not percent.is_finite() or abs(percent) > _LARGEST_PERCENT:
        raise TreasuryError(f"{where}: {text!r} is not a rate in percent")
    return percent / _HUNDRED


def _day(cell: str, year: int, line: int) -> date:
    try:
        day = datetime.strptime(cell.strip(), "%m/%d/%Y").date()
    except ValueError as error:
        raise TreasuryError(f"line {line}: {cell!r} is not a date MM/DD/YYYY") from error
    if day.year != year:
        raise TreasuryError(f"line {line}: {day} is not in the file of {year}")
    return day


def parse_curve_csv(text: str, curve: str, year: int) -> CurveFile:
    """Read a file of the Treasury. An empty body is a year without data."""
    if curve not in CURVES:
        raise TreasuryError(f"unknown curve {curve!r}")
    if not text.strip():
        return CurveFile(curve, year, (), ())
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    header = rows[0]
    if not header or header[0].strip().lower() != "date":
        raise TreasuryError(f"the file does not start with a Date column: {header[:3]}")
    tenors = tuple(tenor_of(label) for label in header[1:])
    if len(set(tenors)) != len(tenors):
        raise TreasuryError(f"a maturity is given twice: {header}")
    days: dict[date, CurveDay] = {}
    for line, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        if len(row) != len(header):
            raise TreasuryError(f"line {line}: {len(row)} cells for {len(header)} columns")
        day = _day(row[0], year, line)
        if day in days:
            raise TreasuryError(f"line {line}: {day} is given twice")
        points = {}
        for tenor, cell in zip(tenors, row[1:], strict=True):
            rate = _rate(cell, f"line {line}, {tenor}")
            if rate is not None:
                points[tenor] = rate
        if points:
            days[day] = CurveDay(day, points)
    return CurveFile(curve, year, tenors, tuple(days[day] for day in sorted(days)))


class TreasuryProvider(BaseFinancialProvider):
    """The yield curve files of the US Treasury, one year at a time."""

    name = "Treasury"
    timeout = 30.0

    async def curve_year(self, curve: str, year: int) -> CurveFile | None:
        """The file of a year, read; ``None`` when the Treasury did not answer it.

        A file that cannot be read raises ``TreasuryError``: nothing of it is used.
        """
        if curve not in CURVES:
            raise TreasuryError(f"unknown curve {curve!r}")
        text = await self._get(CURVES[curve].url(year))
        if text is None:
            return None
        return parse_curve_csv(text, curve, year)
