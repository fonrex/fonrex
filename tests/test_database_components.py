"""Architecture tests for the split synchronous database facade."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from database.assets import AssetRepository
from database.fundamentals import FundamentalsRepository
from database.maintenance import DatabaseMaintenance
from database.migrations import MigrationInspector
from database.service import DatabaseService
from database.usage import UsageRepository
from models import (
    AnalystRatings,
    Asset,
    Base,
    EarningsHistory,
    FinancialStatement,
    FundamentalsHighlights,
)


def test_database_service_composes_focused_components():
    service = DatabaseService("sqlite:///:memory:")
    try:
        assert isinstance(service.assets, AssetRepository)
        assert isinstance(service.fundamentals, FundamentalsRepository)
        assert isinstance(service.maintenance, DatabaseMaintenance)
        assert isinstance(service.migrations, MigrationInspector)
        assert isinstance(service.usage, UsageRepository)
        for component in (
            service.assets,
            service.fundamentals,
            service.maintenance,
            service.migrations,
            service.usage,
        ):
            assert component.engine is service.engine
            assert component.Session is service.Session
    finally:
        service.close()


def test_facade_delegates_asset_queries_without_business_logic():
    service = DatabaseService("sqlite:///:memory:")
    try:
        service.assets.get_asset_context = MagicMock(return_value={"details": {"id": 1}})
        result = service.get_asset_context(ticker="AAPL", exchange="NASDAQ")
        assert result == {"details": {"id": 1}}
        service.assets.get_asset_context.assert_called_once_with(ticker="AAPL", exchange="NASDAQ")
    finally:
        service.close()


def test_facade_delegates_fundamentals_and_usage():
    service = DatabaseService("sqlite:///:memory:")
    try:
        service.fundamentals.get_deep_fundamentals = MagicMock(return_value={"asset_id": 42})
        service.usage.log_usage = MagicMock(return_value=True)

        assert service.get_deep_fundamentals(42) == {"asset_id": 42}
        assert service.log_usage("/quote/AAPL", "GET", 200, 12) is True
        service.fundamentals.get_deep_fundamentals.assert_called_once_with(42)
        service.usage.log_usage.assert_called_once_with("/quote/AAPL", "GET", 200, 12)
    finally:
        service.close()


def test_facade_delegates_deep_section_read_model():
    service = DatabaseService("sqlite:///:memory:")
    try:
        service.fundamentals.get_deep_sections = MagicMock(
            return_value={"highlights": {"market_cap": 123}}
        )

        result = service.get_deep_sections(42, {"highlights"}, False)

        assert result == {"highlights": {"market_cap": 123}}
        service.fundamentals.get_deep_sections.assert_called_once_with(42, {"highlights"}, False)
    finally:
        service.close()


def test_fundamentals_repository_builds_section_read_model():
    service = DatabaseService("sqlite:///:memory:")
    tables = [
        Asset.__table__,
        FundamentalsHighlights.__table__,
        FinancialStatement.__table__,
        EarningsHistory.__table__,
        AnalystRatings.__table__,
    ]
    Base.metadata.create_all(service.engine, tables=tables)
    session = service.get_session()
    try:
        asset = Asset(ticker="AAPL", name="Apple")
        session.add(asset)
        session.flush()
        session.add_all(
            [
                FundamentalsHighlights(
                    asset_id=asset.id,
                    market_cap=Decimal("123.45"),
                    pe_ratio=Decimal("25.5"),
                ),
                FinancialStatement(
                    asset_id=asset.id,
                    statement_type="income",
                    period_type="annual",
                    period_end=date(2025, 12, 31),
                    revenue=Decimal("1000.50"),
                    currency="USD",
                ),
                EarningsHistory(
                    asset_id=asset.id,
                    period="2025Q4",
                    eps_actual=Decimal("2.5"),
                    eps_estimate=Decimal("2.3"),
                    surprise_pct=Decimal("8.7"),
                ),
                AnalystRatings(
                    asset_id=asset.id,
                    consensus="buy",
                    target_mean=Decimal("210.25"),
                ),
            ]
        )
        session.commit()

        result = service.fundamentals.get_deep_sections(asset.id, {"all"}, True)

        assert result["highlights"]["market_cap"] == 123.45
        statement = result["statements"]["income"]["annual"][0]
        assert statement["period_end"] == "2025-12-31"
        assert statement["revenue"] == 1000.5
        assert result["earnings_history"][0]["period"] == "2025Q4"
        assert result["analyst_ratings"]["consensus"] == "buy"
        assert result["analyst_ratings"]["target_mean"] == 210.25
    finally:
        session.close()
        service.close()


def test_identity_helpers_remain_backward_compatible():
    assert DatabaseService._normalize_ticker(" air.pa ") == "AIR.PA"
    assert DatabaseService._looks_like_isin("FR0000120271") is True
    assert DatabaseService.asset_profile_needs_enrichment({"display_name": None})
