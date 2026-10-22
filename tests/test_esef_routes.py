"""``/esef/{ticker}/statements``, ``/check``, ``/filings`` and ``/refresh``.

The services are replaced by fakes fed with the real filing of Airbus of
``tests/fixtures/esef/``; storing is tested on PostgreSQL. No test reaches GLEIF or
filings.xbrl.org.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import routers.esef as esef_routes
from database.price_series import PriceSeries
from financials.esef_statements import EsefLoad, fact_row, is_annual
from financials.providers.esef import parse_xbrl_json
from financials.sec_check import ESEF_NOT_COMPARED, SecCheckResult, compare
from financials.xbrl_mapping import annual_statements
from main import app

FIXTURES = Path(__file__).parent / "fixtures" / "esef"
LEI = "MINO79WLOO247M1IL051"
FXO_ID = f"{LEI}-2025-12-31-ESEF-NL-0"
VIEWER = f"https://filings.xbrl.org/{LEI}/2025-12-31/ESEF/NL/0/report/ixbrlviewer.html"
READ_AT = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)


def _years():
    report = parse_xbrl_json((FIXTURES / f"{FXO_ID}.json").read_bytes(), LEI, FXO_ID)
    rows = [
        fact_row(
            SimpleNamespace(
                lei=LEI,
                taxonomy=fact.prefix,
                concept=fact.name,
                unit=fact.unit,
                period_end=fact.period_end,
                duration_days=fact.duration_days,
                period_start=fact.period_start,
                value=fact.value,
                fxo_id=FXO_ID,
                report_end=date(2025, 12, 31),
            )
        )
        for fact in report.facts
        if is_annual(fact.duration_days)
    ]
    return annual_statements(rows)


YEARS = _years()
FRESH = EsefLoad("fresh", LEI, "AIRBUS SE", READ_AT, 5, date(2025, 12, 31), 269)
FILING = SimpleNamespace(
    fxo_id=FXO_ID,
    listed_period_end=date(2025, 12, 31),
    report_end=date(2025, 12, 31),
    country="NL",
    read_status="read",
    read_reason=None,
    read_at=READ_AT,
    facts=269,
    breakdowns=171,
    conflicts=0,
    error_count=0,
    warning_count=2,
    inconsistency_count=0,
    processed=datetime(2026, 3, 10, 9, 0, tzinfo=UTC),
    viewer_url=VIEWER,
    report_url=None,
    json_url=f"https://filings.xbrl.org/{LEI}/2025-12-31/ESEF/NL/0/{LEI}-2025-12-31.json",
    package_url=None,
)
UNREAD = SimpleNamespace(
    **{
        **vars(FILING),
        "fxo_id": f"{LEI}-2024-12-31-ESEF-NL-0",
        "listed_period_end": date(2024, 12, 31),
        "report_end": None,
        "read_status": None,
        "read_at": None,
        "facts": 0,
        "breakdowns": 0,
    }
)


@pytest.fixture
def services(monkeypatch):
    statements, checker = MagicMock(), MagicMock()
    statements.annual = AsyncMock(return_value=(FRESH, YEARS))
    statements.refresh = AsyncMock(return_value=FRESH)
    statements.filings = AsyncMock(return_value=[FILING, UNREAD])
    yahoo = [
        SimpleNamespace(
            period_end=date(2025, 12, 31),
            currency="EUR",
            revenue=Decimal("73420000000"),
            net_income=Decimal("5000000000"),
        )
    ]
    checker.not_compared = dict(ESEF_NOT_COMPARED)
    checker.check = AsyncMock(
        return_value=SecCheckResult(
            FRESH,
            Decimal("0.01"),
            compare(
                YEARS,
                yahoo,
                Decimal("0.01"),
                filed_with="the ESEF filing",
                not_compared=ESEF_NOT_COMPARED,
            ),
        )
    )

    async def asset_id(service, ticker, isin):
        if ticker.upper() == "NOPE":
            raise esef_routes.HTTPException(status_code=404, detail="No instrument found for NOPE")
        return 7

    monkeypatch.setattr(esef_routes, "_asset_id", asset_id)
    names = ("esef_statements", "esef_check")
    originals = {name: getattr(app.state, name, None) for name in names}
    yield statements, checker
    for name, value in originals.items():
        setattr(app.state, name, value)


@pytest.fixture
def client(services):
    with TestClient(app) as test_client:
        app.state.esef_statements, app.state.esef_check = services
        yield test_client


class TestStatements:
    def test_the_years_filed_with_their_concept_and_filing(self, client, services):
        response = client.get("/esef/AIR.PA/statements")

        assert response.status_code == 200
        body = response.json()
        assert (body["lei"], body["entity_name"], body["load"]["status"]) == (
            LEI,
            "AIRBUS SE",
            "fresh",
        )
        assert "filings.xbrl.org" in body["source"] and "GLEIF" in body["source"]
        assert [year["period_end"] for year in body["years"]] == ["2025-12-31", "2024-12-31"]
        assert body["years"][0]["currency"] == "EUR"
        assert body["years"][0]["figures"]["revenue"] == {
            "value": 73420000000.0,
            "concept": "Revenue",
            "taxonomy": "ifrs-full",
            "unit": "EUR",
            "fxo_id": FXO_ID,
            "report_end": "2025-12-31",
            "viewer_url": VIEWER,
        }
        # The comparative year comes from the same filing.
        assert body["years"][1]["figures"]["revenue"]["fxo_id"] == FXO_ID
        services[0].filings.assert_awaited_once_with(LEI)

    def test_the_most_recent_years(self, client):
        body = client.get("/esef/air.pa/statements", params={"years": 1}).json()
        assert (body["ticker"], len(body["years"])) == ("AIR.PA", 1)

    def test_an_instrument_without_esef_filer(self, client, services):
        services[0].annual.return_value = (
            EsefLoad("no_lei", reason="ETF files no annual report"),
            [],
        )
        response = client.get("/esef/CW8.PA/statements")
        assert response.status_code == 404
        assert (
            response.json()["detail"]
            == "No ESEF filer for this instrument: ETF files no annual report"
        )
        services[0].filings.assert_not_awaited()

    def test_nothing_ever_read_and_the_index_down(self, client, services):
        services[0].annual.return_value = (
            EsefLoad("failed", LEI, reason="EsefError: filings.xbrl.org did not answer"),
            [],
        )
        response = client.get("/esef/AIR.PA/statements")
        assert response.status_code == 502
        assert "did not answer" in response.json()["detail"]

    def test_the_index_down_but_years_stored(self, client, services):
        services[0].annual.return_value = (EsefLoad("failed", LEI, reason="down"), YEARS)
        body = client.get("/esef/AIR.PA/statements").json()
        assert (body["load"]["status"], len(body["years"])) == ("failed", 2)

    def test_a_filing_no_longer_listed_has_no_link(self, client, services):
        services[0].filings.return_value = []
        body = client.get("/esef/AIR.PA/statements").json()
        assert body["years"][0]["figures"]["revenue"]["viewer_url"] is None

    @pytest.mark.parametrize("years", [0, 31])
    def test_years_out_of_bounds(self, client, years):
        assert client.get("/esef/AIR.PA/statements", params={"years": years}).status_code == 422

    def test_an_unknown_ticker(self, client):
        assert client.get("/esef/NOPE/statements").status_code == 404


class TestCheck:
    def test_each_year_with_its_counts(self, client):
        response = client.get("/esef/AIR.PA/check")

        assert response.status_code == 200
        body = response.json()
        assert body["tolerance"] == 0.01
        assert set(body["not_compared"]) == {
            "cash_and_equivalents",
            "total_equity",
            "operating_income",
            "interest_expense",
            "capex",
        }
        compared = {f["column"] for year in body["years"] for f in year["figures"]}
        assert not compared & {"operating_income", "interest_expense", "capex"}
        latest = body["years"][0]
        assert (latest["period_end"], latest["yahoo_period_end"]) == ("2025-12-31", "2025-12-31")
        figures = {figure["column"]: figure for figure in latest["figures"]}
        assert figures["revenue"]["status"] == "match"
        assert figures["revenue"]["viewer_url"] == VIEWER
        assert figures["net_income"] == {
            "column": "net_income",
            "statement": "income",
            "status": "mismatch",
            "esef": 5221000000.0,
            "yahoo": 5000000000.0,
            "difference": pytest.approx(-0.042329, abs=1e-6),
            "concept": "ProfitLossAttributableToOwnersOfParent",
            "viewer_url": VIEWER,
        }
        assert latest["match"] == 1 and latest["mismatch"] == 1
        assert latest["missing_yahoo"] == len(latest["figures"]) - 2
        assert latest["missing_esef"] == 0
        assert body["years"][1]["yahoo_period_end"] is None

    def test_a_figure_only_at_yahoo(self, client, services):
        yahoo = [SimpleNamespace(period_end=date(2025, 12, 31), currency="EUR", shares_basic=7.9e8)]
        services[1].check.return_value = SecCheckResult(
            FRESH, Decimal("0.01"), compare(YEARS[:1], yahoo, Decimal("0.01"))
        )
        latest = client.get("/esef/AIR.PA/check").json()["years"][0]
        shares = next(f for f in latest["figures"] if f["column"] == "shares_basic")
        assert (shares["status"], shares["esef"], shares["viewer_url"]) == (
            "missing_esef",
            None,
            None,
        )
        assert latest["missing_esef"] == 1

    def test_another_currency_is_named_as_the_esef_filing(self, client, services):
        yahoo = [SimpleNamespace(period_end=date(2025, 12, 31), currency="USD")]
        services[1].check.return_value = SecCheckResult(
            FRESH,
            Decimal("0.01"),
            compare(YEARS[:1], yahoo, Decimal("0.01"), filed_with="the ESEF filing"),
        )
        note = client.get("/esef/AIR.PA/check").json()["years"][0]["note"]
        assert note == "Yahoo gives this year in USD, the ESEF filing in EUR: not compared"

    def test_without_esef_filer(self, client, services):
        services[1].check.return_value = SecCheckResult(
            EsefLoad("no_lei", reason="no ISIN"), Decimal("0.01"), []
        )
        assert client.get("/esef/AIR.PA/check").status_code == 404


class TestFilings:
    def test_the_filings_with_their_reading(self, client, services):
        response = client.get("/esef/AIR.PA/filings")

        assert response.status_code == 200
        body = response.json()
        assert body["load"]["filings"] == 5
        read, unread = body["filings"]
        assert (read["fxo_id"], read["read_status"], read["facts"], read["breakdowns"]) == (
            FXO_ID,
            "read",
            269,
            171,
        )
        assert (read["viewer_url"], read["warning_count"]) == (VIEWER, 2)
        assert (unread["read_status"], unread["report_end"]) == (None, None)
        services[0].refresh.assert_awaited_once_with(7)

    def test_without_esef_filer(self, client, services):
        services[0].refresh.return_value = EsefLoad("no_lei", reason="no ISIN")
        assert client.get("/esef/SAP.DE/filings").status_code == 404
        services[0].filings.assert_not_awaited()

    def test_the_index_never_read(self, client, services):
        services[0].refresh.return_value = EsefLoad("failed", LEI, reason="down")
        services[0].filings.return_value = []
        assert client.get("/esef/AIR.PA/filings").status_code == 502


class TestRefresh:
    def test_reads_now_and_asks_gleif_again(self, client, services):
        services[0].refresh.return_value = EsefLoad(
            "fetched",
            LEI,
            "AIRBUS SE",
            READ_AT,
            5,
            date(2025, 12, 31),
            269,
            read=(FXO_ID,),
            refused={f"{LEI}-2024-12-31-ESEF-NL-0": "EsefError: not JSON"},
        )
        response = client.post("/esef/AIR.PA/refresh")

        assert response.status_code == 200
        load = response.json()["load"]
        assert (load["status"], load["read"]) == ("fetched", [FXO_ID])
        assert load["refused"] == {f"{LEI}-2024-12-31-ESEF-NL-0": "EsefError: not JSON"}
        services[0].refresh.assert_awaited_once_with(7, force=True, recheck_lei=True)

    def test_without_esef_filer(self, client, services):
        services[0].refresh.return_value = EsefLoad("no_lei", reason="no ISIN")
        assert client.post("/esef/SAP.DE/refresh").status_code == 404

    def test_a_read_only_key_cannot_refresh(self):
        from auth.dependencies import is_read_request

        assert not is_read_request("POST", "/esef/AIR.PA/refresh")


def test_without_database_the_routes_are_unavailable(services):
    with TestClient(app) as test_client:
        app.state.esef_statements = app.state.esef_check = None
        for response in (
            test_client.get("/esef/AIR.PA/statements"),
            test_client.get("/esef/AIR.PA/check"),
            test_client.get("/esef/AIR.PA/filings"),
            test_client.post("/esef/AIR.PA/refresh"),
        ):
            assert response.status_code == 503


class TestTickerResolution:
    async def test_the_instrument_of_the_ticker(self, monkeypatch):
        session = MagicMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        service = SimpleNamespace(session_factory=lambda: context)
        resolve = AsyncMock(return_value=PriceSeries(asset_id=42, listing_id=420))
        monkeypatch.setattr(esef_routes, "resolve_price_series_async", resolve)

        assert await esef_routes._asset_id(service, "AIR.PA", "NL0000235190") == 42
        resolve.assert_awaited_once_with(session, "AIR.PA", isin="NL0000235190")

    async def test_an_unknown_ticker(self, monkeypatch):
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=MagicMock())
        context.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(esef_routes, "resolve_price_series_async", AsyncMock(return_value=None))
        with pytest.raises(esef_routes.HTTPException) as refused:
            await esef_routes._asset_id(
                SimpleNamespace(session_factory=lambda: context), "NOPE", None
            )
        assert refused.value.status_code == 404


def test_without_the_check_the_check_route_is_unavailable(services):
    with TestClient(app) as test_client:
        app.state.esef_statements, app.state.esef_check = services[0], None
        assert test_client.get("/esef/AIR.PA/check").status_code == 503
        assert test_client.get("/esef/AIR.PA/statements").status_code == 200


class TestOpenBB:
    def test_one_row_per_figure_the_gaps_first(self, client, services):
        response = client.get("/openbb/esef/AIR.PA/check", params={"years": 1})

        assert response.status_code == 200
        rows = response.json()
        assert {row["Year"] for row in rows} == {"2025-12-31"}
        assert rows[0] == {
            "Year": "2025-12-31",
            "Figure": "Net income",
            "Status": "Mismatch",
            "ESEF": 5221000000,
            "Yahoo": 5000000000,
            "Gap": "-4.23%",
            "Concept": "ProfitLossAttributableToOwnersOfParent",
            "Filing": VIEWER,
        }
        assert rows[1]["Status"] == "Missing at Yahoo"
        assert rows[-1]["Status"] == "Match" and rows[-1]["Figure"] == "Revenue"
        services[1].check.assert_awaited_once_with(7)

    def test_three_years_by_default(self, client):
        rows = client.get("/openbb/esef/AIR.PA/check").json()
        assert {row["Year"] for row in rows} == {"2025-12-31", "2024-12-31"}

    def test_a_figure_not_in_the_filing_and_a_year_not_compared(self):
        from integrations.openbb.adapters import format_esef_check_table

        rows = format_esef_check_table(
            {
                "years": [
                    {"period_end": "2025-12-31", "note": "not compared", "figures": []},
                    {
                        "period_end": "2024-12-31",
                        "figures": [
                            {"column": "shares_basic", "status": "missing_esef", "yahoo": 7.9e8}
                        ],
                    },
                ]
            }
        )
        assert rows[0] == {
            "Year": "2025-12-31",
            "Figure": "—",
            "Status": "not compared",
            "ESEF": None,
            "Yahoo": None,
            "Gap": None,
            "Concept": None,
            "Filing": None,
        }
        assert (rows[1]["Figure"], rows[1]["Status"], rows[1]["ESEF"]) == (
            "Shares (basic)",
            "Not in the ESEF filing",
            None,
        )
        assert format_esef_check_table({"years": []})[0]["Status"] == "No fiscal year filed in ESEF"

    def test_an_instrument_without_esef_filer(self, client, services):
        services[1].check.return_value = SecCheckResult(
            EsefLoad("no_lei", reason="no ISIN"), Decimal("0.01"), []
        )
        assert client.get("/openbb/esef/CW8.PA/check").status_code == 404


class TestWarnings:
    def test_a_figure_left_out_is_said_in_the_statements_and_the_check(self, client, services):
        from copy import deepcopy

        years = deepcopy(YEARS)
        years[0].figures.pop("eps_basic")
        years[0].warnings.append("eps_basic 265 left out: an error of scale in the filing")
        services[0].annual.return_value = (FRESH, years)
        yahoo = [SimpleNamespace(period_end=date(2025, 12, 31), currency="EUR", eps_basic=6.6)]
        services[1].check.return_value = SecCheckResult(
            FRESH,
            Decimal("0.01"),
            compare(years[:1], yahoo, Decimal("0.01"), not_compared=ESEF_NOT_COMPARED),
        )

        statements = client.get("/esef/AIR.PA/statements").json()
        check = client.get("/esef/AIR.PA/check").json()
        rows = client.get("/openbb/esef/AIR.PA/check").json()

        warning = "eps_basic 265 left out: an error of scale in the filing"
        assert statements["years"][0]["warnings"] == [warning]
        assert statements["years"][1]["warnings"] == []
        assert check["years"][0]["warnings"] == [warning]
        eps = next(f for f in check["years"][0]["figures"] if f["column"] == "eps_basic")
        assert eps["status"] == "missing_esef"
        assert rows[0]["Status"] == warning and rows[0]["Figure"] == "—"
