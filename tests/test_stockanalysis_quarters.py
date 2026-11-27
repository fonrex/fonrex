"""The quarters stockanalysis.com keeps: read, scaled, and written only where empty.

The fixture is a real page, captured on the date in ``fixtures/stockanalysis/sap_income_quarterly.json``
and reduced to the rows this module reads. ``expected`` holds the figures the page displays, so a
parser that drifts from them fails here instead of in an analysis three months later.
"""

import json
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from financials.enrichment import statement_store
from financials.enrichment import stockanalysis_quarters as quarters

#: Not under ``fixtures/providers``: that folder is read by ``test_provider_real_pages``, which
#: expects every fixture there to belong to a registered provider. This page belongs to none -
#: it feeds a statement table, not ``/fundamental``.
FIXTURES = Path(__file__).parent / "fixtures" / "stockanalysis"
PAGE = FIXTURES / "sap_income_quarterly.html"
EXPECTED = json.loads((FIXTURES / "sap_income_quarterly.json").read_text(encoding="utf-8"))


def page_text() -> str:
    return PAGE.read_text(encoding="utf-8")


def as_comparable(parsed: quarters.Page) -> dict[str, dict[str, float]]:
    """The parsed periods in the shape of the fixture's ``expected`` block."""
    return {
        period["period_end"].isoformat(): {
            column: (float(figure) if column.startswith("eps") else int(figure))
            for column, figure in period.items()
            if column != "period_end"
        }
        for period in parsed.periods
    }


class TestTheParsedPage:
    def test_the_scale_and_the_currency_are_read_from_the_page(self):
        # "Financials in millions EUR." - the figures below are in millions and the currency is
        # read, not assumed: a London line reports in pounds and a German one in euro.
        parsed = quarters.parse(page_text())
        assert parsed.multiplier == Decimal(1_000_000)
        assert parsed.currency == EXPECTED["page_says"]["currency"]

    def test_every_quarter_carries_what_the_page_displays(self):
        assert as_comparable(quarters.parse(page_text())) == EXPECTED["expected"]

    def test_the_quarters_are_newest_first(self):
        ends = [period["period_end"] for period in quarters.parse(page_text()).periods]
        assert ends == sorted(ends, reverse=True)

    def test_a_thousands_page_is_scaled_by_a_thousand(self):
        # The site publishes small caps in thousands; reading that table as millions would make
        # the revenue a thousand times too large without anything raising.
        page = (
            "<div>Financials in thousands GBP. Fiscal year is January - December.</div>"
            "<table>"
            "<tr><td>Period Ending</td><td>Mar '26 Mar 31, 2026</td></tr>"
            "<tr><td>Revenue</td><td>1,500</td></tr>"
            "<tr><td>EPS (Diluted)</td><td>0.12</td></tr>"
            "</table>"
        )
        parsed = quarters.parse(page)
        assert parsed.currency == "GBP"
        assert parsed.periods == [
            {
                "period_end": date(2026, 3, 31),
                "revenue": Decimal(1_500_000),
                "eps_diluted": Decimal("0.12"),
            }
        ]

    def test_a_row_that_does_not_line_up_with_its_dates_is_refused(self):
        # One cell short: matching the figures by position would put this quarter's revenue under
        # the previous quarter's date, which reads as a growth figure and is not one.
        page = (
            "<div>Financials in millions EUR.</div>"
            "<table>"
            "<tr><td>Period Ending</td><td>Mar '26 Mar 31, 2026</td><td>Dec '25 Dec 31, 2025</td></tr>"
            "<tr><td>Revenue</td><td>1,500</td></tr>"
            "</table>"
        )
        assert quarters.parse(page).periods == []

    def test_a_cell_without_a_full_date_is_no_period_end(self):
        # The short form ("Sep '26") appears on the page too; a month with no such day is not
        # a date either, and both must stay None rather than become the first of the month.
        assert quarters.period_end_of("Sep '26 Sep 30, 2026") == date(2026, 9, 30)
        assert quarters.period_end_of("Sep '26") is None
        assert quarters.period_end_of("Feb 30, 2026") is None

    def test_a_page_without_dates_carries_no_quarter(self):
        page = "<div>Financials in millions EUR.</div><table><tr><td>Revenue</td><td>1,500</td></tr></table>"
        assert quarters.parse(page).periods == []

    def test_a_page_that_states_no_scale_is_refused(self):
        # Millions are a guess the site does not always make: a table in thousands read as
        # millions is a thousand times too large, and a written period is never revisited.
        page = (
            "<table>"
            "<tr><td>Period Ending</td><td>Mar '26 Mar 31, 2026</td></tr>"
            "<tr><td>Revenue</td><td>1,500</td></tr>"
            "</table>"
        )
        parsed = quarters.parse(page)
        assert parsed.multiplier is None
        assert parsed.periods == []
        assert not parsed

    def test_a_bank_quarter_without_revenue_stays_readable_through_its_earnings(self):
        page = (
            "<div>Financials in millions EUR.</div>"
            "<table>"
            "<tr><td>Period Ending</td><td>Sep '26 Sep 30, 2026</td></tr>"
            "<tr><td>Revenue</td><td>-</td></tr>"
            "<tr><td>EPS (Diluted)</td><td>1.10</td></tr>"
            "</table>"
        )
        assert quarters.parse(page).periods == [
            {"period_end": date(2026, 9, 30), "eps_diluted": Decimal("1.10")}
        ]


class TestTheUrl:
    def test_a_german_ticker_uses_the_path_of_its_exchange(self):
        assert quarters.url_for("SAP.DE") == (
            "https://stockanalysis.com/quote/etr/SAP/financials/income-statement/?p=quarterly"
        )

    def test_a_us_ticker_uses_the_plain_path(self):
        assert quarters.url_for("msft") == (
            "https://stockanalysis.com/stocks/msft/financials/income-statement/?p=quarterly"
        )

    def test_a_suffix_without_a_known_path_is_refused(self):
        # Better no second source than a page of another instrument.
        assert quarters.url_for("XYZ.QQ") is None


class TestWhatGetsWritten:
    def test_a_period_the_table_holds_is_left_alone(self):
        # Yahoo and this source round differently: one row from one source is worth more than a
        # row stitched from two.
        known = {date(2026, 9, 30)}
        rows = quarters.as_rows(
            7,
            [
                {"period_end": date(2026, 9, 30), "revenue": Decimal(1)},
                {"period_end": date(2026, 6, 30), "revenue": Decimal(2)},
            ],
            "EUR",
            known,
        )
        assert [row["period_end"] for row in rows] == [date(2026, 6, 30)]
        assert rows[0]["asset_id"] == 7
        assert rows[0]["statement_type"] == "income"
        assert rows[0]["period_type"] == "quarterly"
        assert rows[0]["currency"] == "EUR"
        assert rows[0]["revenue"] == Decimal(2)


class TestTheStore:
    """The write both producers share: the table must not gain a second interpretation."""

    def test_an_empty_batch_opens_nothing(self):
        class Untouchable:
            def get_session(self):
                raise AssertionError("a batch of no row must not touch the database")

        #: The store takes a database service; these fakes stand in for it on purpose.
        database: Any = Untouchable()

        assert statement_store.upsert_rows(database, []) == 0

    def test_a_failed_write_is_rolled_back_and_the_error_kept(self):
        # A half-written period is worse than none: the caller must see the failure.
        class BrokenSession:
            rolled_back = False
            closed = False

            def query(self, *args, **kwargs):
                raise RuntimeError("the connection is gone")

            def rollback(self):
                BrokenSession.rolled_back = True

            def close(self):
                BrokenSession.closed = True

        class BrokenDatabase:
            def get_session(self):
                return BrokenSession()

        database: Any = BrokenDatabase()
        with pytest.raises(RuntimeError, match="the connection is gone"):
            statement_store.upsert_rows(
                database,
                [
                    {
                        "asset_id": 1,
                        "statement_type": "income",
                        "period_type": "quarterly",
                        "period_end": date(2026, 9, 30),
                    }
                ],
            )
        assert BrokenSession.rolled_back is True
        assert BrokenSession.closed is True


class Recorder:
    """Stands in for the provider: it answers with a page and remembers that it was asked."""

    def __init__(self, page: quarters.Page | None):
        self.page = page
        self.asked: list[str] = []

    async def quarters(self, ticker: str) -> quarters.Page | None:
        self.asked.append(ticker)
        return self.page


@pytest.fixture
def written(monkeypatch):
    """The rows the backfill wrote, and the provider that was asked for them."""
    state = {"rows": [], "provider": None}

    def install(page):
        provider = Recorder(page)
        state["provider"] = provider
        monkeypatch.setattr(quarters, "StockAnalysisQuarters", lambda: provider)
        monkeypatch.setattr(
            quarters, "insert_rows", lambda _db, rows: state["rows"].extend(rows) or len(rows)
        )
        return provider

    return state, install


#: The backfill takes a database service it never opens in these tests: the periods and the write
#: are stubbed, because what is under test is the decision, not SQLAlchemy. ``TestTheStoredTable``
#: runs against a real database, where the write itself is the question.
DB: Any = object()


@pytest.fixture
def database(tmp_path):
    """A real SQLite file with the two tables these tests write and read back.

    Only those tables are created: the whole schema takes half a minute and none of it is
    what is under test here.
    """
    from database.service import DatabaseService
    from models import Asset, Base, FinancialStatement

    service = DatabaseService(f"sqlite:///{tmp_path / 'quarters.db'}")
    Base.metadata.create_all(service.engine, tables=[Asset.__table__, FinancialStatement.__table__])
    yield service
    service.engine.dispose()


def stored(database, period_end: date) -> Any:
    """The row of one quarter, read back from the table."""
    from models import FinancialStatement

    session = database.get_session()
    try:
        return (
            session.query(FinancialStatement)
            .filter_by(
                asset_id=1, statement_type="income", period_type="quarterly", period_end=period_end
            )
            .one_or_none()
        )
    finally:
        session.close()


def row(period_end: date, revenue: int) -> dict[str, Any]:
    return {
        "asset_id": 1,
        "statement_type": "income",
        "period_type": "quarterly",
        "period_end": period_end,
        "revenue": Decimal(revenue),
    }


class TestTheStoredTable:
    """A written period stays as the source that wrote it - checked on the database, not on a stub."""

    def test_an_existing_row_is_not_touched_by_the_insert(self, database):
        assert statement_store.insert_rows(database, [row(date(2026, 9, 30), 100)]) == 1
        # The same quarter again, other figures: the store says no and the row keeps its source.
        assert statement_store.insert_rows(database, [row(date(2026, 9, 30), 999)]) == 0
        assert stored(database, date(2026, 9, 30)).revenue == Decimal("100.00")

    def test_the_upsert_still_replaces_what_the_enrichment_asked_for(self, database):
        statement_store.insert_rows(database, [row(date(2026, 9, 30), 100)])
        statement_store.upsert_rows(database, [row(date(2026, 9, 30), 555)])
        assert stored(database, date(2026, 9, 30)).revenue == Decimal("555.00")

    async def test_the_backfill_adds_the_missing_quarter_and_leaves_the_stored_one(
        self, database, written, monkeypatch
    ):
        # Yahoo wrote Q3 while this page was already on its way: the backfill must add only Q2.
        statement_store.insert_rows(database, [row(date(2026, 9, 30), 100)])
        monkeypatch.setenv("STOCKANALYSIS_MIN_QUARTERS", "20")
        _, install = written
        install(
            quarters.Page(
                Decimal(1_000_000),
                "EUR",
                [
                    {"period_end": date(2026, 9, 30), "revenue": Decimal(200)},
                    {"period_end": date(2026, 6, 30), "revenue": Decimal(300)},
                ],
            )
        )
        monkeypatch.setattr(
            quarters, "insert_rows", lambda db, rows: statement_store.insert_rows(db, rows)
        )

        assert await quarters.backfill(database, 1, "SAP.DE") == 1
        assert stored(database, date(2026, 9, 30)).revenue == Decimal("100.00")
        assert stored(database, date(2026, 6, 30)).revenue == Decimal("300.00")


class TestTheRequest:
    """The request itself, through the HTTP policy of the project rather than a real socket."""

    async def test_the_page_is_read_from_the_exchange_path_of_the_ticker(self, fake_network):
        fake_network.get("stockanalysis.com", httpx.Response(200, text=page_text()))

        parsed = await quarters.StockAnalysisQuarters().quarters("SAP.DE")

        assert parsed is not None
        assert len(parsed.periods) == len(EXPECTED["expected"])
        assert fake_network.urls("stockanalysis.com") == [
            "https://stockanalysis.com/quote/etr/SAP/financials/income-statement/?p=quarterly"
        ]

    async def test_a_suffix_without_a_path_costs_no_request(self, fake_network):
        # An unmapped suffix is an instrument on an exchange the site may name differently;
        # asking anyway would read the page of another listing.
        assert await quarters.StockAnalysisQuarters().quarters("XYZ.QQ") is None
        assert fake_network.requests == []

    async def test_a_blocked_request_is_not_a_page(self, fake_network):
        fake_network.get("stockanalysis.com", httpx.Response(403, text="Forbidden"))

        assert await quarters.StockAnalysisQuarters().quarters("SAP.DE") is None

    async def test_a_page_without_a_quarter_is_not_a_page(self, fake_network):
        # A redirect to a notice or to another document must not read as an empty statement.
        fake_network.get(
            "stockanalysis.com", httpx.Response(200, text="<html><body>Just a moment</body></html>")
        )

        assert await quarters.StockAnalysisQuarters().quarters("SAP.DE") is None

    async def test_a_page_that_states_no_scale_is_not_a_page(self, fake_network):
        # Without the scale line the figures could be millions or thousands; a written period is
        # never revisited, so the page is refused instead of read on a guess.
        fake_network.get(
            "stockanalysis.com",
            httpx.Response(
                200,
                text=(
                    "<table>"
                    "<tr><td>Period Ending</td><td>Sep '26 Sep 30, 2026</td></tr>"
                    "<tr><td>Revenue</td><td>10,270</td></tr>"
                    "</table>"
                ),
            ),
        )

        assert await quarters.StockAnalysisQuarters().quarters("SAP.DE") is None

    async def test_a_page_beyond_the_cap_is_refused(self, fake_network):
        fake_network.get(
            "stockanalysis.com", httpx.Response(200, text="x" * (quarters.MAX_BYTES + 1))
        )

        assert await quarters.StockAnalysisQuarters().quarters("SAP.DE") is None


class TestTheBackfill:
    async def test_a_full_table_costs_no_request(self, written, monkeypatch):
        state, install = written
        monkeypatch.setenv("STOCKANALYSIS_MIN_QUARTERS", "20")
        monkeypatch.setattr(quarters, "quarters_present", lambda _db, _asset: set(range(20)))
        provider = install(None)

        assert await quarters.backfill(DB, 1, "SAP.DE") == 0
        assert provider.asked == []
        assert state["rows"] == []

    async def test_the_missing_quarters_are_written(self, written, monkeypatch):
        state, install = written
        monkeypatch.setenv("STOCKANALYSIS_MIN_QUARTERS", "20")
        monkeypatch.setattr(quarters, "quarters_present", lambda _db, _asset: {date(2026, 9, 30)})
        provider = install(
            quarters.Page(
                Decimal(1_000_000),
                "EUR",
                [
                    {"period_end": date(2026, 9, 30), "revenue": Decimal(1)},
                    {"period_end": date(2026, 6, 30), "revenue": Decimal(2)},
                ],
            )
        )

        assert await quarters.backfill(DB, 3, "SAP.DE") == 1
        assert provider.asked == ["SAP.DE"]
        assert [row["period_end"] for row in state["rows"]] == [date(2026, 6, 30)]

    async def test_zero_switches_the_backfill_off(self, written, monkeypatch):
        state, install = written
        monkeypatch.setenv("STOCKANALYSIS_MIN_QUARTERS", "0")
        provider = install(None)

        assert await quarters.backfill(DB, 1, "SAP.DE") == 0
        assert provider.asked == []

    async def test_a_page_of_known_periods_writes_nothing(self, written, monkeypatch):
        # The source may answer with periods the table already holds - the first run of an
        # asset, or a page that has not moved since. Nothing is written and nothing is lost.
        state, install = written
        monkeypatch.setenv("STOCKANALYSIS_MIN_QUARTERS", "20")
        monkeypatch.setattr(quarters, "quarters_present", lambda _db, _asset: {date(2026, 9, 30)})
        install(
            quarters.Page(
                Decimal(1_000_000),
                "EUR",
                [{"period_end": date(2026, 9, 30), "revenue": Decimal(1)}],
            )
        )

        assert await quarters.backfill(DB, 1, "SAP.DE") == 0
        assert state["rows"] == []

    async def test_the_database_work_leaves_the_event_loop(self, written, monkeypatch):
        # A query that waits for a connection on the loop thread stalls every other request of
        # the worker; the read and the write belong in the pool (AGENTS.md rule 22).
        state, install = written
        monkeypatch.setenv("STOCKANALYSIS_MIN_QUARTERS", "20")
        seen: list[tuple[str, int]] = []

        def read(_db, _asset):
            seen.append(("read", threading.get_ident()))
            return set()

        def write(_db, rows):
            seen.append(("write", threading.get_ident()))
            return len(rows)

        install(
            quarters.Page(
                Decimal(1_000_000),
                "EUR",
                [{"period_end": date(2026, 9, 30), "revenue": Decimal(1)}],
            )
        )
        # After install(): it replaces the store itself, and this test asks where the store runs.
        monkeypatch.setattr(quarters, "quarters_present", read)
        monkeypatch.setattr(quarters, "insert_rows", write)
        loop_thread = threading.get_ident()

        assert await quarters.backfill(DB, 1, "SAP.DE") == 1
        assert [name for name, _ in seen] == ["read", "write"]
        assert all(identity != loop_thread for _, identity in seen)

    async def test_a_page_without_a_quarter_writes_nothing(self, written, monkeypatch):
        state, install = written
        monkeypatch.setenv("STOCKANALYSIS_MIN_QUARTERS", "20")
        monkeypatch.setattr(quarters, "quarters_present", lambda _db, _asset: set())
        install(None)

        assert await quarters.backfill(DB, 1, "SAP.DE") == 0
        assert state["rows"] == []
