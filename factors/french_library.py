"""Factor returns of the Kenneth R. French Data Library.

The library publishes each dataset as a zipped CSV
(``{FRENCH_LIBRARY_URL}/{file}_CSV.zip``): a few lines of notes, then a header
(``,Mkt-RF,SMB,HML,RF``) and one row per period (``192607`` for a month,
``19260701`` for a day), then a second table of annual factors that is not read.
Returns are given in percent; ``-99.99`` or ``-999`` mark a missing value.

Conventions to keep in mind when using them:

* every return is in **US dollars**, the international datasets included;
* ``RF`` is the US one-month Treasury bill rate, for every region;
* the market factor is ``Mkt-RF``, the excess return of the region's
  value-weighted market;
* the library is updated about once a month and past values may be revised.

This module only reads: it knows the datasets, downloads a file and parses it.
Storing the series is the job of :mod:`factors.store`.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx

FRENCH_LIBRARY_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"

Frequency = Literal["monthly", "daily"]
FREQUENCIES: tuple[Frequency, ...] = ("monthly", "daily")

# Stored as ratios with eight decimals (factor_returns.value is NUMERIC(12, 8)).
_PRECISION = Decimal("0.00000001")
_HUNDRED = Decimal("100")
_MISSING = {Decimal("-99.99"), Decimal("-999")}

# Column names of the library -> names stored by Fonrex.
FACTOR_NAMES = {
    "MKT-RF": "MKT_RF",
    "SMB": "SMB",
    "HML": "HML",
    "RMW": "RMW",
    "CMA": "CMA",
    "RF": "RF",
    "MOM": "MOM",  # US momentum
    "WML": "MOM",  # international momentum (winners minus losers)
}


@dataclass(frozen=True)
class FactorDataset:
    """A dataset of the library: its region, its factors and its two files."""

    name: str
    region: str
    label: str
    factors: tuple[str, ...]
    files: dict[str, str] = field(hash=False)

    def file_name(self, frequency: Frequency) -> str:
        return self.files[frequency]


def _dataset(name, region, label, factors, monthly, daily) -> FactorDataset:
    return FactorDataset(
        name, region, label, tuple(factors.split()), {"monthly": monthly, "daily": daily}
    )


DATASETS: dict[str, FactorDataset] = {
    dataset.name: dataset
    for dataset in (
        _dataset(
            "us_3",
            "us",
            "US Fama/French 3 factors",
            "MKT_RF SMB HML RF",
            "F-F_Research_Data_Factors",
            "F-F_Research_Data_Factors_daily",
        ),
        _dataset(
            "us_5",
            "us",
            "US Fama/French 5 factors (2x3)",
            "MKT_RF SMB HML RMW CMA RF",
            "F-F_Research_Data_5_Factors_2x3",
            "F-F_Research_Data_5_Factors_2x3_daily",
        ),
        _dataset(
            "us_mom",
            "us",
            "US momentum factor",
            "MOM",
            "F-F_Momentum_Factor",
            "F-F_Momentum_Factor_daily",
        ),
        _dataset(
            "europe_3",
            "europe",
            "Europe Fama/French 3 factors",
            "MKT_RF SMB HML RF",
            "Europe_3_Factors",
            "Europe_3_Factors_Daily",
        ),
        _dataset(
            "europe_5",
            "europe",
            "Europe Fama/French 5 factors",
            "MKT_RF SMB HML RMW CMA RF",
            "Europe_5_Factors",
            "Europe_5_Factors_Daily",
        ),
        _dataset(
            "europe_mom",
            "europe",
            "Europe momentum factor",
            "MOM",
            "Europe_Mom_Factor",
            "Europe_Mom_Factor_Daily",
        ),
        _dataset(
            "developed_3",
            "developed",
            "Developed markets Fama/French 3 factors",
            "MKT_RF SMB HML RF",
            "Developed_3_Factors",
            "Developed_3_Factors_Daily",
        ),
        _dataset(
            "developed_5",
            "developed",
            "Developed markets Fama/French 5 factors",
            "MKT_RF SMB HML RMW CMA RF",
            "Developed_5_Factors",
            "Developed_5_Factors_Daily",
        ),
        _dataset(
            "developed_mom",
            "developed",
            "Developed markets momentum factor",
            "MOM",
            "Developed_Mom_Factor",
            "Developed_Mom_Factor_Daily",
        ),
    )
}


class FactorFileError(ValueError):
    """A file of the library could not be read: nothing is stored from it."""


@dataclass(frozen=True)
class FactorTable:
    """The periods of one file: ``rows`` maps each period end to its factor values."""

    factors: tuple[str, ...]
    rows: dict[date, dict[str, Decimal]]
    note: str | None = None

    @property
    def first_period(self) -> date | None:
        return min(self.rows) if self.rows else None

    @property
    def last_period(self) -> date | None:
        return max(self.rows) if self.rows else None


def library_url() -> str:
    """Base address of the library; ``FRENCH_LIBRARY_URL`` replaces it (a mirror)."""
    return (os.environ.get("FRENCH_LIBRARY_URL") or FRENCH_LIBRARY_URL).strip().rstrip("/")


def file_url(dataset: str, frequency: Frequency, base_url: str | None = None) -> str:
    return f"{base_url or library_url()}/{DATASETS[dataset].file_name(frequency)}_CSV.zip"


def _period_end(raw: str, frequency: Frequency) -> date | None:
    """``192607`` -> 1926-07-31 (monthly), ``19260701`` -> 1926-07-01 (daily)."""
    digits = 6 if frequency == "monthly" else 8
    if len(raw) != digits or not raw.isdigit():
        return None
    year, month = int(raw[:4]), int(raw[4:6])
    try:
        if frequency == "monthly":
            return date(year, month, monthrange(year, month)[1])
        return date(year, month, int(raw[6:]))
    except ValueError:
        return None


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.split(",")]


_NOTE = re.compile(r"using the (\d{6}) (\w+) database", re.IGNORECASE)


def parse_factor_csv(text: str, frequency: Frequency) -> FactorTable:
    """The table of periods of a library CSV, values as ratios (``2.89`` -> ``0.0289``)."""
    lines = text.splitlines()
    header_at = None
    for index, line in enumerate(lines):
        cells = _cells(line)
        if len(cells) > 1 and cells[0] == "" and all(cells[1:]):
            names = [FACTOR_NAMES.get(cell.upper()) for cell in cells[1:]]
            if all(names):
                header_at = index
                factors = tuple(names)
                break
    if header_at is None:
        raise FactorFileError("no header of factors (',Mkt-RF,SMB,...') in the file")

    rows: dict[date, dict[str, Decimal]] = {}
    for line in lines[header_at + 1 :]:
        cells = _cells(line)
        period = _period_end(cells[0], frequency)
        if period is None:
            break  # the table of periods ends: blank line or the annual factors
        if len(cells) != len(factors) + 1:
            raise FactorFileError(f"row {cells[0]} has {len(cells) - 1} values, not {len(factors)}")
        values = {}
        for name, raw in zip(factors, cells[1:], strict=True):
            try:
                value = Decimal(raw)
            except InvalidOperation as error:
                raise FactorFileError(f"row {cells[0]}: {raw!r} is not a number") from error
            if value in _MISSING or not value.is_finite():
                continue
            values[name] = (value / _HUNDRED).quantize(_PRECISION)
        if values:
            rows[period] = values
    if not rows:
        raise FactorFileError("the table of periods is empty")

    match = _NOTE.search(text)
    note = f"{match.group(2)} {match.group(1)}" if match else None
    return FactorTable(factors=factors, rows=rows, note=note)


def read_zip(content: bytes) -> str:
    """The text of the single CSV of a library zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise FactorFileError(f"expected one CSV in the zip, found {names}")
            return archive.read(names[0]).decode("latin-1")
    except zipfile.BadZipFile as error:
        raise FactorFileError("the answer is not a zip file") from error


async def download_factor_table(
    dataset: str, frequency: Frequency, base_url: str | None = None
) -> FactorTable:
    """Download and parse one file of the library (raises on any failure)."""
    url = file_url(dataset, frequency, base_url)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
    table = parse_factor_csv(read_zip(response.content), frequency)
    expected = set(DATASETS[dataset].factors)
    if set(table.factors) != expected:
        raise FactorFileError(
            f"{url} gives the factors {', '.join(table.factors)}, not {', '.join(sorted(expected))}"
        )
    return table
