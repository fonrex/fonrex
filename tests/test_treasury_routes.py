"""``/macro/treasury/curve``, ``/series`` and ``/refresh``.

The service is replaced by a fake that holds the real files of
``tests/fixtures/treasury/`` (nominal 2025 and 2026, real 2026) and answers as the
stored rates would; storing them is tested on PostgreSQL. No test reaches the
Treasury.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from financials.providers.treasury import parse_curve_csv
from macro.treasury_curves import CurveLoad, StoredDay
from main import app
from routers.macro import _before

FIXTURES = Path(__file__).parent / "fixtures" / "treasury"
READ_AT = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


def stored(name: str, curve: str, year: int) -> dict:
    text = (FIXTURES / name).read_bytes().decode()
    return {day.day: day.points for day in parse_curve_csv(text, curve, year).days}


class FakeCurves:
    """The stored rates of the fixtures, and the years each request asked to read."""

    def __init__(self):
        self.days = {
            "nominal": {
                **stored("nominal_2025.csv", "nominal", 2025),
                **stored("nominal_2026.csv", "nominal", 2026),
            },
            "real": stored("real_2026.csv", "real", 2026),
        }
        self.asked = []
        self.load_status = "fresh"
        self.reason = None

    def today(self) -> date:
        return date(2026, 10, 17)

    async def refresh(self, curve, start_year, end_year=None, *, force=False):
        self.asked.append((curve, start_year, end_year, force))
        days = sorted(self.days[curve])
        return CurveLoad(
            curve,
            self.load_status,
            READ_AT,
            days[0] if days else None,
            days[-1] if days else None,
            years_failed=[start_year] if self.load_status == "failed" else [],
            reason=self.reason,
        )

    async def curve_on(self, curve, day=None):
        days = [d for d in sorted(self.days[curve]) if day is None or d <= day]
        if not days:
            return None
        points = self.days[curve][days[-1]]
        order = [
            "1M",
            "6W",
            "2M",
            "3M",
            "4M",
            "6M",
            "1Y",
            "2Y",
            "3Y",
            "5Y",
            "7Y",
            "10Y",
            "20Y",
            "30Y",
        ]
        return StoredDay(days[-1], {t: points[t] for t in order if t in points})

    async def history(self, curve, tenors, start=None, end=None):
        return {
            tenor: [
                (day, points[tenor])
                for day, points in sorted(self.days[curve].items())
                if tenor in points
                and (start is None or day >= start)
                and (end is None or day <= end)
            ]
            for tenor in tenors
        }


@pytest.fixture
def curves():
    original = getattr(app.state, "treasury_curves", None)
    yield FakeCurves()
    app.state.treasury_curves = original


@pytest.fixture
def client(curves):
    with TestClient(app) as test_client:
        app.state.treasury_curves = curves
        yield test_client


def by_tenor(points) -> dict:
    return {point["tenor"]: point for point in points}


class TestCurve:
    def test_the_latest_curve_with_its_breakeven_inflation(self, client, curves):
        response = client.get("/macro/treasury/curve")

        assert response.status_code == 200
        body = response.json()
        assert (body["curve"], body["date"], body["requested_date"]) == (
            "nominal",
            "2026-10-16",
            None,
        )
        assert [point["tenor"] for point in body["points"]][:3] == ["1M", "6W", "2M"]
        assert len(body["points"]) == 14
        assert by_tenor(body["points"])["10Y"] == {"tenor": "10Y", "months": 120.0, "value": 0.0509}
        assert by_tenor(body["points"])["6W"]["months"] == 1.5
        breakeven = {point["tenor"]: point["value"] for point in body["breakeven"]}
        assert breakeven == pytest.approx(
            {"5Y": 0.0237, "7Y": 0.0236, "10Y": 0.0235, "20Y": 0.0246, "30Y": 0.0227}
        )
        assert body["load"]["status"] == "fresh"
        assert curves.asked == [("nominal", 2026, 2026, False), ("real", 2026, 2026, False)]

    def test_a_sunday_gives_the_friday_before(self, client):
        body = client.get("/macro/treasury/curve", params={"date": "2026-10-18"}).json()
        assert (body["requested_date"], body["date"]) == ("2026-10-18", "2026-10-16")

    def test_new_years_day_reads_the_year_before(self, client, curves):
        body = client.get("/macro/treasury/curve", params={"date": "2026-01-01"}).json()
        assert body["date"] == "2025-12-31"
        assert body["breakeven"] == []  # no real yield stored that day
        assert ("nominal", 2025, 2025, False) in curves.asked

    def test_the_real_curve(self, client):
        body = client.get("/macro/treasury/curve", params={"curve": "real"}).json()
        assert [point["tenor"] for point in body["points"]] == ["5Y", "7Y", "10Y", "20Y", "30Y"]
        assert by_tenor(body["breakeven"])["10Y"]["value"] == pytest.approx(0.0235)

    def test_compared_with_a_date_and_with_a_year_before(self, client):
        body = client.get(
            "/macro/treasury/curve", params=[("compare", "2026-01-02"), ("compare", "1y")]
        ).json()

        new_year, year_before = body["comparisons"]
        assert (new_year["requested"], new_year["date"]) == ("2026-01-02", "2026-01-02")
        ten_years = by_tenor(new_year["points"])["10Y"]
        assert ten_years["value"] == 0.0419
        assert ten_years["change"] == pytest.approx(0.0509 - 0.0419)
        # A year before 2026-10-16 is 2025-10-16: the fixture holds 2025-02-18 before it.
        assert (year_before["requested"], year_before["date"]) == ("1y", "2025-02-18")
        assert by_tenor(year_before["points"])["6W"]["change"] == pytest.approx(0.0414 - 0.0441)

    def test_a_comparison_before_any_day_stored(self, client):
        body = client.get("/macro/treasury/curve", params={"compare": "40Y"}).json()
        assert body["comparisons"] == [{"requested": "40Y", "date": None, "points": []}]

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"compare": "yesterday"}, "compare takes a date YYYY-MM-DD or 1W, 1M, 3M, 1Y"),
            ([("compare", f"{n}M") for n in range(1, 7)], "At most 5 curves to compare with"),
            ({"curve": "zero"}, "String should match pattern"),
            ({"date": "2026-13-01"}, "valid date"),
        ],
    )
    def test_refused(self, client, params, message):
        response = client.get("/macro/treasury/curve", params=params)
        assert response.status_code == 422
        assert message in str(response.json()["detail"])

    def test_no_curve_on_or_before_the_day(self, client):
        response = client.get("/macro/treasury/curve", params={"date": "1989-12-31"})
        assert response.status_code == 404
        assert response.json()["detail"] == (
            "No nominal Treasury curve on or before 1989-12-31 (the curve starts in 1990)"
        )

    def test_the_treasury_down_and_nothing_stored(self, client, curves):
        curves.days["nominal"] = {}
        curves.load_status, curves.reason = "failed", "the Treasury did not answer"
        response = client.get("/macro/treasury/curve")
        assert response.status_code == 502
        assert (
            response.json()["detail"]
            == "The Treasury could not be read: the Treasury did not answer"
        )

    def test_the_treasury_down_but_days_stored(self, client, curves):
        curves.load_status, curves.reason = "failed", "the Treasury did not answer"
        body = client.get("/macro/treasury/curve").json()
        assert (body["date"], body["load"]["status"]) == ("2026-10-16", "failed")
        assert body["load"]["reason"] == "the Treasury did not answer"


class TestSeries:
    def test_the_10_year_rate_over_the_last_10_years(self, client, curves):
        body = client.get("/macro/treasury/series").json()

        assert (body["start"], body["end"]) == ("2016-10-17", "2026-10-17")
        (ten_years,) = body["series"]
        assert ten_years["tenor"] == "10Y"
        assert ten_years["observations"][0] == {"date": "2025-01-02", "value": 0.0457}
        assert ten_years["observations"][-1] == {"date": "2026-10-16", "value": 0.0509}
        assert curves.asked == [("nominal", 2016, 2026, False)]

    def test_the_10_year_minus_2_year_spread(self, client):
        body = client.get(
            "/macro/treasury/series", params={"tenor": "10y-2y", "start": "2026-10-01"}
        ).json()
        assert body["series"][0]["tenor"] == "10Y-2Y"
        assert [o["date"] for o in body["series"][0]["observations"]] == [
            "2026-10-14",
            "2026-10-15",
            "2026-10-16",
        ]
        assert body["series"][0]["observations"][-1]["value"] == pytest.approx(0.0035)

    def test_a_spread_only_on_the_days_of_both_maturities(self, client):
        body = client.get(
            "/macro/treasury/series", params={"tenor": "6W-1M", "start": "2025-01-01"}
        ).json()
        days = [o["date"] for o in body["series"][0]["observations"]]
        assert "2025-02-14" not in days and days[0] == "2025-02-18"

    def test_several_maturities_repeated_or_separated_by_commas(self, client):
        body = client.get(
            "/macro/treasury/series",
            params=[("tenor", "2Y,10Y"), ("tenor", "10Y"), ("tenor", "30Y")],
        ).json()
        assert [series["tenor"] for series in body["series"]] == ["2Y", "10Y", "30Y"]

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"tenor": "2Y", "curve": "real"}, "'2Y' is not a maturity of the real curve"),
            ({"tenor": "15Y"}, "'15Y' is not a maturity of the nominal curve"),
            ({"tenor": "10Y-2Y-1M"}, "nor a spread of two"),
            ({"tenor": ","}, "tenor names no maturity"),
            ({"tenor": "1M,2M,3M,6M,1Y,2Y,5Y"}, "At most 6 maturities or spreads"),
            ({"start": "2026-10-16", "end": "2026-01-01"}, "start is after end"),
        ],
    )
    def test_refused(self, client, params, message):
        response = client.get("/macro/treasury/series", params=params)
        assert response.status_code == 422
        assert message in response.json()["detail"]

    def test_the_treasury_down_and_nothing_stored(self, client, curves):
        curves.days["nominal"] = {}
        curves.load_status, curves.reason = "failed", "the Treasury did not answer"
        assert client.get("/macro/treasury/series").status_code == 502

    def test_nothing_in_the_period(self, client):
        body = client.get(
            "/macro/treasury/series", params={"start": "2010-01-01", "end": "2010-12-31"}
        ).json()
        assert body["series"] == [{"tenor": "10Y", "observations": []}]


class TestRefresh:
    def test_both_curves_of_the_current_year(self, client, curves):
        response = client.post("/macro/treasury/refresh")
        assert response.status_code == 200
        assert [load["curve"] for load in response.json()["loads"]] == ["nominal", "real"]
        assert curves.asked == [("nominal", 2026, 2026, False), ("real", 2026, 2026, False)]

    def test_the_whole_history_of_one_curve(self, client, curves):
        client.post(
            "/macro/treasury/refresh",
            params={"curve": "nominal", "start_year": 1990, "force": "true"},
        )
        assert curves.asked == [("nominal", 1990, 2026, True)]

    @pytest.mark.parametrize(
        ("params", "status"),
        [
            ({"curve": "zero"}, 422),
            ({"start_year": 2026, "end_year": 2020}, 422),
            ({"start_year": 1985}, 422),
        ],
    )
    def test_refused(self, client, params, status):
        assert client.post("/macro/treasury/refresh", params=params).status_code == status


def test_without_database_the_routes_are_unavailable(curves):
    with TestClient(app) as test_client:
        app.state.treasury_curves = None
        for response in (
            test_client.get("/macro/treasury/curve"),
            test_client.get("/macro/treasury/series"),
            test_client.post("/macro/treasury/refresh"),
        ):
            assert response.status_code == 503


@pytest.mark.parametrize(
    ("day", "requested", "before"),
    [
        (date(2026, 10, 16), "1W", date(2026, 10, 9)),
        (date(2026, 10, 16), "10D", date(2026, 10, 6)),
        (date(2026, 3, 31), "1M", date(2026, 2, 28)),
        (date(2024, 2, 29), "1Y", date(2023, 2, 28)),
        (date(2026, 1, 15), "3M", date(2025, 10, 15)),
        (date(2026, 10, 16), "2025-06-30", date(2025, 6, 30)),
    ],
)
def test_the_day_of_a_comparison(day, requested, before):
    assert _before(day, requested) == before


def test_values_are_ratios_of_the_files():
    assert FakeCurves().days["real"][date(2026, 10, 16)]["5Y"] == Decimal("0.0252")


class TestOpenBB:
    def test_the_curve_of_today_and_of_the_days_compared(self, client):
        response = client.get("/openbb/macro/treasury/curve/chart")

        assert response.status_code == 200
        figure = response.json()
        today, month, year = figure["data"]
        assert [trace["name"] for trace in figure["data"]] == [
            "2026-10-16",
            "1M before (2026-01-02)",  # the fixture holds no day between 2026-01-02 and 10-14
            "1Y before (2025-02-18)",
        ]
        assert today["x"][:3] == ["1M", "6W", "2M"] and today["x"][-1] == "30Y"
        assert today["y"][today["x"].index("10Y")] == 0.0509
        assert (today["line"]["dash"], month["line"]["dash"]) == ("solid", "dot")
        assert year["y"][year["x"].index("10Y")] == 0.0455
        assert figure["layout"]["title"] == "US Treasury curve, par yields — 2026-10-16"
        assert figure["layout"]["xaxis"]["type"] == "category"
        assert figure["layout"]["yaxis"]["tickformat"] == ".2%"

    def test_compared_with_a_date_or_with_nothing(self, client):
        dated = client.get(
            "/openbb/macro/treasury/curve/chart", params={"compare": "2026-01-02, 40Y"}
        ).json()
        alone = client.get("/openbb/macro/treasury/curve/chart", params={"compare": ""}).json()

        # A day before any stored one draws no line.
        assert [trace["name"] for trace in dated["data"]] == ["2026-10-16", "2026-01-02"]
        assert len(alone["data"]) == 1

    def test_the_real_curve(self, client):
        figure = client.get("/openbb/macro/treasury/curve/chart", params={"curve": "real"}).json()
        assert figure["data"][0]["x"] == ["5Y", "7Y", "10Y", "20Y", "30Y"]
        assert figure["layout"]["title"] == "US Treasury curve, real yields (TIPS) — 2026-10-16"

    def test_the_10_year_minus_2_year_spread_with_its_zero_line(self, client):
        figure = client.get(
            "/openbb/macro/treasury/series/chart", params={"start": "2026-10-01"}
        ).json()

        (spread,) = figure["data"]
        assert spread["name"] == "10Y-2Y"
        assert spread["x"] == ["2026-10-14", "2026-10-15", "2026-10-16"]
        assert spread["y"][-1] == pytest.approx(0.0035)
        assert figure["layout"]["yaxis"]["title"] == "Spread"
        assert figure["layout"]["shapes"][0]["y0"] == 0

    def test_maturities_without_zero_line(self, client):
        figure = client.get(
            "/openbb/macro/treasury/series/chart", params={"tenor": "2Y,10Y"}
        ).json()
        assert [trace["name"] for trace in figure["data"]] == ["2Y", "10Y"]
        assert figure["layout"]["yaxis"]["title"] == "Yield"
        assert "shapes" not in figure["layout"]

    def test_refusals_are_those_of_the_routes(self, client):
        assert (
            client.get("/openbb/macro/treasury/series/chart", params={"tenor": "15Y"}).status_code
            == 422
        )
        assert (
            client.get(
                "/openbb/macro/treasury/curve/chart", params={"date": "1989-12-31"}
            ).status_code
            == 404
        )

    def test_an_empty_field_of_the_widget_is_left_out(self, client):
        # OpenBB sends an empty date field as ``date=``: the latest curve, 10 years.
        curve = client.get("/openbb/macro/treasury/curve/chart?date=&compare=1M")
        series = client.get("/openbb/macro/treasury/series/chart?start=")
        assert (curve.status_code, series.status_code) == (200, 200)
        assert curve.json()["data"][0]["name"] == "2026-10-16"

    def test_a_date_field_that_is_not_a_date(self, client):
        response = client.get("/openbb/macro/treasury/series/chart?start=yesterday")
        assert response.status_code == 422
        assert response.json()["detail"] == "start is not a date YYYY-MM-DD: 'yesterday'"
