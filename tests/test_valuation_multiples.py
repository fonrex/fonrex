"""Tests for ValuationMultiplesService and valuation multiples endpoints."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from schemas.fundamentals import ValuationMultiplesResult
from valuation.multiples_service import ValuationMultiplesService


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.asyncio
async def test_multiples_service_empty_fallback():
    """Service returns an empty result when no data is available without throwing errors."""
    service = ValuationMultiplesService(db_service=None, redis_client=None)

    with patch.object(service, "_compute_from_yfinance", return_value=[]):
        res = await service.get_multiples("UNKNOWN_XYZ", period="FY")
        assert isinstance(res, ValuationMultiplesResult)
        assert res.ticker == "UNKNOWN_XYZ"
        assert res.period == "FY"
        assert res.series == []


@pytest.mark.asyncio
async def test_multiples_service_parsing():
    """Service properly sorts and structures points."""
    service = ValuationMultiplesService(db_service=None, redis_client=None)

    mock_points = [
        {
            "date": "2024-09-30",
            "pe_ratio": 37.26,
            "ps_ratio": 8.93,
            "pb_ratio": 61.33,
            "ev_sales_ratio": 9.13,
            "ev_ebitda": 26.51,
        },
        {
            "date": "2023-09-30",
            "pe_ratio": 27.08,
            "ps_ratio": 6.85,
            "pb_ratio": 42.27,
            "ev_sales_ratio": 7.07,
            "ev_ebitda": 21.52,
        },
    ]

    with patch.object(service, "_compute_from_yfinance", return_value=mock_points):
        res = await service.get_multiples("TEST", period="FY")
        assert len(res.series) == 2
        # Chronological order
        assert res.series[0].date == "2023-09-30"
        assert res.series[1].date == "2024-09-30"
        assert res.series[1].pe_ratio == 37.26
        assert res.series[1].ev_ebitda == 26.51


@pytest.mark.asyncio
async def test_multiples_service_inception_history():
    """Service generates historical timeline starting from IPO inception (1980 for AAPL)."""
    service = ValuationMultiplesService(db_service=None, redis_client=None)
    res = await service.get_multiples("AAPL", period="FY")
    assert len(res.series) >= 40
    # First point starts at market inception in 1980
    assert res.series[0].date == "1980-09-30"
    assert res.series[0].pe_ratio is not None
    assert res.series[0].ps_ratio is not None
    # Points are sorted chronologically
    dates = [p.date for p in res.series]
    assert dates == sorted(dates)
    # Reaches recent years (>= 2024)
    assert int(dates[-1][:4]) >= 2024


def test_dcf_multiples_endpoint(client):
    """GET /dcf/{ticker}/multiples returns ValuationMultiplesResult schema."""
    from unittest.mock import AsyncMock

    from main import app
    from schemas.fundamentals import ValuationMultiplesPoint

    mock_service = MagicMock()
    mock_service.get_multiples = AsyncMock(
        return_value=ValuationMultiplesResult(
            ticker="AAPL",
            period="FY",
            currency="USD",
            series=[
                ValuationMultiplesPoint(
                    date="2024-09-30",
                    pe_ratio=37.26,
                    ps_ratio=8.93,
                    pb_ratio=61.33,
                    ev_sales_ratio=9.13,
                    ev_ebitda=26.51,
                )
            ],
            source="Fonrex",
        )
    )

    orig_service = getattr(app.state, "multiples_service", None)
    app.state.multiples_service = mock_service
    try:
        resp = client.get("/dcf/AAPL/multiples?period=FY")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticker"] == "AAPL"
        assert data["period"] == "FY"
        assert len(data["series"]) == 1
        assert data["series"][0]["pe_ratio"] == 37.26
    finally:
        app.state.multiples_service = orig_service
