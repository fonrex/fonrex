"""Routes of the factor returns: ``GET /factors``, ``GET /factors/{dataset}``, ``POST /factors/refresh``."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from factors.french_library import DATASETS
from factors.store import FactorLoad
from main import app

FETCHED_AT = datetime(2026, 10, 10, 15, 25, tzinfo=timezone.utc)
US_3 = FactorLoad(
    "us_3",
    "monthly",
    "fresh",
    FETCHED_AT,
    date(1926, 7, 31),
    date(2026, 8, 31),
    1202,
    "CRSP 202608",
)
SERIES = {
    date(2026, 7, 31): {
        "MKT_RF": Decimal("-0.0061"),
        "SMB": Decimal("-0.0192"),
        "HML": Decimal("0.0211"),
        "RF": Decimal("0.0033"),
    },
    date(2026, 8, 31): {
        "MKT_RF": Decimal("0.0256"),
        "SMB": Decimal("0.0034"),
        "HML": Decimal("-0.0354"),
        "RF": Decimal("0.0029"),
    },
}


class FakeLibrary:
    """The factor library with what the database would hold."""

    def __init__(self):
        self.loads = {("us_3", "monthly"): US_3}
        self.refreshed = []
        self.series_asked = []
        self.refresh_answer = None

    async def status(self, dataset, frequency):
        return self.loads.get((dataset, frequency))

    async def refresh(self, dataset, frequency, force=False):
        self.refreshed.append((dataset, frequency, force))
        if self.refresh_answer is not None:
            return self.refresh_answer
        load = replace(US_3, dataset=dataset, frequency=frequency, status="fetched")
        self.loads[(dataset, frequency)] = replace(load, status="fresh")
        return load

    async def series(self, dataset, frequency, start=None, end=None):
        self.series_asked.append((dataset, frequency, start, end))
        return SERIES


@pytest.fixture
def client():
    names = ("factor_library", "redis_client", "db_service", "db_available")
    originals = {name: getattr(app.state, name, None) for name in names}
    library = FakeLibrary()
    with TestClient(app) as test_client:
        app.state.factor_library = library
        test_client.library = library
        yield test_client
    for name, value in originals.items():
        setattr(app.state, name, value)


class TestDatasets:
    def test_every_dataset_with_what_is_stored_of_each_file(self, client):
        answer = client.get("/factors").json()

        assert "Kenneth R. French" in answer["source"]
        assert [dataset["dataset"] for dataset in answer["datasets"]] == list(DATASETS)
        us_3 = answer["datasets"][0]
        assert (us_3["region"], us_3["currency"]) == ("us", "USD")
        assert us_3["factors"] == ["MKT_RF", "SMB", "HML", "RF"]
        monthly, daily = us_3["loads"]
        assert (monthly["status"], monthly["periods"], monthly["source_note"]) == (
            "fresh",
            1202,
            "CRSP 202608",
        )
        assert (daily["frequency"], daily["status"], daily["periods"]) == ("daily", "missing", 0)
        assert client.library.refreshed == []  # listing downloads nothing


class TestSeries:
    def test_stored_returns_as_ratios_oldest_first(self, client):
        answer = client.get("/factors/us_3?start=2026-07-01").json()

        assert (answer["dataset"], answer["frequency"], answer["unit"]) == (
            "us_3",
            "monthly",
            "ratio",
        )
        assert answer["currency"] == "USD"
        assert answer["data"][0] == {
            "date": "2026-07-31",
            "MKT_RF": -0.0061,
            "SMB": -0.0192,
            "HML": 0.0211,
            "RF": 0.0033,
        }
        assert answer["load"]["status"] == "fresh"
        assert client.library.series_asked == [("us_3", "monthly", date(2026, 7, 1), None)]
        assert client.library.refreshed == []

    def test_a_file_never_downloaded_is_downloaded_first(self, client):
        answer = client.get("/factors/Europe_3?frequency=daily").json()

        assert client.library.refreshed == [("europe_3", "daily", False)]
        assert answer["load"]["status"] == "fetched"
        assert answer["region"] == "europe"

    def test_a_stale_file_is_downloaded_again(self, client):
        client.library.loads[("us_3", "monthly")] = replace(US_3, status="stale")

        client.get("/factors/us_3")

        assert client.library.refreshed == [("us_3", "monthly", False)]

    def test_a_failed_download_answers_what_is_stored(self, client):
        client.library.loads[("us_3", "monthly")] = replace(US_3, status="stale")
        client.library.refresh_answer = replace(US_3, status="failed", reason="ConnectError: down")

        answer = client.get("/factors/us_3").json()

        assert (answer["load"]["status"], answer["load"]["reason"]) == (
            "failed",
            "ConnectError: down",
        )
        assert len(answer["data"]) == 2

    def test_nothing_stored_and_nothing_downloaded(self, client):
        client.library.refresh_answer = FactorLoad(
            "us_5", "daily", "failed", reason="ConnectError: down"
        )

        response = client.get("/factors/us_5?frequency=daily")

        assert response.status_code == 503
        assert "ConnectError: down" in response.json()["detail"]

    @pytest.mark.parametrize(
        "path, status",
        [
            ("/factors/us_4", 404),
            ("/factors/us_3?frequency=weekly", 422),
            ("/factors/us_3?start=2026-08-01&end=2026-07-01", 422),
        ],
        ids=["unknown dataset", "unknown frequency", "start after end"],
    )
    def test_a_request_that_cannot_be_answered(self, client, path, status):
        assert client.get(path).status_code == status


class TestRefresh:
    def test_every_dataset_monthly_by_default(self, client):
        answer = client.post("/factors/refresh").json()

        assert [(load["dataset"], load["frequency"]) for load in answer["loads"]] == [
            (name, "monthly") for name in DATASETS
        ]
        assert {load["status"] for load in answer["loads"]} == {"fetched"}

    def test_chosen_datasets_and_frequencies_forced(self, client):
        client.post(
            "/factors/refresh?dataset=us_3&dataset=EUROPE_5&dataset=us_3"
            "&frequency=monthly&frequency=daily&force=true"
        )

        assert client.library.refreshed == [
            ("us_3", "monthly", True),
            ("us_3", "daily", True),
            ("europe_5", "monthly", True),
            ("europe_5", "daily", True),
        ]

    def test_an_unknown_dataset_downloads_nothing(self, client):
        assert client.post("/factors/refresh?dataset=us_9").status_code == 404
        assert client.library.refreshed == []


def test_without_database_the_routes_are_unavailable(client):
    app.state.factor_library = None

    assert client.get("/factors").status_code == 503
    assert client.post("/factors/refresh").status_code == 503
