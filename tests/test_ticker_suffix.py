"""``AIR.PA`` is Airbus in Paris, never the ``AIR`` of another exchange.

A ticker with a Yahoo suffix used to be looked up without it when the catalogue
had no such listing, and the first ``AIR`` found was taken: ``AIR.PA`` gave the
fundamentals and the valuation of AAR Corp, listed in New York.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.assets import AssetRepository
from database.price_series import PriceSeries, resolve_price_series
from database.ticker_suffix import PLACES, TickerLookup, ticker_lookups
from models import Asset, AssetListing, Base, FundamentalsHighlights
from news.news_service import NewsService
from valuation.dcf_service import DCFService

AAR = 1
AIRBUS = 2


class TestLookups:
    def test_the_bare_symbol_is_looked_up_on_the_place_of_the_suffix(self):
        assert ticker_lookups(" air.pa ") == [
            TickerLookup("AIR.PA"),
            TickerLookup("AIR", PLACES["PA"]),
        ]

    def test_a_ticker_without_suffix_is_looked_up_as_it_is(self):
        assert ticker_lookups("AAPL") == [TickerLookup("AAPL")]

    def test_a_share_class_is_not_an_exchange(self):
        assert ticker_lookups("BRK.B") == [TickerLookup("BRK.B")]


def _catalogue(*airbus_listings):
    """AAR Corp listed as ``AIR`` in New York, and Airbus with the given listings."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add_all(
        [
            Asset(id=AAR, ticker="AIR", name="AAR Corp", isin="US0003611052", is_active=True),
            Asset(id=AIRBUS, ticker="AIR", name="Airbus", isin="NL0000235190", is_active=True),
            AssetListing(
                id=10,
                asset_id=AAR,
                ticker="AIR",
                exchange="NYSE",
                currency="USD",
                is_primary=True,
                is_active=True,
            ),
            FundamentalsHighlights(asset_id=AAR),
        ]
    )
    for number, (exchange, currency) in enumerate(airbus_listings, start=20):
        session.add(
            AssetListing(
                id=number,
                asset_id=AIRBUS,
                ticker="AIR",
                exchange=exchange,
                currency=currency,
                is_primary=number == 20,
                is_active=True,
            )
        )
    session.commit()
    session.close()
    return engine, Session


def _resolve(Session, ticker):
    series = resolve_price_series(Session(), ticker)
    return series.asset_id if series else None


class TestPriceSeries:
    @pytest.mark.parametrize("exchange", ["XPAR", "EPA", "PA", "PAR", "xpar"])
    def test_the_listing_in_paris_is_taken_whatever_the_code_of_the_exchange(self, exchange):
        _, Session = _catalogue((exchange, "EUR"))
        assert _resolve(Session, "AIR.PA") == AIRBUS

    def test_a_listing_without_exchange_is_taken_in_the_currency_of_the_place(self):
        _, Session = _catalogue(("", "EUR"))
        assert _resolve(Session, "AIR.PA") == AIRBUS

    def test_without_a_listing_in_paris_another_company_is_not_taken(self):
        _, Session = _catalogue()
        assert _resolve(Session, "AIR.PA") is None

    def test_a_listing_on_another_european_exchange_is_not_paris(self):
        _, Session = _catalogue(("XETR", "EUR"))
        assert _resolve(Session, "AIR.PA") is None
        assert _resolve(Session, "AIR.DE") == AIRBUS

    def test_an_unknown_exchange_in_another_currency_is_not_paris(self):
        _, Session = _catalogue(("SOMEWHERE", "USD"))
        assert _resolve(Session, "AIR.PA") is None

    def test_a_ticker_without_suffix_is_not_restricted_to_a_place(self):
        _, Session = _catalogue()
        assert resolve_price_series(Session(), "AIR") == PriceSeries(asset_id=AAR, listing_id=10)


class TestAssetRepository:
    def _repository(self, *airbus_listings):
        engine, Session = _catalogue(*airbus_listings)
        return AssetRepository(engine, Session)

    def test_listings_of_another_company_are_not_found(self):
        repository = self._repository()
        assert repository.find_listings(ticker="AIR.PA") == []
        assert repository.get_asset_context(ticker="AIR.PA") is None

    def test_the_listing_in_paris_is_found(self):
        repository = self._repository(("XPAR", "EUR"))
        context = repository.get_asset_context(ticker="AIR.PA")
        assert context["details"]["isin"] == "NL0000235190"

    def test_an_instrument_known_by_its_ticker_only_must_be_listed_in_paris(self):
        engine, Session = _catalogue(("XPAR", "EUR"))
        session = Session()
        # Airbus loses its listings: only the instrument carries the ticker AIR.
        session.query(AssetListing).filter_by(asset_id=AIRBUS).update({"is_active": False})
        session.commit()
        repository = AssetRepository(engine, Session)

        asset = repository.get_asset_by_identity(ticker="AIR.PA")

        assert asset is not None and asset.id == AIRBUS


class TestValuation:
    def _asset(self, Session, ticker):
        asset = DCFService(MagicMock())._find_asset_by_ticker(Session(), ticker)
        return asset.id if asset else None

    def test_aar_corp_is_not_valued_for_airbus(self):
        _, Session = _catalogue()
        assert self._asset(Session, "AIR.PA") is None
        assert self._asset(Session, "AIR") == AAR

    def test_airbus_is_valued_from_its_listing_in_paris(self):
        _, Session = _catalogue(("XPAR", "EUR"))
        assert self._asset(Session, "AIR.PA") == AIRBUS


class TestNews:
    async def _asset(self, Session, ticker):
        session = Session()
        database = MagicMock()
        database.execute = AsyncMock(side_effect=session.execute)
        asset = await NewsService(db_session=database)._resolve_asset(ticker)
        return asset.id if asset else None

    async def test_news_of_another_company_are_not_given(self):
        _, Session = _catalogue()
        assert await self._asset(Session, "AIR.PA") is None

    async def test_news_of_the_company_in_paris(self):
        _, Session = _catalogue(("XPAR", "EUR"))
        assert await self._asset(Session, "AIR.PA") == AIRBUS
