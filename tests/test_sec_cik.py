"""The CIK of an instrument (``financials/sec_cik.py``): verified, never guessed.

The table of the SEC is the reduced real one of ``tests/fixtures/sec/``. The
stored mapping is tested on PostgreSQL (``tests/test_timescale_integration.py``).
No test reaches the SEC.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from financials.providers.sec_edgar import SECEdgarProvider
from financials.sec_cik import (
    FOUND_BY_TICKER,
    NOT_FOUND,
    SET_BY_HAND,
    CikResolution,
    Instrument,
    InstrumentListing,
    SecCikResolver,
    SecTableError,
    is_us_listing,
    parse_ticker_table,
    same_company,
    sec_symbol,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
TABLE = json.loads((FIXTURES / "company_tickers_exchange.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)


def listing(ticker, exchange="", currency="USD"):
    return InstrumentListing(ticker, exchange, currency)


class FakeStore:
    def __init__(self, instrument):
        self.instrument = instrument
        self.remembered: list[CikResolution] = []

    async def load(self, asset_id):
        return self.instrument if self.instrument and self.instrument.asset_id == asset_id else None

    async def remember(self, asset_id, resolution, now):
        self.remembered.append(resolution)


def resolver(instrument, table=TABLE, **options):
    source = AsyncMock(return_value=table)
    store = FakeStore(instrument)
    return SecCikResolver(store, source, now=lambda: NOW, **options), store, source


def instrument(name, *listings, quote_type="EQUITY", **stored):
    return Instrument(1, name, quote_type, tuple(listings), **stored)


class TestTable:
    def test_rows_by_ticker(self):
        table = parse_ticker_table(TABLE)
        assert table["AAPL"][0].cik == 320193
        assert table["AAPL"][0].exchange == "Nasdaq"
        assert [entry.ticker for entry in table["GOOG"]] == ["GOOG"]
        assert table["AIR"][0].name == "AAR CORP"

    @pytest.mark.parametrize(
        "data",
        [None, [], {"fields": ["cik"], "data": []}, {"data": "rows"}],
    )
    def test_a_table_that_cannot_be_read_is_refused(self, data):
        with pytest.raises(SecTableError):
            parse_ticker_table(data)

    def test_a_broken_row_is_left_out(self):
        table = parse_ticker_table(
            {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [["x", "Bad", "BAD", "NYSE"], [1, "Ok", "OK", None]],
            }
        )
        assert list(table) == ["OK"]


class TestUsListings:
    @pytest.mark.parametrize(
        ("ticker", "symbol"),
        [
            ("AAPL", "AAPL"),
            (" msft ", "MSFT"),
            ("MSFT.US", "MSFT"),
            ("BRK.B", "BRK-B"),
            ("BRK-B", "BRK-B"),
            ("AIR.PA", None),
            ("SAP.DE", None),
            ("VOD.L", None),
            ("ABC.XYZ", None),
            ("", None),
            (".B", None),
        ],
    )
    def test_the_ticker_as_the_sec_writes_it(self, ticker, symbol):
        assert sec_symbol(ticker) == symbol

    @pytest.mark.parametrize(
        ("ticker", "exchange", "currency", "us"),
        [
            ("AAPL", "", "USD", True),
            ("AAPL", "XNAS", "usd", True),
            ("SAP", "NYSE", "USD", True),
            ("AIR", "XPAR", "EUR", False),
            ("AIR.PA", "", "EUR", False),
            ("SAP", "XETR", "USD", False),
            ("SPFF", "", "EUR", False),
            ("AAPL", "", None, False),
        ],
    )
    def test_only_a_us_listing_is_looked_up(self, ticker, exchange, currency, us):
        assert is_us_listing(ticker, exchange, currency) is us


class TestNames:
    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [
            ("Apple Inc.", "Apple Inc."),
            ("Alphabet Inc Class A", "Alphabet Inc."),
            ("JPMorgan Chase & Co", "JPMORGAN CHASE & CO"),
            ("Berkshire Hathaway Inc Class B", "BERKSHIRE HATHAWAY INC"),
            ("SAP SE ADR", "SAP SE"),
            ("Novartis AG Sponsored ADR", "NOVARTIS AG"),
            ("ADR on SAP SE", "SAP SE"),
            ("Sponsored ADR of Novartis AG", "NOVARTIS AG"),
            ("ON Semiconductor Corp", "ON SEMICONDUCTOR CORP"),
            ("Bank of America Corp", "BANK OF AMERICA CORP"),
            ("Nestlé S.A.", "Nestle SA"),
            ("The Coca-Cola Company", "COCA COLA CO"),
        ],
    )
    def test_the_same_company(self, ours, theirs):
        assert same_company(ours, theirs)

    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [
            ("Airbus SE", "AAR CORP"),
            ("Apple Hospitality REIT", "Apple Inc."),
            ("Inc.", "Apple Inc."),
            (None, "Apple Inc."),
            ("Apple Inc.", ""),
        ],
    )
    def test_another_company(self, ours, theirs):
        assert not same_company(ours, theirs)


class TestResolution:
    async def test_a_us_share_is_found_and_remembered(self):
        cik_resolver, store, _ = resolver(instrument("Apple Inc.", listing("AAPL")))

        resolution = await cik_resolver.resolve(1)

        assert (resolution.cik, resolution.origin, resolution.company_name) == (
            "0000320193",
            FOUND_BY_TICKER,
            "Apple Inc.",
        )
        assert store.remembered == [resolution]

    async def test_airbus_is_never_aar_corp(self):
        airbus = instrument(
            "Airbus SE", listing("AIR.PA", "XPAR", "EUR"), listing("AIR", "", "EUR")
        )
        cik_resolver, store, source = resolver(airbus)

        resolution = await cik_resolver.resolve(1)

        assert resolution.cik is None
        assert "No US listing" in resolution.reason
        source.assert_not_awaited()  # the SEC is not even asked
        assert store.remembered == []

    async def test_a_us_ticker_of_another_company_is_refused(self):
        # A listing quoted in USD under AIR, named like Airbus: the SEC's AIR is AAR Corp.
        cik_resolver, store, _ = resolver(instrument("Airbus SE", listing("AIR", "NYSE")))

        resolution = await cik_resolver.resolve(1)

        assert resolution.cik is None
        assert resolution.origin == NOT_FOUND
        assert "AAR CORP" in resolution.reason
        assert store.remembered == [resolution]

    async def test_a_european_company_through_its_us_listing(self):
        sap = instrument("SAP SE", listing("SAP.DE", "XETR", "EUR"), listing("SAP", "NYSE", "USD"))
        cik_resolver, _, _ = resolver(sap)

        assert (await cik_resolver.resolve(1)).cik == "0001000184"

    async def test_a_european_company_without_us_listing_is_not_looked_up(self):
        sap = instrument("SAP SE", listing("SAP.DE", "XETR", "EUR"), listing("SAP", "XETR", "EUR"))
        cik_resolver, _, source = resolver(sap)

        assert (await cik_resolver.resolve(1)).cik is None
        source.assert_not_awaited()

    async def test_two_classes_of_shares_are_one_company(self):
        alphabet = instrument("Alphabet Inc Class A", listing("GOOGL"), listing("GOOG"))
        cik_resolver, _, _ = resolver(alphabet)

        assert (await cik_resolver.resolve(1)).cik == "0001652044"

    async def test_a_class_of_shares_written_with_a_dot(self):
        cik_resolver, _, _ = resolver(
            instrument("Berkshire Hathaway Inc", listing("BRK.B", "NYSE"))
        )
        assert (await cik_resolver.resolve(1)).cik == "0001067983"

    async def test_several_companies_for_one_instrument_give_none(self):
        table = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[1, "Acme Corp", "ACM", "NYSE"], [2, "Acme Corporation", "ACME", "Nasdaq"]],
        }
        acme = instrument("Acme Corp", listing("ACM"), listing("ACME"))
        cik_resolver, _, _ = resolver(acme, table=table)

        resolution = await cik_resolver.resolve(1)

        assert resolution.cik is None
        assert "Several companies" in resolution.reason

    async def test_a_ticker_off_the_us_exchanges_is_refused(self):
        table = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[7, "Acme Corp", "ACM", None]],
        }
        cik_resolver, _, _ = resolver(instrument("Acme Corp", listing("ACM")), table=table)

        resolution = await cik_resolver.resolve(1)
        assert resolution.cik is None
        assert "not on a US exchange" in resolution.reason

    async def test_an_unknown_ticker(self):
        cik_resolver, _, _ = resolver(instrument("Nobody Inc", listing("ZZZZ")))
        resolution = await cik_resolver.resolve(1)
        assert resolution.cik is None
        assert resolution.reason == "No SEC company named like 'Nobody Inc' for ZZZZ"

    async def test_an_instrument_without_name_cannot_be_confirmed(self):
        cik_resolver, _, _ = resolver(instrument(None, listing("AAPL")))
        assert "no name" in (await cik_resolver.resolve(1)).reason

    async def test_a_fund_files_no_statements(self):
        cik_resolver, _, source = resolver(
            instrument("SPDR S&P 500", listing("SPY"), quote_type="ETF")
        )
        assert "files no financial statements" in (await cik_resolver.resolve(1)).reason
        source.assert_not_awaited()

    async def test_an_unknown_instrument(self):
        cik_resolver, _, _ = resolver(None)
        assert (await cik_resolver.resolve(1)).reason == "Instrument 1 does not exist"

    async def test_an_unreadable_table_concludes_nothing(self):
        cik_resolver, store, _ = resolver(instrument("Apple Inc.", listing("AAPL")), table=None)

        resolution = await cik_resolver.resolve(1)

        assert resolution.cik is None
        assert "could not be read" in resolution.reason
        assert store.remembered == []


class TestWhatIsStored:
    async def test_a_verified_cik_is_reused_without_asking(self):
        stored = instrument(
            "Apple Inc.",
            listing("AAPL"),
            stored_cik="0000320193",
            stored_source=FOUND_BY_TICKER,
            stored_active=True,
            stored_verified=True,
        )
        cik_resolver, store, source = resolver(stored)

        assert (await cik_resolver.resolve(1)).cik == "0000320193"
        source.assert_not_awaited()
        assert store.remembered == []

    async def test_a_cik_set_by_hand_is_trusted(self):
        stored = instrument(
            "SAP SE",
            listing("SAP.DE", "XETR", "EUR"),
            stored_cik="0001000184",
            stored_source=SET_BY_HAND,
            stored_active=True,
        )
        cik_resolver, _, source = resolver(stored)

        assert (await cik_resolver.resolve(1)).cik == "0001000184"
        source.assert_not_awaited()

    async def test_an_unverified_cik_is_not_trusted(self):
        stored = instrument(
            "Apple Inc.",
            listing("AAPL"),
            stored_cik="0000000001",
            stored_source="csv_import",
            stored_active=True,
        )
        cik_resolver, _, source = resolver(stored)

        assert (await cik_resolver.resolve(1)).cik == "0000320193"
        source.assert_awaited_once()

    async def test_a_recent_failure_is_not_asked_again(self):
        stored = instrument(
            "Nobody Inc",
            listing("ZZZZ"),
            stored_source=NOT_FOUND,
            stored_at=(NOW - timedelta(days=2)).replace(tzinfo=None),
        )
        cik_resolver, _, source = resolver(stored)

        resolution = await cik_resolver.resolve(1)

        assert resolution.origin == NOT_FOUND
        assert "checked on 2026-10-14" in resolution.reason
        source.assert_not_awaited()

    async def test_an_old_failure_or_a_refresh_asks_again(self):
        old = instrument(
            "Apple Inc.",
            listing("AAPL"),
            stored_source=NOT_FOUND,
            stored_at=NOW - timedelta(days=8),
        )
        cik_resolver, _, _ = resolver(old)
        assert (await cik_resolver.resolve(1)).cik == "0000320193"

        recent = instrument(
            "Apple Inc.",
            listing("AAPL"),
            stored_source=NOT_FOUND,
            stored_at=NOW - timedelta(hours=1),
        )
        cik_resolver, _, _ = resolver(recent)
        assert (await cik_resolver.resolve(1, refresh=True)).cik == "0000320193"

    async def test_the_table_is_read_once_a_day(self):
        moments = [NOW]
        source = AsyncMock(return_value=TABLE)
        cik_resolver = SecCikResolver(
            FakeStore(instrument("Apple Inc.", listing("AAPL"))), source, now=lambda: moments[0]
        )

        await cik_resolver.resolve(1, refresh=True)
        await cik_resolver.resolve(1, refresh=False)  # stored: no table needed
        moments[0] = NOW + timedelta(hours=2)
        cik_resolver._store.instrument = instrument("Microsoft", listing("MSFT"))
        await cik_resolver.resolve(1)
        assert source.await_count == 1
        moments[0] = NOW + timedelta(hours=25)
        await cik_resolver.resolve(1)
        assert source.await_count == 2


class TestTheTableOfTheSec:
    async def test_read_with_the_user_agent_of_the_instance(self, fake_network, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_EMAIL", "pierre@example.org")
        fake_network.get(
            "/files/company_tickers_exchange.json",
            httpx.Response(200, text=json.dumps(TABLE)),
        )

        table = await SECEdgarProvider().ticker_table()

        assert table["data"][0] == [320193, "Apple Inc.", "AAPL", "Nasdaq"]
        request = fake_network.requests[0]
        assert str(request.url) == "https://www.sec.gov/files/company_tickers_exchange.json"
        assert request.headers["User-Agent"] == "Fonrex pierre@example.org"

    async def test_a_refusal_gives_no_table(self, fake_network):
        fake_network.get(
            "/files/company_tickers_exchange.json",
            httpx.Response(403, text="Undeclared Automated Tool"),
        )
        assert await SECEdgarProvider().ticker_table() is None
