"""The enrichment started by the import asks Yahoo with a verified symbol.

``import_assets.py --enrich-only`` and ``scripts/seed_database.py --enrich``
fill the fundamentals of the instruments of the catalogue. A ticker of the
catalogue is not a Yahoo symbol: ``SPFF`` is a bond UCITS ETF in EUR here and a
US fund on Yahoo. Asked as typed, the fundamentals of the US fund were stored
under the European one.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

import import_assets
from historical.yahoo_symbols import SymbolResolution
from models import Asset, AssetListing, AssetMapping, Base


@pytest.fixture
def catalogue():
    """A database knowing SPFF (two listings) and an instrument without listing."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine))
    session = Session()
    session.add(Asset(id=1, ticker="SPFF", name="Global Aggregate Bond", isin="IE000AQ7A2X6"))
    session.add(
        AssetListing(id=10, asset_id=1, ticker="SPFF", exchange="XLON", currency="GBP", is_primary=False)
    )
    session.add(
        AssetListing(id=11, asset_id=1, ticker="SPFF", exchange="XETR", currency="EUR", is_primary=True)
    )
    session.add(Asset(id=2, ticker="OLD", name="Instrument without listing"))
    session.commit()
    session.close()
    database = MagicMock()
    database.get_session.side_effect = lambda: Session()
    yield database
    Session.remove()
    engine.dispose()


def _resolver(symbol, reason=None):
    return SimpleNamespace(resolve=AsyncMock(return_value=SymbolResolution(symbol, reason)))


@pytest.fixture
def enricher():
    instance = SimpleNamespace(enrich=AsyncMock(return_value={"errors": []}))
    with patch(
        "financials.enrichment.yfinance_enricher.YFinanceEnricher", return_value=instance
    ):
        yield instance


@pytest.mark.asyncio
async def test_yahoo_is_asked_with_the_symbol_verified_for_the_primary_listing(catalogue, enricher):
    symbols = _resolver("SPFF.DE")

    await import_assets.enrich_after_import(1, "SPFF", catalogue, symbols=symbols)

    symbols.resolve.assert_awaited_once_with(11)  # the primary listing, in EUR
    enricher.enrich.assert_awaited_once_with(1, "SPFF.DE")


@pytest.mark.asyncio
async def test_instrument_without_verified_symbol_is_not_enriched(catalogue, enricher, caplog):
    symbols = _resolver(None, "No verified Yahoo symbol for SPFF in EUR")

    with caplog.at_level("INFO"):
        await import_assets.enrich_after_import(1, "SPFF", catalogue, symbols=symbols)

    enricher.enrich.assert_not_awaited()
    assert "No verified Yahoo symbol for SPFF in EUR" in caplog.text


@pytest.mark.asyncio
async def test_instrument_without_listing_is_not_enriched(catalogue, enricher):
    symbols = _resolver("OLD")

    await import_assets.enrich_after_import(2, "OLD", catalogue, symbols=symbols)

    symbols.resolve.assert_not_awaited()
    enricher.enrich.assert_not_awaited()


def _verified_mapping(catalogue, symbol):
    session = catalogue.get_session()
    session.add(
        AssetMapping(
            asset_id=1,
            asset_listing_id=11,
            provider_name="YahooFinance",
            provider_ticker=symbol,
            source="isin_search",
            is_active=True,
            last_verified_at=datetime.now(UTC),
        )
    )
    session.commit()


@pytest.mark.asyncio
async def test_default_resolver_is_the_one_of_the_price_ingestion(catalogue, enricher):
    """Without an injected resolver, the symbol stored for the listing is read."""
    _verified_mapping(catalogue, "SPFF.DE")

    await import_assets.enrich_after_import(1, "SPFF", catalogue)

    enricher.enrich.assert_awaited_once_with(1, "SPFF.DE")


@pytest.mark.asyncio
async def test_a_failure_never_stops_the_batch(catalogue, enricher):
    _verified_mapping(catalogue, "SPFF.DE")
    enricher.enrich.side_effect = RuntimeError("Yahoo is down")

    await import_assets.enrich_batch(catalogue, [(1, "SPFF")], batch_size=1)  # does not raise

    enricher.enrich.assert_awaited_once_with(1, "SPFF.DE")
