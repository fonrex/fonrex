"""``/macro/indicators``: the indicators of Eurostat, latest values and series.

The service is replaced by a fake one: reading and storing the series is tested
in ``tests/test_eurostat_service.py`` and on PostgreSQL. No test reaches Eurostat.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from macro.eurostat_service import INDICATORS, Observation, SeriesLoad
from main import app

READ_AT = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
STORED = {
    ("hicp", "EA21"): [
        Observation(date(2026, 7, 1), Decimal("0.029")),
        Observation(date(2026, 8, 1), Decimal("0.032")),
        Observation(date(2026, 9, 1), Decimal("0.038"), "e"),
    ],
    ("hicp", "FR"): [Observation(date(2026, 9, 1), Decimal("0.034"), "e")],
    ("unemployment", "EA21"): [Observation(date(2026, 8, 1), Decimal("0.064"))],
    ("economic_sentiment", "EA21"): [
        Observation(date(2026, 8, 1), Decimal("98.4")),
        Observation(date(2026, 9, 1), Decimal("97.9")),
    ],
}


def _service(failing=()):
    service = MagicMock()

    async def refresh(name, geo, force=False):
        stored = STORED.get((name, geo), [])
        first = stored[0].period if stored else None
        last = stored[-1].period if stored else None
        if (name, geo) in failing:
            return SeriesLoad(name, geo, "failed", None, first, last, reason="EurostatError: down")
        return SeriesLoad(name, geo, "fetched" if force else "fresh", READ_AT, first, last, 3)

    async def latest(name, geo, count=2):
        return list(reversed(STORED.get((name, geo), [])))[:count]

    async def observations(name, geo, start=None, end=None):
        return [
            o
            for o in STORED.get((name, geo), [])
            if (start is None or o.period >= start) and (end is None or o.period <= end)
        ]

    service.refresh = AsyncMock(side_effect=refresh)
    service.latest = AsyncMock(side_effect=latest)
    service.observations = AsyncMock(side_effect=observations)
    return service


@pytest.fixture
def eurostat():
    original = getattr(app.state, "eurostat_service", None)
    service = _service(failing={("hicp_core", "EA21")})
    app.state.eurostat_service = service
    yield service
    app.state.eurostat_service = original


@pytest.fixture
def client(eurostat):
    with TestClient(app) as test_client:
        app.state.eurostat_service = eurostat
        yield test_client


class TestLatestValues:
    def test_every_indicator_of_the_euro_area(self, client, eurostat):
        response = client.get("/macro/indicators")

        assert response.status_code == 200
        body = response.json()
        assert body["geo"] == "EA21"
        assert [item["indicator"] for item in body["indicators"]] == list(INDICATORS)
        hicp = body["indicators"][0]
        assert hicp == {
            "indicator": "hicp",
            "label": INDICATORS["hicp"].label,
            "frequency": "monthly",
            "unit": "ratio",
            "period": "2026-09-01",
            "value": 0.038,
            "status": "e",
            "previous_period": "2026-08-01",
            "previous_value": 0.032,
            "change": pytest.approx(0.006),
            "load": {
                "status": "fresh",
                "fetched_at": "2026-10-15T09:00:00Z",
                "first_period": "2026-07-01",
                "last_period": "2026-09-01",
                "reason": None,
            },
        }

    def test_an_index_keeps_its_unit_and_a_single_value_has_no_change(self, client):
        body = client.get("/macro/indicators?geo=ea21").json()
        values = {item["indicator"]: item for item in body["indicators"]}

        sentiment = values["economic_sentiment"]
        assert (sentiment["unit"], sentiment["value"], sentiment["change"]) == (
            "index",
            97.9,
            pytest.approx(-0.5),
        )
        unemployment = values["unemployment"]
        assert (unemployment["value"], unemployment["previous_value"], unemployment["change"]) == (
            0.064,
            None,
            None,
        )

    def test_a_failed_series_without_value_says_why(self, client):
        body = client.get("/macro/indicators", params={"indicator": "hicp_core"}).json()

        assert body["indicators"][0]["value"] is None
        assert body["indicators"][0]["load"]["status"] == "failed"
        assert body["indicators"][0]["load"]["reason"] == "EurostatError: down"

    def test_some_indicators_of_a_country(self, client, eurostat):
        response = client.get(
            "/macro/indicators", params={"geo": "FR", "indicator": ["HICP", "hicp"]}
        )

        assert response.status_code == 200
        assert [item["indicator"] for item in response.json()["indicators"]] == ["hicp"]
        assert eurostat.refresh.await_args.args == ("hicp", "FR")

    @pytest.mark.parametrize(
        ("params", "status"),
        [
            ({"indicator": "gdp"}, 404),
            ({"geo": "FR,DE"}, 422),
            ({"geo": "F"}, 422),
            ({"geo": " , "}, 422),
        ],
    )
    def test_a_request_that_cannot_be_answered(self, client, params, status):
        assert client.get("/macro/indicators", params=params).status_code == status


class TestSeries:
    def test_one_series_per_country(self, client):
        response = client.get("/macro/indicators/hicp", params={"geo": "EA,FR"})

        assert response.status_code == 200
        body = response.json()
        assert (body["indicator"], body["frequency"], body["unit"]) == ("hicp", "monthly", "ratio")
        assert [series["geo"] for series in body["series"]] == ["EA21", "FR"]
        assert body["series"][0]["observations"][-1] == {
            "period": "2026-09-01",
            "value": 0.038,
            "status": "e",
        }
        assert body["series"][1]["observations"] == [
            {"period": "2026-09-01", "value": 0.034, "status": "e"}
        ]

    def test_the_periods_asked(self, client):
        body = client.get(
            "/macro/indicators/hicp", params={"start": "2026-08-01", "end": "2026-08-31"}
        ).json()
        assert [o["period"] for o in body["series"][0]["observations"]] == ["2026-08-01"]

    def test_the_same_country_twice_is_one_series(self, client):
        body = client.get("/macro/indicators/hicp", params={"geo": "EA,EA21,ea"}).json()
        assert [series["geo"] for series in body["series"]] == ["EA21"]

    @pytest.mark.parametrize(
        ("path", "params", "status"),
        [
            ("/macro/indicators/gdp", {}, 404),
            ("/macro/indicators/hicp", {"start": "2026-09-01", "end": "2026-01-01"}, 422),
            (
                "/macro/indicators/hicp",
                {
                    "geo": ",".join(
                        ["DE", "FR", "IT", "ES", "NL", "BE", "AT", "PT", "IE", "FI", "GR"]
                    )
                },
                422,
            ),
            ("/macro/indicators/hicp", {"geo": "EA;DROP"}, 422),
        ],
    )
    def test_a_request_that_cannot_be_answered(self, client, path, params, status):
        assert client.get(path, params=params).status_code == status


class TestRefresh:
    def test_every_indicator_of_each_country_is_read(self, client, eurostat):
        response = client.post(
            "/macro/indicators/refresh", params={"geo": "EA,FR", "force": "true"}
        )

        assert response.status_code == 200
        loads = response.json()["loads"]
        assert len(loads) == 2 * len(INDICATORS)
        assert loads[0] == {
            "indicator": "hicp",
            "geo": "EA21",
            "received": 3,
            "status": "fetched",
            "fetched_at": "2026-10-15T09:00:00Z",
            "first_period": "2026-07-01",
            "last_period": "2026-09-01",
            "reason": None,
        }
        assert eurostat.refresh.await_args_list[0].kwargs == {"force": True}

    def test_one_indicator_without_force(self, client, eurostat):
        response = client.post("/macro/indicators/refresh", params={"indicator": "unemployment"})

        assert [load["indicator"] for load in response.json()["loads"]] == ["unemployment"]
        assert eurostat.refresh.await_args.kwargs == {"force": False}

    def test_an_unknown_indicator_is_refused(self, client):
        response = client.post("/macro/indicators/refresh", params={"indicator": "gdp"})
        assert response.status_code == 404


def test_without_database_the_indicators_are_unavailable():
    original = getattr(app.state, "eurostat_service", None)
    with TestClient(app) as test_client:
        app.state.eurostat_service = None
        try:
            for response in (
                test_client.get("/macro/indicators"),
                test_client.get("/macro/indicators/hicp"),
                test_client.post("/macro/indicators/refresh"),
            ):
                assert response.status_code == 503
                assert "Eurostat" in response.json()["detail"]
        finally:
            app.state.eurostat_service = original


class TestOpenBB:
    def test_one_tile_per_indicator_with_its_change(self, client):
        response = client.get("/openbb/macro/indicators")

        assert response.status_code == 200
        tiles = response.json()
        assert len(tiles) == len(INDICATORS)
        assert tiles[0] == {
            "label": "Inflation (HICP) (Sep 2026, estimate)",
            "value": "3.80%",
            "delta": 0.6,  # percentage points
        }
        by_label = {tile["label"].split(" (")[0]: tile for tile in tiles}
        assert by_label["Core inflation"] == {
            "label": "Core inflation (not refreshed)",
            "value": "N/A",
            "delta": None,
        }
        assert by_label["Unemployment"]["value"] == "6.40%"
        assert by_label["Unemployment"]["delta"] is None
        assert by_label["Economic sentiment"]["value"] == "97.9"
        assert by_label["Economic sentiment"]["delta"] == -0.5

    def test_a_country_is_passed_on(self, client, eurostat):
        assert client.get("/openbb/macro/indicators", params={"geo": "FR"}).status_code == 200
        assert eurostat.refresh.await_args.args[1] == "FR"

    def test_one_line_per_country(self, client, eurostat):
        response = client.get(
            "/openbb/macro/indicators/hicp/chart", params={"geo": "EA,FR", "start": "2026-08-01"}
        )

        assert response.status_code == 200
        figure = response.json()
        assert [trace["name"] for trace in figure["data"]] == ["EA21", "FR"]
        assert figure["data"][0]["x"] == ["2026-08-01", "2026-09-01"]
        assert figure["data"][0]["y"] == [0.032, 0.038]
        assert figure["layout"]["yaxis"]["tickformat"] == ".1%"
        assert figure["layout"]["title"] == "Inflation (HICP) — Eurostat"

    def test_the_chart_covers_ten_years_by_default(self, client, eurostat):
        client.get("/openbb/macro/indicators/economic_sentiment/chart")

        start = eurostat.observations.await_args.args[2]
        assert (start.year, start.month, start.day) == (date.today().year - 10, 1, 1)
        assert [call.args[1] for call in eurostat.observations.await_args_list] == [
            "EA21",
            "DE",
            "FR",
            "IT",
            "ES",
        ]

    def test_an_index_is_not_shown_as_a_percentage(self):
        from integrations.openbb.adapters import format_macro_indicator_chart

        figure = format_macro_indicator_chart(
            {"indicator": "consumer_confidence", "unit": "balance", "series": []}
        )
        assert figure["layout"]["yaxis"] == {"title": "Balance", "tickformat": ".1f"}

    def test_a_quarter_is_named_as_such(self):
        from integrations.openbb.adapters import format_macro_indicators_metric

        tiles = format_macro_indicators_metric(
            {
                "indicators": [
                    {
                        "indicator": "gdp_qoq",
                        "frequency": "quarterly",
                        "unit": "ratio",
                        "period": "2026-04-01",
                        "value": 0.006,
                        "change": 0.006,
                        "load": {"status": "fresh"},
                    }
                ]
            }
        )
        assert tiles == [{"label": "GDP q/q (2026-Q2)", "value": "0.60%", "delta": 0.6}]

    def test_nothing_to_show(self):
        from integrations.openbb.adapters import format_macro_indicators_metric

        assert format_macro_indicators_metric({"indicators": []}) == [
            {"label": "Eurostat indicators", "value": "N/A", "delta": None}
        ]

    def test_an_unknown_indicator_is_refused(self, client):
        assert client.get("/openbb/macro/indicators/gdp/chart").status_code == 404
