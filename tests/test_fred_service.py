from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from macro.fred_service import FREDService
from schemas.macro import MacroRate


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_session = MagicMock()
    return db


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    return redis


@pytest.fixture
def fred_service(mock_db, mock_redis):
    # Set api key directly for testing
    with patch("os.environ.get", return_value="test_key"):
        service = FREDService(mock_db, mock_redis)
        service.api_key = "test_key"
        return service


@pytest.mark.asyncio
async def test_get_series_from_redis(fred_service, mock_redis):
    """Test fetching a series from Redis cache."""
    rate = MacroRate(
        series_id="DGS10",
        value=Decimal("0.045"),
        observation_date=date(2026, 9, 8)
    )
    mock_redis.get.return_value = rate.model_dump_json()

    result = await fred_service._get_series("DGS10", "Test")

    assert result is not None
    assert result.value == Decimal("0.045")
    mock_redis.get.assert_called_once_with("macro:fred:DGS10")
    # Shouldn't call DB or API


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_fetch_fred_series_success(mock_get, fred_service):
    """Test fetching from FRED API directly."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "observations": [
            {"date": "2026-09-08", "value": "4.50"}
        ]
    }
    mock_response.raise_for_status = MagicMock()
    mock_get.return_value = mock_response

    rate = await fred_service._fetch_fred_series("DGS10", "10Y Treasury")

    assert rate is not None
    assert rate.value == Decimal("0.045")  # 4.5% -> 0.045
    assert rate.observation_date == date(2026, 9, 8)


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_fetch_fred_series_holiday_fallback(mock_get, fred_service):
    """Test when FRED returns '.' for the most recent observation."""
    mock_resp1 = MagicMock()
    mock_resp1.json.return_value = {
        "observations": [{"date": "2026-09-08", "value": "."}]
    }
    
    mock_resp2 = MagicMock()
    mock_resp2.json.return_value = {
        "observations": [
            {"date": "2026-09-08", "value": "."},
            {"date": "2026-09-07", "value": "4.25"}
        ]
    }
    
    # Return resp1 on first call, resp2 on second call
    mock_get.side_effect = [mock_resp1, mock_resp2]

    rate = await fred_service._fetch_fred_series("DGS10", "10Y Treasury")

    assert rate is not None
    assert rate.value == Decimal("0.0425")
    assert rate.observation_date == date(2026, 9, 7)
    assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_get_risk_free_rate_fallback(fred_service, monkeypatch):
    """Without FRED and without a stored rate, DCF_RISK_FREE_RATE is used."""
    import macro.fred_service as module

    monkeypatch.setattr(module, "DEFAULT_RISK_FREE_RATE", Decimal("0.038"))
    fred_service.redis_client.get.return_value = None
    fred_service.db_service.get_session.return_value.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = None
    fred_service.api_key = None

    rate = await fred_service.get_risk_free_rate()

    assert (rate.value, rate.source, rate.observation_date) == (
        Decimal("0.038"),
        "env_fallback",
        None,
    )


# ── The stored rate is refreshed ──────────────────────────────────────────────
#
# The value kept in ``macro_rates_cache`` used to be returned before FRED was
# asked, whatever its age: the risk-free rate of every valuation stayed the one
# of the first successful call.


@pytest.fixture
def stored_rates():
    """A real table of stored rates, and a service reading it without Redis."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker
    from sqlalchemy.pool import StaticPool

    from models import Base, MacroRateCache

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine))
    database = MagicMock()
    database.get_session.side_effect = lambda: Session()

    def store(value, observed, age):
        session = Session()
        session.add(
            MacroRateCache(
                series_id="DGS10",
                label="10Y",
                value=Decimal(value),
                unit="percent",
                observation_date=observed,
                fetched_at=datetime.now(timezone.utc) - age,
            )
        )
        session.commit()

    def rows():
        return Session().query(MacroRateCache).order_by(MacroRateCache.observation_date).all()

    service = FREDService(database, redis_client=None)
    service.api_key = "test_key"
    yield MagicMock(service=service, store=store, rows=rows, timedelta=timedelta)
    Session.remove()
    engine.dispose()


def _fred_answers(observed, value):
    response = MagicMock()
    response.json.return_value = {"observations": [{"date": observed, "value": value}]}
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_old_stored_rate_is_refreshed_from_fred(mock_get, stored_rates):
    stored_rates.store("0.0400", date(2026, 9, 1), stored_rates.timedelta(days=30))
    mock_get.return_value = _fred_answers("2026-10-02", "4.75")

    rate = await stored_rates.service._get_series("DGS10", "10Y")

    assert rate.value == Decimal("0.0475")
    assert rate.observation_date == date(2026, 10, 2)
    assert [row.observation_date for row in stored_rates.rows()] == [date(2026, 9, 1), date(2026, 10, 2)]


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_rate_read_from_fred_recently_is_not_asked_again(mock_get, stored_rates):
    stored_rates.store("0.0400", date(2026, 10, 2), stored_rates.timedelta(hours=1))

    rate = await stored_rates.service._get_series("DGS10", "10Y")

    assert rate.value == Decimal("0.0400")
    mock_get.assert_not_called()


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_old_stored_rate_is_what_is_left_when_fred_fails(mock_get, stored_rates):
    stored_rates.store("0.0400", date(2026, 9, 1), stored_rates.timedelta(days=30))
    mock_get.side_effect = RuntimeError("FRED is down")

    rate = await stored_rates.service._get_series("DGS10", "10Y")

    assert rate.value == Decimal("0.0400")


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_without_api_key_the_stored_rate_is_used(mock_get, stored_rates):
    stored_rates.store("0.0400", date(2026, 9, 1), stored_rates.timedelta(days=30))
    stored_rates.service.api_key = None

    rate = await stored_rates.service._get_series("DGS10", "10Y")

    assert rate.value == Decimal("0.0400")
    mock_get.assert_not_called()


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_same_observation_confirms_the_row_instead_of_adding_one(mock_get, stored_rates):
    stored_rates.store("0.0400", date(2026, 10, 2), stored_rates.timedelta(days=3))
    mock_get.return_value = _fred_answers("2026-10-02", "4.10")  # revised by FRED

    await stored_rates.service._get_series("DGS10", "10Y")
    # Confirmed a moment ago: the next request does not ask FRED again.
    again = await stored_rates.service._get_series("DGS10", "10Y")

    (row,) = stored_rates.rows()
    assert row.value == Decimal("0.0410")
    assert again.value == Decimal("0.0410")
    assert mock_get.call_count == 1


# ── Rates are ratios of any sign, and say how fresh they are ──────────────────


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_rate_read_from_fred_now_is_live_and_a_ratio(mock_get, stored_rates):
    mock_get.return_value = _fred_answers("2026-10-02", "4.12")

    rate = await stored_rates.service._get_series("DGS10", "10Y")

    assert (rate.value, rate.unit, rate.freshness) == (Decimal("0.0412"), "ratio", "live")
    assert stored_rates.rows()[0].unit == "ratio"


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_recent_stored_rate_is_cached_and_a_ratio_even_if_stored_as_percent(
    mock_get, stored_rates
):
    stored_rates.store("0.0400", date(2026, 10, 2), stored_rates.timedelta(hours=1))

    rate = await stored_rates.service._get_series("DGS10", "10Y")

    assert (rate.unit, rate.freshness) == ("ratio", "cached")


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_old_stored_rate_used_when_fred_fails_is_stale(mock_get, stored_rates):
    stored_rates.store("0.0400", date(2026, 9, 1), stored_rates.timedelta(days=30))
    mock_get.side_effect = RuntimeError("FRED is down")

    rate = await stored_rates.service.get_risk_free_rate()

    assert (rate.value, rate.source, rate.observation_date) == (
        Decimal("0.0400"),
        "fred_stale",
        date(2026, 9, 1),
    )


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_negative_rate_is_a_rate_not_a_missing_one(mock_get, stored_rates):
    # German 10-year yields were negative in 2019-2021.
    mock_get.return_value = _fred_answers("2020-08-03", "-0.52")

    rate = await stored_rates.service.get_risk_free_rate()

    assert (rate.value, rate.source) == (Decimal("-0.0052"), "fred_live")


@pytest.mark.asyncio
async def test_rate_from_redis_is_cached(fred_service, mock_redis):
    live = MacroRate(
        series_id="DGS10", value=Decimal("0.045"), observation_date=date(2026, 9, 8), freshness="live"
    )
    mock_redis.get.return_value = live.model_dump_json()

    rate = await fred_service.get_risk_free_rate()

    assert (rate.value, rate.source) == (Decimal("0.045"), "fred_cached")
