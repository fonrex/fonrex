"""``POST /database/cleanup`` cannot delete the price history by accident."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database.maintenance import MAX_DAYS_TO_KEEP, MIN_DAYS_TO_KEEP
from database.price_series import session_timestamp
from database.service import DatabaseService
from models import Asset, AssetListing, Base, IngestLog, PriceEOD, UsageLog
from routers import admin

TODAY = date.today()


def _bar(days_ago: int) -> PriceEOD:
    return PriceEOD(
        timestamp=session_timestamp(TODAY - timedelta(days=days_ago)),
        asset_id=1,
        asset_listing_id=10,
        resolution="1D",
        open=1,
        high=1,
        low=1,
        close=1,
        volume=1,
    )


@pytest.fixture
def database():
    service = DatabaseService("sqlite:///:memory:")
    Base.metadata.create_all(service.engine)
    session = service.get_session()
    session.add(Asset(id=1, ticker="AAPL", name="Apple", is_active=True))
    session.add(AssetListing(id=10, asset_id=1, ticker="AAPL", currency="USD", is_active=True))
    # Ten years of history, as a first ingestion fetches it.
    session.add_all(_bar(days_ago) for days_ago in (0, 1, 29, 30, 31, 400, 800, 3000))
    recent, old = datetime.now(UTC) - timedelta(days=2), datetime.now(UTC) - timedelta(days=45)
    for created_at in (recent, old):
        session.add(
            UsageLog(
                endpoint="/eod/AAPL",
                method="GET",
                status_code=200,
                latency_ms=5,
                created_at=created_at.replace(tzinfo=None),
            )
        )
        session.add(
            IngestLog(
                asset_id=1,
                ticker="AAPL",
                resolution="1D",
                source="yfinance",
                status="success",
                created_at=created_at,
            )
        )
    session.commit()
    session.close()
    yield service
    service.close()


def _ages(database) -> list[int]:
    session = database.get_session()
    try:
        return sorted(
            (TODAY - bar.timestamp.date()).days for bar in session.query(PriceEOD).all()
        )
    finally:
        session.close()


class TestRetention:
    def test_bars_older_than_the_limit_are_deleted_and_the_limit_day_is_kept(self, database):
        success, result, error = database.cleanup_old_data(days_to_keep=30)

        assert (success, error) == (True, None)
        assert _ages(database) == [0, 1, 29, 30]
        assert result["status"] == "success"
        assert result["deleted_records"] == 4
        assert result["cutoff_date"] == (TODAY - timedelta(days=30)).isoformat()
        # Logs older than 30 days go with them, recent ones stay.
        assert (result["deleted_usage_logs"], result["deleted_ingest_logs"]) == (1, 1)

    def test_default_keeps_two_years(self, database):
        database.cleanup_old_data()

        assert _ages(database) == [0, 1, 29, 30, 31, 400]

    @pytest.mark.parametrize("days", [0, -1, -3650, 1, MIN_DAYS_TO_KEEP - 1, MAX_DAYS_TO_KEEP + 1])
    def test_value_that_would_empty_the_history_is_refused(self, database, days):
        """``0`` and negative values put the limit today or in the future: every bar was deleted."""
        success, result, error = database.cleanup_old_data(days_to_keep=days)

        assert (success, result) == (False, None)
        assert "days_to_keep must be a whole number between 30 and 36500" in error
        assert len(_ages(database)) == 8

    @pytest.mark.parametrize("days", [None, "30", 30.5, True])
    def test_value_that_is_not_a_whole_number_is_refused(self, database, days):
        success, _result, _error = database.cleanup_old_data(days_to_keep=days)

        assert success is False
        assert len(_ages(database)) == 8

    def test_dry_run_counts_and_deletes_nothing(self, database):
        success, result, _error = database.cleanup_old_data(days_to_keep=365, dry_run=True)

        assert success is True
        assert result["status"] == "dry_run"
        assert result["deleted_records"] == 3
        assert (result["deleted_usage_logs"], result["deleted_ingest_logs"]) == (1, 1)
        assert len(_ages(database)) == 8
        # The real run deletes what the simulation counted.
        assert database.cleanup_old_data(days_to_keep=365)[1]["deleted_records"] == 3
        assert _ages(database) == [0, 1, 29, 30, 31]


class TestRoute:
    @pytest.fixture
    def client(self):
        application = FastAPI()
        application.include_router(admin.router)
        database = MagicMock()
        database.cleanup_old_data.return_value = (True, {"status": "success"}, None)
        application.state.db_service = database
        application.state.db_available = True
        with TestClient(application) as test_client:
            test_client.database = database
            yield test_client

    @pytest.mark.parametrize("days", [0, -1, 29, MAX_DAYS_TO_KEEP + 1, "all", 30.5, None])
    def test_out_of_bounds_request_is_rejected_before_reaching_the_database(self, client, days):
        response = client.post("/database/cleanup", json={"days_to_keep": days})

        assert response.status_code == 422
        client.database.cleanup_old_data.assert_not_called()

    def test_default_and_explicit_requests(self, client):
        assert client.post("/database/cleanup", json={}).status_code == 200
        client.database.cleanup_old_data.assert_called_with(730, False)

        client.post("/database/cleanup", json={"days_to_keep": 30, "dry_run": True})
        client.database.cleanup_old_data.assert_called_with(30, True)
