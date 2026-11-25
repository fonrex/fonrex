"""BankOfEnglandProvider — the SONIA of the Bank of England, month by month.

Source: the Interactive Statistical Database of the Bank of England
(https://www.bankofengland.co.uk/boeapps/database, official, free, no key). The
Bank administers SONIA, the overnight rate of the sterling market, and publishes
the mean of each month as series ``IUMASOIA`` (percent a year, from January 1997)::

    https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp
        ?csv.x=yes&Datefrom=01/Jan/2026&Dateto=now&SeriesCodes=IUMASOIA
        &CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N

answers a CSV of two columns, a month dated by its last day::

    DATE,IUMASOIA
    31 Oct 2026,3.7333

A period without any value answers the header alone; an unknown series answers
HTTP 200 with an HTML page titled ``Error``. The OECD stopped its 3-month rate of
the United Kingdom in early 2026: SONIA is the risk-free rate of the pound.

The rates are kept as ratios (``3.7333`` becomes ``0.037333``), the month dated
by its first day as the OECD rates are. An answer that is not this CSV is
refused, never ignored, so that a change of the database is seen.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from financials.providers.base import BaseFinancialProvider
from financials.providers.oecd import ShortRate

logger = logging.getLogger(__name__)

BOE_API_URL = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
SERIES = "IUMASOIA"  # monthly mean of SONIA, percent a year
# The rates of the pound are stored under the area of the United Kingdom.
AREA = "GBR"
SOURCE = "Bank of England, SONIA, monthly mean (https://www.bankofengland.co.uk)"

# SONIA is a percent of a few units; beyond this the answer is not read.
_LARGEST_PERCENT = Decimal(100)
_HUNDRED = Decimal(100)


class BoEError(ValueError):
    """An answer of the Bank of England could not be read: nothing of it is kept."""


def sonia_params(start: date) -> dict[str, str]:
    """The query of the monthly SONIA from the month of ``start`` to the last one."""
    return {
        "csv.x": "yes",
        "Datefrom": f"01/{start:%b/%Y}",
        "Dateto": "now",
        "SeriesCodes": SERIES,
        "CSVF": "TN",  # tabular, no titles: the header is DATE and the series code
        "UsingCodes": "Y",
        "VPD": "Y",  # the provisional data too
        "VFD": "N",  # no footnotes
    }


def _month(cell: str, line: int) -> date:
    try:
        day = datetime.strptime(cell.strip(), "%d %b %Y").date()
    except ValueError as error:
        raise BoEError(f"line {line}: {cell!r} is not a day such as 31 Oct 2026") from error
    return day.replace(day=1)


def _rate(cell: str, line: int) -> Decimal | None:
    text = cell.strip()
    if not text:
        return None
    try:
        percent = Decimal(text)
    except InvalidOperation as error:
        raise BoEError(f"line {line}: {text!r} is not a rate") from error
    if not percent.is_finite() or abs(percent) > _LARGEST_PERCENT:
        raise BoEError(f"line {line}: {text!r} is not a rate in percent")
    return percent / _HUNDRED


def parse_sonia_csv(text: str) -> list[ShortRate]:
    """Read a CSV answer of the Bank of England: the monthly SONIA, oldest month first."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    header = [cell.strip() for cell in rows[0]] if rows else []
    if header != ["DATE", SERIES]:
        shown = text.strip()[:60].replace("\r", " ").replace("\n", " ")
        raise BoEError(f"the answer is not the CSV of {SERIES}: {shown!r}")
    rates: dict[date, ShortRate] = {}
    for line, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        if len(row) != 2:
            raise BoEError(f"line {line}: {len(row)} cells for 2 columns")
        month = _month(row[0], line)
        value = _rate(row[1], line)
        if value is None:
            continue
        if month in rates:
            raise BoEError(f"line {line}: {month:%Y-%m} is given twice")
        rates[month] = ShortRate(AREA, month, value)
    return [rates[month] for month in sorted(rates)]


class BankOfEnglandProvider(BaseFinancialProvider):
    """The monthly SONIA of the Bank of England, in one request."""

    name = "BankOfEngland"
    timeout = 60.0

    async def sonia_monthly(self, start: date) -> list[ShortRate] | None:
        """The monthly means of SONIA from the month of ``start``; ``None`` when the Bank
        did not answer. An answer that cannot be read raises ``BoEError``."""
        text = await self._get(BOE_API_URL, params=sonia_params(start))
        if text is None:
            return None
        return parse_sonia_csv(text)
