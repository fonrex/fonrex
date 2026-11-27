"""The quarters Yahoo does not keep, read from stockanalysis.com.

``/fundamental/deep`` serves whatever ``financial_statements`` holds. Yahoo hands over
five or six quarters for a European paper and eight for a US one; stockanalysis.com
publishes twenty - five years - in one plain table, without a key and without a quota.
A trailing-twelve-month comparison needs eight, because it puts one window against the
window before it, and a growth trend over years needs all twenty.

What is used here
-----------------
* ``StockAnalysisQuarters.quarters(ticker)`` - the quarterly income statement, newest
  first, as the page displays it.
* ``backfill`` - writes the periods the database does not have yet and returns how many
  were added.

What is deliberately not done
-----------------------------
* **No provider registration.** A fundamentals provider answers with one snapshot of
  ratios (``FinancialMetrics``); what these pages carry is a series. Registering it
  there would add a provider to every ``/fundamental`` call and still deliver no quarter.
* **No overwriting.** A period the database already holds stays untouched: the Yahoo
  enrichment and this source may round differently, and a row that keeps its own source
  is worth more than a row stitched from two.
* **No guessing.** A scale the page does not state, a row that does not line up with its
  dates, a period end without a year: the page is refused rather than read on hope.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from concurrency import run_sync
from financials.enrichment.statement_store import insert_rows
from financials.numbers import parse_number
from financials.providers.base import BaseFinancialProvider
from settings import env_int

if TYPE_CHECKING:
    from database.service import DatabaseService

logger = logging.getLogger(__name__)

BASE = "https://stockanalysis.com"

#: Ticker suffix → exchange path on the site. The empty suffix is a US listing.
#: Checked against the site before these were written down: every one of them answers
#: with the income table, and a paper without one carries no second source at all.
EXCHANGES = {
    ".DE": "etr",
    ".F": "fra",
    ".PA": "epa",
    ".AS": "ams",
    ".BR": "bru",
    ".LS": "lis",
    ".MC": "bme",
    ".MI": "bit",
    ".L": "lon",
    ".SW": "swx",
    ".VI": "vie",
    ".ST": "sto",
    ".CO": "cph",
    ".OL": "osl",
    ".HE": "hel",
    ".IR": "ise",
    ".WA": "wse",
    ".PR": "prg",
    ".HK": "hkg",
    ".T": "tyo",
    ".SS": "sha",
    ".SZ": "she",
    ".TO": "tsx",
    ".AX": "asx",
}
US = ""

#: Row label on the page → column in ``financial_statements``, with the scale that is
#: applied to the published figure. Per-share figures are published as they are.
FIELDS: dict[str, tuple[str, str]] = {
    "Revenue": ("revenue", "scaled"),
    "Gross Profit": ("gross_profit", "scaled"),
    "Operating Income": ("operating_income", "scaled"),
    "EBITDA": ("ebitda", "scaled"),
    "EBIT": ("ebit", "scaled"),
    "Net Income": ("net_income", "scaled"),
    "EPS (Basic)": ("eps_basic", "as_is"),
    "EPS (Diluted)": ("eps_diluted", "as_is"),
    "Shares Outstanding (Basic)": ("shares_basic", "scaled"),
    "Shares Outstanding (Diluted)": ("shares_diluted", "scaled"),
}
#: Columns that hold whole share counts, published in the same millions as the figures.
WHOLE_NUMBER_COLUMNS = frozenset({"shares_basic", "shares_diluted"})

#: "Financials in millions EUR." - the scale of the table, and the currency where the page
#: names one. A page without the scale line is refused: the figures would be written a
#: thousand times too large or too small, and a backfill never revisits them.
SCALE_TEXT = re.compile(r"Financials in (millions|thousands)\b")
CURRENCY_TEXT = re.compile(r"Financials in (?:millions|thousands)\s+([A-Z]{3})\b")
SCALES = {"millions": Decimal(1_000_000), "thousands": Decimal(1_000)}
#: "Sep '26 Sep 30, 2026" - the cell carries the short and the long date; the long one counts.
PERIOD_TEXT = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")
MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}

#: Name in logs: a constant, so a test that replaces the class does not also rename the log.
PROVIDER_NAME = "StockAnalysis"

#: How many quarters the table should hold before the source is asked at all.
MIN_QUARTERS_ENV_VAR = "STOCKANALYSIS_MIN_QUARTERS"
DEFAULT_MIN_QUARTERS = 20

#: The page weighs about 280 kB. A page far beyond that is refused before it is parsed, so a
#: redirect to some other document is not read as a statement.
MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Page:
    """One income statement page: its scale, its currency and its quarters."""

    multiplier: Decimal | None
    currency: str | None
    periods: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.periods)


def multiplier_and_currency(page: str) -> tuple[Decimal | None, str | None]:
    """The scale the page announces and the currency it names, if any.

    ``(None, None)`` when the page does not state its scale. There is no default: a table
    published in thousands, read as millions, makes the revenue a thousand times too large,
    and a backfill never revisits a period it has written.
    """
    match = SCALE_TEXT.search(page)
    if match is None:
        return None, None
    currency = CURRENCY_TEXT.search(page)
    return SCALES[match.group(1)], currency.group(1) if currency else None


def figure(text: str) -> Decimal | None:
    """A table cell as a figure, read by the one number reader of the project.

    ``parse_number`` knows what a cell can carry - a thousands separator, brackets meaning a
    negative, a dash meaning no figure, a currency code that is not a scale - and its rules are
    written once for every page this project scrapes. A second reader here would drift from it.
    """
    value = parse_number(text, decimal=".")
    if value is None:
        return None
    # The reader answers with a float; the column expects the exact figure the page displays.
    return Decimal(str(value))


def period_end_of(cell: str) -> date | None:
    """The period end of a "Period Ending" cell, or None when it carries no full date."""
    match = PERIOD_TEXT.search(html_module.unescape(cell or ""))
    if match is None or match.group(1) not in MONTHS:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(1)], int(match.group(2)))
    except ValueError:
        return None


def _cells(row: str) -> list[str]:
    found = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in found]


def parse(page: str) -> Page:
    """Read the quarters of an income statement page, newest first.

    The page has two tables: the statement itself and a block of ratios that repeats the
    quarter columns. Both start with a "Period Ending" row, so a row is attached to the
    dates above it - never to the dates of the other block. A row whose cell count does
    not match those dates is refused: a column read one place off would put one quarter's
    figures under another's date without ever raising.
    """
    rows = [_cells(row) for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S)]
    rows = [row for row in rows if row]
    multiplier, currency = multiplier_and_currency(page)
    if multiplier is None or not rows:
        return Page(multiplier, currency)

    ends: list[date | None] = []
    seen: dict[date, dict[str, str]] = {}
    for row in rows:
        label = html_module.unescape(row[0]).strip()
        cells = row[1:]
        if label == "Period Ending":
            ends = [period_end_of(cell) for cell in cells]
            for end in ends:
                if end is not None:
                    seen.setdefault(end, {})
            continue
        if label not in FIELDS or not ends:
            continue
        if len(cells) != len(ends):
            logger.debug(
                "stockanalysis: row %r has %d cells for %d dates", label, len(cells), len(ends)
            )
            continue
        for end, cell in zip(ends, cells, strict=True):
            if end is not None:
                seen[end].setdefault(label, cell)

    periods = []
    for end in sorted(seen, reverse=True):
        values: dict[str, Any] = {}
        for label, text in seen[end].items():
            column, scale = FIELDS[label]
            value = figure(text)
            if value is None:
                continue
            if scale == "scaled":
                value *= multiplier
            if column in WHOLE_NUMBER_COLUMNS:
                value = Decimal(int(value))
            values[column] = value
        if "revenue" not in values and "eps_diluted" not in values:
            continue
        periods.append({"period_end": end, **values})
    return Page(multiplier, currency, periods)


def url_for(ticker: str) -> str | None:
    """The quarterly income statement page of a ticker, or None for a suffix we do not map."""
    upper = ticker.upper()
    stem, _, suffix = upper.partition(".")
    if not suffix:
        return f"{BASE}/stocks/{stem.lower()}/financials/income-statement/?p=quarterly"
    exchange = EXCHANGES.get("." + suffix)
    if exchange is None:
        return None
    return f"{BASE}/quote/{exchange}/{stem}/financials/income-statement/?p=quarterly"


class StockAnalysisQuarters(BaseFinancialProvider):
    """Reads the quarterly income statement of one listing.

    Extends the common base for one reason: the HTTP policy of this project - retries,
    User-Agent rotation, the limiter, the optional proxy - must apply here too, and the
    base module is the only place allowed to build a client. It is not a registered
    fundamentals provider: no ``/fundamental`` call reaches it.
    """

    name = PROVIDER_NAME
    timeout = 15.0
    max_retries = 2

    async def quarters(self, ticker: str) -> Page | None:
        """The page of a ticker, or None when it cannot be read or carries no quarter."""
        url = url_for(ticker)
        if url is None:
            logger.debug("[%s] no exchange path for %s", self.name, ticker)
            return None
        body = await self._get(url)
        if body is None:
            return None
        if len(body) > MAX_BYTES:
            logger.warning(
                "[%s] page of %s is larger than %d bytes, refused", self.name, ticker, MAX_BYTES
            )
            return None
        page = parse(body)
        if page.multiplier is None:
            # The page does not say whether the table is in millions or thousands. Writing its
            # figures on a guess would persist a wrong scale that no later run corrects.
            logger.warning("[%s] %s: the page states no scale, refused", self.name, ticker)
            return None
        if not page.periods:
            logger.warning("[%s] %s: the table carries no readable quarter", self.name, ticker)
            return None
        return page


def minimum_quarters() -> int:
    """How many quarters the table should hold; 0 switches the backfill off."""
    return env_int(MIN_QUARTERS_ENV_VAR, DEFAULT_MIN_QUARTERS, minimum=0, maximum=80)


def quarters_present(db_service: DatabaseService, asset_id: int) -> set[date]:
    """The quarterly periods the database already holds for this asset."""
    from models import FinancialStatement

    session = db_service.get_session()
    try:
        rows = (
            session.query(FinancialStatement.period_end)
            .filter_by(asset_id=asset_id, statement_type="income", period_type="quarterly")
            .all()
        )
    finally:
        session.close()
    return {row[0] for row in rows}


def as_rows(
    asset_id: int,
    periods: list[dict[str, Any]],
    currency: str | None,
    known: set[date],
) -> list[dict[str, Any]]:
    """The rows to hand to the insert: only the periods the database does not have yet.

    This saves rows, it is not the guarantee - ``insert_rows`` decides, so a period another
    enrichment added while the page was in flight is not overwritten by these values.

    A period it already holds keeps the figures it was given: Yahoo and this source may
    round differently, and one row from one source is worth more than a mixture.
    """
    rows = []
    for period in periods:
        if period["period_end"] in known:
            continue
        values: dict[str, Any] = {
            "asset_id": asset_id,
            "statement_type": "income",
            "period_type": "quarterly",
            "period_end": period["period_end"],
            "fetched_at": datetime.now(timezone.utc),
            "currency": currency,
        }
        for column, figure in period.items():
            if column != "period_end":
                values[column] = figure
        rows.append(values)
    return rows


async def backfill(db_service: DatabaseService, asset_id: int, ticker: str) -> int:
    """Fill the quarters Yahoo did not bring; return how many were added.

    Nothing is fetched when the table already holds what it should, which is the usual
    case after the first run: the periods this source adds are then simply there.
    """
    wanted = minimum_quarters()
    if wanted <= 0:
        return 0
    # Synchronous database work goes through the worker pool: a query that waits for a
    # connection would otherwise stall every other request on this event loop.
    known = await run_sync(quarters_present, db_service, asset_id)
    if len(known) >= wanted:
        return 0
    page = await StockAnalysisQuarters().quarters(ticker)
    if page is None or not page.periods:
        return 0
    rows = as_rows(asset_id, page.periods, page.currency, known)
    if not rows:
        logger.debug("[%s] %s: no quarter left to add", PROVIDER_NAME, ticker)
        return 0
    # The list above only saves rows; the insert is what keeps a stored period untouched when
    # another enrichment added it while this page was in flight.
    added = await run_sync(insert_rows, db_service, rows)
    logger.info(
        "[%s] %s: %d quarter(s) added (the table held %d)",
        PROVIDER_NAME,
        ticker,
        added,
        len(known),
    )
    return added
