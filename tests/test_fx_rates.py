"""ECB reference rates of the euro (``macro/fx_rates.py``).

Storing and refreshing them is tested on PostgreSQL (``tests/test_timescale_integration.py``);
these tests cover the answers of the ECB, the conversions and the script. No test
reaches the network.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from macro.fx_rates import (
    DEFAULT_CURRENCIES,
    EcbExchangeRates,
    FxLoad,
    FxRatesError,
    download_rates,
    parse_fx_csv,
    series_url,
)
from scripts import load_fx_rates

# The ECB answer with detail=dataonly: the dimensions of the key, the day, the value.
DATA_ONLY = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\r\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-07,1.0861\r\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-08,1.0850\r\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-09,\r\n"
)


class TestAnswers:
    def test_rates_are_read_by_currency_and_day(self):
        rates = parse_fx_csv(DATA_ONLY)

        assert rates == {
            "USD": {date(2026, 10, 7): Decimal("1.0861"), date(2026, 10, 8): Decimal("1.0850")}
        }

    def test_a_day_without_value_is_left_out(self):
        assert date(2026, 10, 9) not in parse_fx_csv(DATA_ONLY)["USD"]

    def test_the_currency_is_read_from_the_key_when_its_column_is_missing(self):
        text = "KEY,TIME_PERIOD,OBS_VALUE\nEXR.D.JPY.EUR.SP00.A,2026-10-08,161.27\n"

        assert parse_fx_csv(text) == {"JPY": {date(2026, 10, 8): Decimal("161.27")}}

    def test_several_currencies_in_one_answer(self):
        text = DATA_ONLY + "EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2026-10-08,0.8412\r\n"

        assert set(parse_fx_csv(text)) == {"USD", "GBP"}

    def test_a_zero_or_negative_rate_is_left_out(self):
        text = "KEY,CURRENCY,TIME_PERIOD,OBS_VALUE\nk,USD,2026-10-08,0\nk,USD,2026-10-09,-1\n"

        assert parse_fx_csv(text) == {}

    @pytest.mark.parametrize(
        "text, message",
        [
            ("<html>error</html>", "no TIME_PERIOD"),
            ("KEY,CURRENCY,TIME_PERIOD,OBS_VALUE\nk,USD,2026-13-01,1.08\n", "cannot be read"),
            ("KEY,CURRENCY,TIME_PERIOD,OBS_VALUE\nk,USD,2026-10-01,n/a\n", "cannot be read"),
        ],
        ids=["not a csv", "bad day", "bad value"],
    )
    def test_an_answer_that_cannot_be_read_is_refused(self, text, message):
        with pytest.raises(FxRatesError, match=message):
            parse_fx_csv(text)


class TestAddresses:
    def test_the_series_of_a_currency(self, monkeypatch):
        monkeypatch.delenv("ECB_API_URL", raising=False)

        assert series_url("usd") == (
            "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
        )

    def test_the_mirror_of_the_ecb_is_used(self, monkeypatch):
        monkeypatch.setenv("ECB_API_URL", "https://mirror.example/data/")

        assert series_url("GBP").startswith("https://mirror.example/data/EXR/")

    @pytest.mark.parametrize("code", ["US", "US D", "U$D", ""])
    def test_a_code_that_is_not_a_currency_is_refused(self, code):
        with pytest.raises(ValueError, match="not a currency code"):
            series_url(code)


def _ecb(text: str, status: int = 200):
    request = httpx.Request("GET", "https://ecb")
    return AsyncMock(return_value=httpx.Response(status, text=text, request=request))


class TestDownload:
    async def test_only_the_days_from_the_start_are_asked_without_attributes(self):
        get = _ecb(DATA_ONLY)
        with patch("httpx.AsyncClient.get", get):
            rates = await download_rates("USD", date(2026, 10, 1))

        assert rates[date(2026, 10, 8)] == Decimal("1.0850")
        params = get.await_args.kwargs["params"]
        assert params == {"startPeriod": "2026-10-01", "format": "csvdata", "detail": "dataonly"}

    async def test_nothing_published_since_the_start(self):
        with patch("httpx.AsyncClient.get", _ecb("")):
            assert await download_rates("USD", date(2026, 10, 10)) == {}

    async def test_an_http_error_is_raised(self):
        with patch("httpx.AsyncClient.get", _ecb("No results found.", status=404)):
            with pytest.raises(httpx.HTTPStatusError):
                await download_rates("XXX", date(2026, 10, 1))


class TestConversions:
    PER_EUR = {
        "USD": {date(2026, 10, 7): Decimal("1.08"), date(2026, 10, 8): Decimal("1.10")},
        "GBP": {date(2026, 10, 8): Decimal("0.88")},
    }

    @pytest.fixture
    def rates(self, monkeypatch):
        service = EcbExchangeRates(session_factory=None)

        async def per_euro(currency, start=None, end=None):
            return self.PER_EUR[currency]

        monkeypatch.setattr(service, "per_euro", per_euro)
        return service

    async def test_dollars_per_euro(self, rates):
        assert await rates.rates("EUR", "USD") == self.PER_EUR["USD"]

    async def test_euros_per_dollar(self, rates):
        assert (await rates.rates("USD", "EUR"))[date(2026, 10, 8)] == Decimal("0.90909091")

    async def test_dollars_per_pound_go_through_the_euro_on_the_days_both_are_known(self, rates):
        # (1.10 USD per EUR) / (0.88 GBP per EUR) = 1.25 USD per GBP
        assert await rates.rates("GBP", "USD") == {date(2026, 10, 8): Decimal("1.25000000")}

    async def test_the_same_currency_is_refused(self, rates):
        with pytest.raises(ValueError, match="same"):
            await rates.rates("usd", "USD")

    async def test_the_euro_is_never_asked(self):
        rates = EcbExchangeRates(session_factory=None, downloader=AsyncMock())

        assert await rates.refresh("eur") == FxLoad("EUR", "fresh")
        rates.downloader.assert_not_awaited()

    def test_the_refresh_delay_follows_the_environment(self, monkeypatch):
        monkeypatch.setenv("FX_RATES_REFRESH_HOURS", "48")

        assert EcbExchangeRates(session_factory=None).refresh_after.total_seconds() == 48 * 3600


class TestScript:
    def test_the_default_currencies(self):
        arguments = load_fx_rates.parse_arguments([])

        assert (arguments.currency, arguments.force) == (list(DEFAULT_CURRENCIES), False)

    def test_a_line_per_currency(self):
        fetched = FxLoad(
            "USD", "fetched", first_day=date(1999, 1, 4), last_day=date(2026, 10, 9), added=7140
        )
        failed = FxLoad("XXX", "failed", reason="HTTPStatusError: 404")

        assert (
            load_fx_rates.describe(fetched)
            == "USD  fetched  1999-01-04 → 2026-10-09  (7140 days read)"
        )
        assert (
            load_fx_rates.describe(failed) == "XXX  failed   nothing stored  (HTTPStatusError: 404)"
        )

    async def test_without_database_the_script_stops(self, monkeypatch, capsys):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("ASYNC_DATABASE_URL", raising=False)

        assert await load_fx_rates.main([]) == 2
        assert "DATABASE_URL" in capsys.readouterr().err
