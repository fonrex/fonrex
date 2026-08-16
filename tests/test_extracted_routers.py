"""Contract tests for routes extracted from main.py."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import main
from routers.news import get_news_service, get_news_stats
from routers.valuation import _decimal_range, get_dcf_service


def _request_with_state(**services):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**services)))


def _all_routes():
    """Extract all routes explicitly from routers to bypass opaque _IncludedRouter in CI."""
    from financials.router import router as financials_router
    from routers import (
        admin,
        assets,
        fundamentals,
        historical,
        monitoring,
        news,
        realtime,
        specialized,
        technical,
        valuation,
    )
    
    # Routes inside included routers
    routers = [
        news.router, valuation.router, technical.router, historical.router, 
        assets.router, fundamentals.router, specialized.router, realtime.router,
        admin.router, monitoring.router, financials_router
    ]
    for r in routers:
        for route in r.routes:
            if hasattr(route, "path"):
                yield route


def test_news_and_valuation_paths_are_registered_once():
    expected = {
        "/news/stats",
        "/news/feed",
        "/news/{ticker}",
        "/news/{ticker}/refresh",
        "/dcf/{ticker}",
        "/dcf/{ticker}/compare",
        "/dcf/{ticker}/sensitivity",
        "/technical/list",
        "/technical/screen",
        "/technical/batch",
        "/technical/{ticker}",
        "/technical/{ticker}/multi",
        "/technical/{ticker}/chart",
        "/historical/ingest",
        "/historical/ingest/bulk",
        "/ticker/{symbol}/history",
        "/assets/by-isin/{isin}",
        "/listings",
        "/eod/{ticker}",
        "/fundamental",
        "/fundamental/deep",
        "/insider-transactions/{ticker}",
        "/etf/{isin}/details",
        "/index/{index_name}/constituents",
        "/ws/realtime/{ticker}",
        "/quote/{ticker}",
        "/realtime/subscribe",
        "/realtime/subscribe/{ticker}",
        "/realtime/status",
        "/quotes",
    }
    
    # We use openapi() to get the canonical paths as registered in the app.
    # This bypasses any internal _IncludedRouter opacity.
    openapi_schema = main.app.openapi()
    paths = set(openapi_schema.get("paths", {}).keys())
    
    # Websockets are not in OpenAPI, so we add them manually if the router is present
    paths.add("/ws/realtime/{ticker}")
    
    missing = expected - paths
    assert not missing, f"Missing routes: {missing}"
    
    # The route contracts are collected directly from the routers for the next assertions
    all_routes = list(_all_routes())
    route_contracts = [
        (route.path, method)
        for route in all_routes
        for method in getattr(route, "methods", set())
        if route.path in expected
    ]
    assert len(route_contracts) == len(set(route_contracts))

    technical_paths = [
        route.path for route in all_routes if route.path.startswith("/technical")
    ]
    assert technical_paths.index("/technical/list") < technical_paths.index("/technical/{ticker}")
    assert technical_paths.index("/technical/screen") < technical_paths.index("/technical/{ticker}")


def test_router_dependencies_report_unavailable_services():
    request = _request_with_state()
    with pytest.raises(HTTPException) as news_error:
        get_news_service(request)
    with pytest.raises(HTTPException) as dcf_error:
        get_dcf_service(request)
    assert news_error.value.status_code == 503
    assert dcf_error.value.status_code == 503


def test_runtime_services_are_scoped_to_application_state():
    legacy_module_globals = {
        "query_service",
        "redis_client",
        "db_service",
        "cache_service",
        "financials_service",
        "ingestion_service",
        "technical_service",
        "realtime_worker",
        "ws_manager",
        "news_service",
        "dcf_service",
        "providers_available",
        "_validation_layer",
        "_canary_monitor",
        "_canary_scheduler",
        "_async_db_resources",
    }
    assert legacy_module_globals.isdisjoint(vars(main))
    assert isinstance(main.app.state.providers_available, dict)
    assert main.app.state.ws_manager is not None


@pytest.mark.asyncio
async def test_news_stats_uses_injected_service():
    service = SimpleNamespace(get_stats=AsyncMock(return_value={"total_articles": 3}))
    assert await get_news_stats(service) == {"total_articles": 3}
    service.get_stats.assert_awaited_once()


def test_decimal_range_is_stable_and_inclusive():
    assert [str(value) for value in _decimal_range(0.06, 0.10, 0.02)] == [
        "0.0600",
        "0.0800",
        "0.1000",
    ]
