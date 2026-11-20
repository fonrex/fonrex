"""OECDProvider — the 3-month interest rates of the OECD, every country at once.

Source: the SDMX API of the OECD (https://sdmx.oecd.org, official, free, no key).
The monthly financial indicators of the Short-Term Economic Statistics
(dataflow ``OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0``) give, for each country or
area, the 3-month interbank rate (measure ``IR3TIB``, in percent a year): one
request returns every country, the reference area left empty in the key::

    https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/
        .M.IR3TIB.PA._Z._Z._Z._Z.N?startPeriod=2026-01&dimensionAtObservation=AllDimensions

with ``Accept: application/vnd.sdmx.data+csv``: one row per country and month
(``REF_AREA`` such as ``CHE``, ``GBR`` or ``EA20`` for the euro area,
``TIME_PERIOD`` ``2026-10``, ``OBS_VALUE`` ``-0.0415``). A period without any
value answers HTTP 404 with ``NoRecordsFound``.

The OECD allows about 60 downloads an hour from one address, and answers 429
beyond: the provider asks once, without retrying, and the caller keeps the rates
it has. The rates are kept as ratios (``2.63`` becomes ``0.0263``); a row that
is not a monthly 3-month rate in percent is refused, never ignored, so that a
change of the dataflow is seen.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from financials.providers.base import BaseFinancialProvider

logger = logging.getLogger(__name__)

OECD_API_URL = "https://sdmx.oecd.org/public/rest/data"
DATAFLOW = "OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0"
MEASURE = "IR3TIB"
# Every reference area, monthly, the 3-month interbank rate in percent a year.
SERIES_KEY = f".M.{MEASURE}.PA._Z._Z._Z._Z.N"
CSV_MEDIA_TYPE = "application/vnd.sdmx.data+csv; charset=utf-8"
SOURCE = "OECD, Short-Term Economic Statistics, 3-month interbank rate (https://sdmx.oecd.org)"

# The columns read; the others (TIME_HORIZ, DECIMALS, BASE_PER...) are not used.
_COLUMNS = ("REF_AREA", "FREQ", "MEASURE", "UNIT_MEASURE", "TIME_PERIOD", "OBS_VALUE")
# A 3-month rate is a percent of a few units; beyond this the answer is not read.
_LARGEST_PERCENT = Decimal(100)
_HUNDRED = Decimal(100)


class OECDError(ValueError):
    """An answer of the OECD could not be read: nothing of it is kept."""


@dataclass(frozen=True)
class ShortRate:
    """The 3-month rate of a country for a month, as a ratio; the month is dated by its first day."""

    area: str
    period: date
    value: Decimal
    status: str | None = None


def short_rates_url() -> str:
    return f"{OECD_API_URL}/{DATAFLOW}/{SERIES_KEY}"


def _period(cell: str, line: int) -> date:
    try:
        year, month = (int(part) for part in cell.strip().split("-"))
        return date(year, month, 1)
    except ValueError as error:
        raise OECDError(f"line {line}: {cell!r} is not a month YYYY-MM") from error


def _rate(cell: str, line: int) -> Decimal | None:
    text = cell.strip()
    if not text:
        return None
    try:
        percent = Decimal(text)
    except InvalidOperation as error:
        raise OECDError(f"line {line}: {text!r} is not a rate") from error
    if not percent.is_finite() or abs(percent) > _LARGEST_PERCENT:
        raise OECDError(f"line {line}: {text!r} is not a rate in percent")
    return percent / _HUNDRED


def parse_short_rates_csv(text: str) -> list[ShortRate]:
    """Read an SDMX-CSV answer of the OECD: the rates, by country then month."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    header = [cell.strip() for cell in rows[0]] if rows else []
    missing = [column for column in _COLUMNS if column not in header]
    if missing:
        raise OECDError(f"the answer has no column {', '.join(missing)}: {header[:4]}")
    at = {column: header.index(column) for column in header}
    rates: dict[tuple[str, date], ShortRate] = {}
    for line, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        if len(row) != len(header):
            raise OECDError(f"line {line}: {len(row)} cells for {len(header)} columns")
        kind = (row[at["FREQ"]], row[at["MEASURE"]], row[at["UNIT_MEASURE"]])
        if kind != ("M", MEASURE, "PA"):
            raise OECDError(f"line {line}: {'/'.join(kind)} is not a monthly 3-month rate")
        if "UNIT_MULT" in at and row[at["UNIT_MULT"]].strip() not in ("", "0"):
            raise OECDError(f"line {line}: unit multiplier {row[at['UNIT_MULT']]!r}")
        area = row[at["REF_AREA"]].strip()
        if not area:
            raise OECDError(f"line {line}: no reference area")
        period = _period(row[at["TIME_PERIOD"]], line)
        value = _rate(row[at["OBS_VALUE"]], line)
        if value is None:
            continue
        if (area, period) in rates:
            raise OECDError(f"line {line}: {area} {period:%Y-%m} is given twice")
        status = (row[at["OBS_STATUS"]].strip() or None) if "OBS_STATUS" in at else None
        rates[(area, period)] = ShortRate(area, period, value, status)
    return [rates[key] for key in sorted(rates)]


class OECDProvider(BaseFinancialProvider):
    """The 3-month rates of every country the OECD publishes, in one request."""

    name = "OECD"
    timeout = 60.0
    # About 60 downloads an hour: a refusal (429) is not asked again.
    max_retries = 1

    async def short_rates(self, start: date) -> list[ShortRate] | None:
        """The rates of every country from the month of ``start``; ``None`` when the OECD
        did not answer (refused, over its limit, or no value for the period).

        An answer that cannot be read raises ``OECDError``: nothing of it is used.
        """
        text = await self._get(
            short_rates_url(),
            headers={"Accept": CSV_MEDIA_TYPE},
            params={
                "startPeriod": f"{start:%Y-%m}",
                "dimensionAtObservation": "AllDimensions",
            },
        )
        if text is None:
            return None
        return parse_short_rates_csv(text)
