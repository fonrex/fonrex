"""``/sec/{ticker}/statements``, ``/check`` and ``/refresh``.

The services are replaced by fakes fed with the real answers of
``tests/fixtures/sec/``; storing is tested on PostgreSQL. No test reaches the SEC.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import routers.sec as sec_routes
from database.price_series import PriceSeries
from financials.providers.sec_edgar import uses_default_contact
from financials.sec_check import SecCheckResult, compare
from financials.sec_statements import SecLoad
from financials.xbrl_mapping import annual_statements, facts_to_rows
from main import app

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
ANSWER = json.loads((FIXTURES / "companyfacts_aapl.json").read_text(encoding="utf-8"))
YEARS = annual_statements(facts_to_rows("0000320193", ANSWER))
READ_AT = datetime(2026, 10, 16, 13, 0, tzinfo=UTC)
FRESH = SecLoad("fresh", "0000320193", "Apple Inc.", READ_AT, date(2025, 10, 31), 32)


@pytest.fixture
def services(monkeypatch):
    statements, checker = MagicMock(), MagicMock()
    statements.annual = AsyncMock(return_value=(FRESH, YEARS))
    statements.refresh = AsyncMock(
        return_value=SecLoad("fetched", "0000320193", "Apple Inc.", READ_AT)
    )
    yahoo = [
        SimpleNamespace(
            period_end=date(2025, 9, 30),
            currency="USD",
            revenue=Decimal("416161000000"),
            net_income=Decimal("100000000000"),
        )
    ]
    checker.check = AsyncMock(
        return_value=SecCheckResult(FRESH, Decimal("0.01"), compare(YEARS, yahoo, Decimal("0.01")))
    )

    async def asset_id(service, ticker, isin):
        if ticker.upper() == "NOPE":
            raise sec_routes.HTTPException(status_code=404, detail="No instrument found for NOPE")
        return 7

    monkeypatch.setattr(sec_routes, "_asset_id", asset_id)
    originals = {name: getattr(app.state, name, None) for name in ("sec_statements", "sec_check")}
    yield statements, checker
    for name, value in originals.items():
        setattr(app.state, name, value)


@pytest.fixture
def client(services):
    with TestClient(app) as test_client:
        app.state.sec_statements, app.state.sec_check = services
        yield test_client


class TestStatements:
    def test_the_years_filed_with_their_concept_and_filing(self, client):
        response = client.get("/sec/AAPL/statements")

        assert response.status_code == 200
        body = response.json()
        assert (body["cik"], body["entity_name"], body["load"]["status"]) == (
            "0000320193",
            "Apple Inc.",
            "fresh",
        )
        assert [year["period_end"] for year in body["years"]] == ["2025-09-27", "2024-09-28"]
        revenue = body["years"][0]["figures"]["revenue"]
        assert revenue == {
            "value": 416161000000.0,
            "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
            "taxonomy": "us-gaap",
            "unit": "USD",
            "form": "10-K",
            "filed": "2025-10-31",
            "accession": "0000320193-25-000079",
            "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/",
        }
        assert body["years"][0]["figures"]["capex"]["value"] == 12715000000.0  # positive, as filed

    def test_the_most_recent_years(self, client):
        body = client.get("/sec/aapl/statements", params={"years": 1}).json()
        assert (body["ticker"], len(body["years"])) == ("AAPL", 1)

    def test_an_instrument_without_sec_filer(self, client, services):
        services[0].annual.return_value = (
            SecLoad("no_cik", reason="No US listing in the catalogue"),
            [],
        )
        response = client.get("/sec/AIR.PA/statements")
        assert response.status_code == 404
        assert (
            response.json()["detail"]
            == "No SEC filer for this instrument: No US listing in the catalogue"
        )

    def test_nothing_ever_read_and_the_sec_down(self, client, services):
        services[0].annual.return_value = (
            SecLoad("failed", "0000320193", reason="XbrlError: no answer"),
            [],
        )
        response = client.get("/sec/AAPL/statements")
        assert response.status_code == 502
        assert "XbrlError: no answer" in response.json()["detail"]

    def test_the_sec_down_but_years_stored(self, client, services):
        services[0].annual.return_value = (SecLoad("failed", "0000320193", reason="down"), YEARS)
        body = client.get("/sec/AAPL/statements").json()
        assert (body["load"]["status"], len(body["years"])) == ("failed", 2)

    @pytest.mark.parametrize("years", [0, 31])
    def test_years_out_of_bounds(self, client, years):
        assert client.get("/sec/AAPL/statements", params={"years": years}).status_code == 422

    def test_an_unknown_ticker(self, client):
        assert client.get("/sec/NOPE/statements").status_code == 404


class TestCheck:
    def test_each_year_with_its_counts(self, client):
        response = client.get("/sec/AAPL/check")

        assert response.status_code == 200
        body = response.json()
        assert body["tolerance"] == 0.01
        assert set(body["not_compared"]) == {"cash_and_equivalents", "total_equity"}
        latest = body["years"][0]
        assert (latest["period_end"], latest["yahoo_period_end"]) == ("2025-09-27", "2025-09-30")
        figures = {figure["column"]: figure for figure in latest["figures"]}
        assert figures["revenue"]["status"] == "match"
        assert figures["net_income"]["status"] == "mismatch"
        assert figures["net_income"]["difference"] == pytest.approx(-0.107223, abs=1e-6)
        assert latest["match"] == 1 and latest["mismatch"] == 1
        assert latest["missing_yahoo"] == len(latest["figures"]) - 2
        assert body["years"][1]["yahoo_period_end"] is None

    def test_without_sec_filer(self, client, services):
        services[1].check.return_value = SecCheckResult(
            SecLoad("no_cik", reason="No US listing in the catalogue"), Decimal("0.01"), []
        )
        assert client.get("/sec/AIR.PA/check").status_code == 404


class TestRefresh:
    def test_reads_now_and_looks_the_cik_up_again(self, client, services):
        response = client.post("/sec/AAPL/refresh")

        assert response.status_code == 200
        assert response.json()["load"]["status"] == "fetched"
        services[0].refresh.assert_awaited_once_with(7, force=True, recheck_cik=True)

    def test_without_sec_filer(self, client, services):
        services[0].refresh.return_value = SecLoad("no_cik", reason="No US listing")
        assert client.post("/sec/SAP.DE/refresh").status_code == 404


def test_without_database_the_routes_are_unavailable(services):
    with TestClient(app) as test_client:
        app.state.sec_statements = app.state.sec_check = None
        for response in (
            test_client.get("/sec/AAPL/statements"),
            test_client.get("/sec/AAPL/check"),
            test_client.post("/sec/AAPL/refresh"),
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
        monkeypatch.setattr(sec_routes, "resolve_price_series_async", resolve)

        assert await sec_routes._asset_id(service, "AAPL", "US0378331005") == 42
        resolve.assert_awaited_once_with(session, "AAPL", isin="US0378331005")

    async def test_an_unknown_ticker(self, monkeypatch):
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=MagicMock())
        context.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(sec_routes, "resolve_price_series_async", AsyncMock(return_value=None))
        with pytest.raises(sec_routes.HTTPException) as refused:
            await sec_routes._asset_id(
                SimpleNamespace(session_factory=lambda: context), "NOPE", None
            )
        assert refused.value.status_code == 404


class TestContact:
    @pytest.mark.parametrize("value", [None, "", "contact@fonrex.io", " CONTACT@fonrex.io "])
    def test_the_address_of_the_example_is_reported(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("SEC_EDGAR_EMAIL", raising=False)
        else:
            monkeypatch.setenv("SEC_EDGAR_EMAIL", value)
        assert uses_default_contact()

    def test_an_address_of_the_owner_is_not(self, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_EMAIL", "pierre@example.org")
        assert not uses_default_contact()
