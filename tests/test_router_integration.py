# -*- coding: utf-8 -*-
"""
Integration tests for fonrex routers using TestClient.

"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from schemas.dcf import DCFModelResult, DCFResult, SensitivityCell, SensitivityResult, WACCResult

# Mock data for DCF
MOCK_WACC = WACCResult(
    wacc=Decimal("0.08"),
    cost_of_equity=Decimal("0.10"),
    cost_of_debt=Decimal("0.05"),
    tax_rate=Decimal("0.25"),
    weight_equity=Decimal("0.70"),
    weight_debt=Decimal("0.30"),
    beta_used=Decimal("1.1"),
)

MOCK_MODEL_RESULT = DCFModelResult(
    model_name="FCF DCF",
    intrinsic_value_per_share=Decimal("150.50"),
    upside_pct=Decimal("10.5"),
    projected_values=[
        Decimal("110"),
        Decimal("120"),
        Decimal("130"),
        Decimal("140"),
        Decimal("150"),
    ],
    terminal_value=Decimal("2000"),
    present_values=[Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100")],
    pv_terminal=Decimal("1200"),
    warnings=[],
)

MOCK_DCF_RESULT = DCFResult(
    ticker="AAPL",
    currency="USD",
    current_price=Decimal("136.20"),
    shares_outstanding=10000000,
    wacc=MOCK_WACC,
    models={"fcf": MOCK_MODEL_RESULT},
    consensus_value=Decimal("150.50"),
    consensus_upside_pct=Decimal("10.5"),
    analyst_target=Decimal("160"),
)

MOCK_SENSITIVITY_RESULT = SensitivityResult(
    ticker="AAPL",
    model="fcf",
    wacc_range=[Decimal("0.08"), Decimal("0.10")],
    growth_range=[Decimal("0.02")],
    matrix=[
        [
            SensitivityCell(
                wacc=Decimal("0.08"),
                terminal_growth=Decimal("0.02"),
                intrinsic_value=Decimal("150.50"),
                upside_pct=Decimal("10.5"),
            )
        ],
        [
            SensitivityCell(
                wacc=Decimal("0.10"),
                terminal_growth=Decimal("0.02"),
                intrinsic_value=Decimal("130.00"),
                upside_pct=Decimal("-4.5"),
            )
        ],
    ],
)


@pytest.fixture
def client():
    # Mock services
    mock_dcf = MagicMock()
    mock_dcf.compute_dcf = AsyncMock(return_value=MOCK_DCF_RESULT)
    mock_dcf.compute_sensitivity = MagicMock(return_value=MOCK_SENSITIVITY_RESULT)

    mock_news = MagicMock()
    mock_news.get_stats = AsyncMock(return_value={"total_articles": 100})
    mock_news.get_news = AsyncMock(
        return_value={
            "ticker": "AAPL",
            "isin": None,
            "count": 0,
            "providers": [],
            "cached": False,
            "articles": [],
        }
    )
    mock_news.get_feed = AsyncMock(
        return_value={"count": 0, "from_date": None, "to_date": None, "articles": []}
    )

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.setex = AsyncMock()

    # Store original state
    orig_dcf = getattr(app.state, "dcf_service", None)
    orig_news = getattr(app.state, "news_service", None)
    orig_redis = getattr(app.state, "redis_client", None)
    orig_db = getattr(app.state, "db_service", None)

    with TestClient(app) as test_client:
        # Override state AFTER startup event has completed
        app.state.dcf_service = mock_dcf
        app.state.news_service = mock_news
        app.state.redis_client = mock_redis
        app.state.db_service = MagicMock()
        yield test_client

    # Restore original state
    app.state.dcf_service = orig_dcf
    app.state.news_service = orig_news
    app.state.redis_client = orig_redis
    app.state.db_service = orig_db


def test_get_dcf_valuation_success(client):
    response = client.get("/dcf/AAPL")
    assert response.status_code == 200
    data = response.json()
    assert data["ticker"] == "AAPL"
    assert data["consensus_value"] == "150.50"
    app.state.dcf_service.compute_dcf.assert_awaited_once()


def test_get_dcf_valuation_not_found(client):
    app.state.dcf_service.compute_dcf.side_effect = ValueError("Ticker not found")
    response = client.get("/dcf/INVALID")
    assert response.status_code == 404
    assert response.json()["detail"] == "Ticker not found"


def test_post_custom_dcf_valuation(client):
    payload = {
        "models": ["fcf", "eps"],
        "projection_years": 5,
        "terminal_growth_rate": 0.02,
        "wacc_params": {"risk_free_rate": 0.04, "equity_risk_premium": 0.055},
    }
    response = client.post("/dcf/AAPL", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["ticker"] == "AAPL"


def test_compare_dcf_models(client):
    response = client.get("/dcf/AAPL/compare")
    assert response.status_code == 200
    data = response.json()
    assert data["ticker"] == "AAPL"


def test_compare_dcf_models_handle_ddm_error(client):
    # If the first call with fcf, eps, ddm raises ValueError with "dividende",
    # the router should try again with only fcf, eps.
    side_effects = [ValueError("Pas de dividende"), MOCK_DCF_RESULT]
    app.state.dcf_service.compute_dcf.side_effect = side_effects

    response = client.get("/dcf/AAPL/compare")
    assert response.status_code == 200
    data = response.json()
    assert data["ticker"] == "AAPL"
    assert (
        data["models"]["ddm"]["warnings"][0] == "Modèle DDM impossible : aucun dividende distribué."
    )


def test_get_dcf_sensitivity_success(client):
    response = client.get(
        "/dcf/AAPL/sensitivity?model=fcf&wacc_min=0.06&wacc_max=0.10&wacc_step=0.02"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ticker"] == "AAPL"
    assert data["model"] == "fcf"


def test_get_dcf_sensitivity_validation_error(client):
    # step <= 0
    response = client.get("/dcf/AAPL/sensitivity?wacc_step=0")
    assert response.status_code == 400

    # min >= max
    response = client.get("/dcf/AAPL/sensitivity?wacc_min=0.12&wacc_max=0.10")
    assert response.status_code == 400


def test_get_news_stats(client):
    response = client.get("/news/stats")
    assert response.status_code == 200
    assert response.json()["total_articles"] == 100


def test_get_news_feed(client):
    response = client.get("/news/feed?limit=20&language=fr&tickers=AAPL,MSFT")
    assert response.status_code == 200
    app.state.news_service.get_feed.assert_awaited_once_with(
        limit=20, language="fr", ticker_filter=["AAPL", "MSFT"]
    )


def test_get_ticker_news(client):
    response = client.get("/news/AAPL?limit=10&language=fr")
    assert response.status_code == 200
    app.state.news_service.get_news.assert_awaited_once_with(
        ticker="AAPL", limit=10, language="fr", force_refresh=False
    )


def test_refresh_ticker_news(client):
    response = client.post("/news/AAPL/refresh")
    assert response.status_code == 200
    assert response.json() == {"status": "queued", "ticker": "AAPL"}
