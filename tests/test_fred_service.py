import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

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
@patch("os.environ.get")
async def test_get_risk_free_rate_fallback(mock_env, fred_service):
    """Test that when API and cache fail, it falls back to the .env variable."""
    # Ensure Redis returns None
    fred_service.redis_client.get.return_value = None
    
    # Ensure DB returns None
    fred_service.db_service.get_session.return_value.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = None
    
    # Disable API fetch by removing key
    fred_service.api_key = None
    
    # Set env var
    def env_side_effect(key, default=None):
        if key == "DCF_RISK_FREE_RATE":
            return "0.038"
        return default
    mock_env.side_effect = env_side_effect

    val, source = await fred_service.get_risk_free_rate()
    
    assert val == Decimal("0.038")
    assert source == "env_fallback"
