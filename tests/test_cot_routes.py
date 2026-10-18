"""``/cot/markets``, ``/positions``, ``/signals``, ``/extremes`` and ``/refresh``.

The service is replaced by a fake that holds the real files of ``tests/fixtures/cftc/``
and answers as the stored lines would, with the real computation of the signals;
storing them is tested on PostgreSQL. No test reaches the CFTC.
"""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from financials.providers.cftc import parse_cot_zip
from macro.cot_reports import (
    CotReports,
    ReportLoad,
    StoredMarket,
    StoredPosition,
    StoredWeek,
)
from main import app

FIXTURES = Path(__file__).parent / "fixtures" / "cftc"
READ_AT = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
FILES = {
    "legacy": [("deacot1986_2016.zip", 2000), ("deacot2025.zip", 2025), ("deacot2026.zip", 2026)],
    "tff": [("fin_fut_txt_2006_2016.zip", 2010), ("fut_fin_txt_2026.zip", 2026)],
    "disaggregated": [
        ("fut_disagg_txt_hist_2006_2016.zip", 2010),
        ("fut_disagg_txt_2026.zip", 2026),
    ],
}


class FakeReports:
    """The stored lines of the fixtures, and what each request asked to read."""

    def __init__(self):
        self.lines: dict[str, dict[str, list[StoredWeek]]] = {}
        for report, files in FILES.items():
            markets: dict[str, list[StoredWeek]] = {}
            for name, year in files:
                cot_file = parse_cot_zip((FIXTURES / name).read_bytes(), report, year)
                for line in cot_file.weeks:
                    markets.setdefault(line.code, []).append(
                        StoredWeek(
                            line.report_date,
                            line.name,
                            line.open_interest,
                            {
                                p.category: StoredPosition(p.long, p.short, p.spreading)
                                for p in line.positions
                            },
                        )
                    )
                    self.units = line.contract_units
            self.lines[report] = markets
        self.asked = []
        self.status = "fresh"
        self.reason = None

    def today(self) -> date:
        return date(2026, 10, 18)

    def _load(self, report, years) -> ReportLoad:
        days = [w.report_date for weeks in self.lines[report].values() for w in weeks]
        return ReportLoad(
            report,
            self.status,
            READ_AT,
            min(days, default=None),
            max(days, default=None),
            years_failed=list(years) if self.status == "failed" else [],
            reason=self.reason,
        )

    async def refresh(self, report, start_year, end_year=None, *, force=False):
        self.asked.append(("refresh", report, start_year, end_year, force))
        return self._load(report, range(start_year, (end_year or 2026) + 1))

    async def ensure(self, report, start, end=None):
        self.asked.append(("ensure", report, start, end))
        return self._load(report, range(start.year, (end or self.today()).year + 1))

    async def markets(self, report=None):
        return [
            StoredMarket(
                report,
                code,
                weeks[-1].name,
                None,
                weeks[0].report_date,
                weeks[-1].report_date,
                len(weeks),
            )
            for code, weeks in sorted(self.lines[report].items())
        ]

    async def history(self, report, code, start=None, end=None):
        return [
            week
            for week in self.lines[report].get(code, [])
            if (start is None or week.report_date >= start)
            and (end is None or week.report_date <= end)
        ]


@pytest.fixture
def reports():
    original = getattr(app.state, "cot_reports", None)
    yield FakeReports()
    app.state.cot_reports = original


@pytest.fixture
def client(reports):
    with TestClient(app) as test_client:
        app.state.cot_reports = reports
        yield test_client


class TestMarkets:
    def test_every_report_and_its_current_year(self, client, reports):
        body = client.get("/cot/markets").json()

        assert {m["report"] for m in body["markets"]} == {"legacy", "tff", "disaggregated"}
        assert [load["report"] for load in body["loads"]] == ["legacy", "tff", "disaggregated"]
        assert reports.asked == [
            ("ensure", report, date(2026, 10, 18), None)
            for report in ("legacy", "tff", "disaggregated")
        ]

    def test_the_latest_name_and_the_dates_stored(self, client):
        body = client.get("/cot/markets", params={"report": "legacy", "q": "043602"}).json()
        (ten_year,) = body["markets"]
        assert ten_year == {
            "report": "legacy",
            "market": "043602",
            "name": "UST 10Y NOTE - CHICAGO BOARD OF TRADE",
            "contract_units": None,
            "first_date": "1986-01-15",
            "last_date": "2026-10-13",
            "weeks": 18,
            "key": True,
        }

    def test_a_search_in_the_names(self, client):
        body = client.get("/cot/markets", params={"report": "disaggregated", "q": "gold"}).json()
        assert [m["market"] for m in body["markets"]] == ["088691"]

    def test_the_key_markets(self, client):
        body = client.get("/cot/markets", params={"report": "tff", "key": True}).json()
        assert [m["market"] for m in body["markets"]] == ["043602", "099741", "13874A"]
        assert all(m["key"] for m in body["markets"])

    def test_an_unknown_report(self, client):
        assert client.get("/cot/markets", params={"report": "cit"}).status_code == 422


class TestPositions:
    def test_every_type_of_trader_report_by_report(self, client, reports):
        response = client.get(
            "/cot/positions", params={"market": "13874a", "report": "tff", "start": "2026-10-01"}
        )

        assert response.status_code == 200
        body = response.json()
        assert (body["market"], body["name"]) == (
            "13874A",
            "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
        )
        assert [c["code"] for c in body["categories"]] == [
            "dealer",
            "asset_manager",
            "leveraged_funds",
            "other_reportable",
            "nonreportable",
        ]
        assert [w["date"] for w in body["weeks"]] == ["2026-10-06", "2026-10-13"]
        latest = body["weeks"][-1]
        assert latest["published_on"] == "2026-10-16"
        assert reports.asked == [("ensure", "tff", date(2026, 10, 1), date(2026, 10, 18))]
        funds = next(p for p in latest["positions"] if p["category"] == "leveraged_funds")
        assert funds["net"] == funds["long"] - funds["short"]

    def test_three_years_by_default(self, client, reports):
        client.get("/cot/positions", params={"market": "088691"})
        assert reports.asked == [("ensure", "legacy", date(2023, 10, 19), date(2026, 10, 18))]

    def test_the_history_of_a_market(self, client):
        body = client.get(
            "/cot/positions",
            params={"market": "043602", "start": "1987-01-01", "end": "1987-12-31"},
        ).json()
        (week,) = body["weeks"]
        assert week["date"] == "1987-06-30"
        nonreportable = week["positions"][-1]
        assert (nonreportable["category"], nonreportable["long"]) == ("nonreportable", -984)
        assert body["name"] == "6.5-10 YEAR U.S. TREASURY NOTE - CHICAGO BOARD OF TRADE"

    def test_a_market_without_reports(self, client):
        response = client.get("/cot/positions", params={"market": "999999"})
        assert response.status_code == 404
        assert "GET /cot/markets" in response.json()["detail"]

    def test_the_cftc_could_not_be_read_and_nothing_is_stored(self, client, reports):
        reports.status, reports.reason = "failed", "the CFTC did not answer"
        reports.lines["tff"] = {}
        response = client.get("/cot/positions", params={"market": "13874A", "report": "tff"})
        assert response.status_code == 502
        assert response.json()["detail"] == "The CFTC could not be read: the CFTC did not answer"

    def test_stored_reports_are_given_when_the_cftc_fails(self, client, reports):
        reports.status = "failed"
        body = client.get("/cot/positions", params={"market": "088691"}).json()
        assert body["load"]["status"] == "failed" and body["weeks"]

    @pytest.mark.parametrize(
        "params",
        [
            {"market": "S&P"},
            {"market": "13874"},
            {"market": "13874A", "report": "cit"},
            {"market": "13874A", "start": "2026-10-02", "end": "2026-10-01"},
        ],
    )
    def test_refused(self, client, params):
        assert client.get("/cot/positions", params=params).status_code == 422


class TestSignals:
    def test_the_speculators_by_default(self, client, reports):
        body = client.get(
            "/cot/signals", params={"market": "067651", "report": "disaggregated", "window": 4}
        ).json()

        assert body["category"] == {"code": "managed_money", "label": "Managed money"}
        latest = body["signals"][-1]
        assert latest["date"] == "2026-10-13" and latest["published_on"] == "2026-10-16"
        assert (latest["long"], latest["short"], latest["spreading"]) == (171973, 131399, 286687)
        assert latest["net"] == 171973 - 131399
        assert latest["net_share"] == pytest.approx(40574 / 1900179, abs=1e-6)
        assert 0 <= latest["cot_index"] <= 100
        # The window before start is read too.
        assert reports.asked == [("ensure", "disaggregated", date(2023, 9, 21), date(2026, 10, 18))]

    def test_another_type_of_trader(self, client):
        body = client.get(
            "/cot/signals",
            params={"market": "13874A", "report": "tff", "category": "asset_manager"},
        ).json()
        assert body["category"]["code"] == "asset_manager"
        assert body["window_weeks"] == 156

    def test_no_index_before_the_window_is_covered(self, client):
        body = client.get(
            "/cot/signals", params={"market": "088691", "start": "2026-01-01", "window": 156}
        ).json()
        assert all(signal["cot_index"] is None for signal in body["signals"])
        assert [s["change"] for s in body["signals"]][0] is not None

    def test_a_type_of_trader_of_another_report(self, client):
        response = client.get(
            "/cot/signals",
            params={"market": "13874A", "report": "tff", "category": "managed_money"},
        )
        assert response.status_code == 422
        assert "Unknown type of trader 'managed_money' for tff" in response.json()["detail"]

    @pytest.mark.parametrize("window", [0, 521])
    def test_a_window_out_of_bounds(self, client, window):
        params = {"market": "13874A", "window": window}
        assert client.get("/cot/signals", params=params).status_code == 422

    def test_a_market_without_reports(self, client):
        assert client.get("/cot/signals", params={"market": "999999"}).status_code == 404


class TestExtremes:
    def test_the_key_markets_the_most_extreme_first(self, client, reports):
        body = client.get("/cot/extremes", params={"report": "tff", "window": 4}).json()

        assert body["category"]["code"] == "leveraged_funds"
        assert {m["market"] for m in body["markets"]} == {"13874A", "043602", "099741"}
        indexes = [m["signal"]["cot_index"] for m in body["markets"]]
        distances = [abs(index - 50) for index in indexes if index is not None]
        assert distances == sorted(distances, reverse=True)
        assert all(m["signal"]["date"] == "2026-10-13" for m in body["markets"])
        assert set(body["missing"]) == {
            "209742",
            "239742",
            "042601",
            "097741",
            "096742",
            "1170E1",
            "133741",
        }
        # The window and four weeks more for a late report: 8 weeks before today.
        assert reports.asked == [("ensure", "tff", date(2026, 8, 23), date(2026, 10, 18))]

    def test_some_markets(self, client):
        body = client.get(
            "/cot/extremes", params={"market": ["088691,067651", "088691"], "window": 4}
        ).json()
        assert [m["market"] for m in body["markets"]] in (
            ["088691", "067651"],
            ["067651", "088691"],
        )
        assert body["missing"] == []

    def test_a_market_without_index_comes_last(self, client):
        body = client.get(
            "/cot/extremes", params={"market": ["004603", "088691"], "window": 4}
        ).json()
        assert [m["market"] for m in body["markets"]] == ["088691", "004603"]
        assert body["markets"][-1]["signal"]["cot_index"] is None

    def test_too_many_markets(self, client):
        codes = ",".join(f"{index:06d}" for index in range(21))
        assert client.get("/cot/extremes", params={"market": codes}).status_code == 422

    def test_the_cftc_could_not_be_read_and_nothing_is_stored(self, client, reports):
        reports.status, reports.reason = "failed", "the CFTC did not answer"
        reports.lines["tff"] = {}
        assert client.get("/cot/extremes", params={"report": "tff"}).status_code == 502


class TestRefresh:
    def test_every_report_this_year_by_default(self, client, reports):
        body = client.post("/cot/refresh").json()
        assert [load["report"] for load in body["loads"]] == ["legacy", "tff", "disaggregated"]
        assert reports.asked == [
            ("refresh", report, 2026, 2026, False) for report in ("legacy", "tff", "disaggregated")
        ]

    def test_the_whole_history_of_one_report(self, client, reports):
        response = client.post(
            "/cot/refresh", params={"report": "legacy", "start_year": 1986, "force": True}
        )
        assert response.status_code == 200
        assert reports.asked == [("refresh", "legacy", 1986, 2026, True)]

    @pytest.mark.parametrize(
        "params",
        [
            {"report": "cit"},
            {"start_year": 1985},
            {"start_year": 2026, "end_year": 2025},
        ],
    )
    def test_refused(self, client, params):
        assert client.post("/cot/refresh", params=params).status_code == 422


class TestOpenBB:
    def test_the_positioning_of_the_leveraged_funds_by_default(self, client, reports):
        response = client.get("/openbb/cot/signals/chart", params={"start": ""})

        assert response.status_code == 200
        figure = response.json()
        net, index = figure["data"]
        assert net["name"] == "Net position, Leveraged funds"
        assert index["name"] == "COT index (156 weeks)" and index["yaxis"] == "y2"
        assert net["x"][-1] == "2026-10-13" and len(net["x"]) == len(index["y"]) == 8
        assert figure["layout"]["title"] == (
            "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE — Leveraged funds "
            "(CFTC, Traders in Financial Futures)"
        )
        assert figure["layout"]["yaxis2"]["range"] == [0, 100]
        assert reports.asked[0][:2] == ("ensure", "tff")

    def test_another_market_report_and_type_of_trader(self, client):
        figure = client.get(
            "/openbb/cot/signals/chart",
            params={
                "market": "067651",
                "report": "disaggregated",
                "category": "swap_dealer",
                "window": 4,
            },
        ).json()
        net, index = figure["data"]
        assert net["name"] == "Net position, Swap dealers"
        assert net["y"][-1] == 126747 - 605225
        assert index["y"][-1] is not None

    def test_the_most_extreme_markets_as_a_table(self, client):
        rows = client.get("/openbb/cot/extremes", params={"window": 4}).json()

        found = [row for row in rows if row["Market"] != "No report stored"]
        assert {row["Code"] for row in found} == {"13874A", "043602", "099741"}
        first = found[0]
        assert set(first) == {
            "Market",
            "Code",
            "Report date",
            "Published",
            "Net",
            "Net % OI",
            "Change",
            "COT index",
            "Reading",
        }
        assert (first["Report date"], first["Published"]) == ("2026-10-13", "2026-10-16")
        assert first["Reading"] in ("Top of window", "Bottom of window", "")
        missing = [row["Code"] for row in rows if row["Market"] == "No report stored"]
        assert "1170E1" in missing

    def test_some_markets_of_a_report(self, client):
        rows = client.get(
            "/openbb/cot/extremes",
            params={"report": "disaggregated", "market": "088691, 067651", "category": ""},
        ).json()
        assert sorted(row["Code"] for row in rows) == ["067651", "088691"]
        gold = next(row for row in rows if row["Code"] == "088691")
        assert gold["Net % OI"] == pytest.approx(100 * (124747 - 17658) / 396345, abs=0.01)

    def test_refusals_are_those_of_the_routes(self, client):
        bad_market = client.get("/openbb/cot/signals/chart", params={"market": "S&P"})
        bad_start = client.get("/openbb/cot/signals/chart?start=yesterday")
        assert (bad_market.status_code, bad_start.status_code) == (422, 422)


def test_the_reading_of_an_index():
    from integrations.openbb.adapters import _cot_reading

    assert [_cot_reading(v) for v in (None, 95.0, 90.0, 50.0, 10.0, 3.2)] == [
        "",
        "Top of window",
        "Top of window",
        "",
        "Bottom of window",
        "Bottom of window",
    ]


class TestWithoutDatabase:
    def test_the_routes_answer_503(self, client):
        app.state.cot_reports = None
        assert client.get("/cot/markets").status_code == 503
        assert client.post("/cot/refresh").status_code == 503


def test_the_fake_has_the_methods_of_the_service():
    for name in ("today", "refresh", "ensure", "markets", "history"):
        assert callable(getattr(CotReports, name))
