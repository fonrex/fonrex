"""Factor exposure of a listing (``factors/exposure.py``, ``GET /factors/exposure/{ticker}``).

The regression is checked on returns built from known betas: it must find them
again. Prices, factors and exchange rates come from fakes; reading the prices of
a listing is tested on PostgreSQL (``tests/test_timescale_integration.py``).
"""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from factors.exposure import (
    ExposureError,
    FactorExposure,
    align,
    major_currency,
    period_closes,
    period_returns,
    region_of,
    regress,
    to_usd,
)
from factors.store import FactorLoad
from macro.fx_rates import FxLoad
from main import app

BETAS = {"MKT_RF": 1.2, "SMB": 0.5, "HML": -0.3, "MOM": 0.2}
ALPHA = 0.001  # per month


def _month_ends(first: date, count: int) -> list[date]:
    days, year, month = [], first.year, first.month
    for _ in range(count):
        days.append(date(year, month, monthrange(year, month)[1]))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return days


def _market(count: int = 72, noise: float = 0.002, seed: int = 7):
    """Monthly factors, and closes of a stock whose excess returns follow BETAS."""
    rng = np.random.default_rng(seed)
    days = _month_ends(date(2020, 1, 1), count + 1)
    factors, closes, price = {}, {days[0]: 100.0}, 100.0
    for day in days[1:]:
        values = {name: rng.normal(0.0, 0.04) for name in BETAS}
        values["RF"] = 0.002
        excess = ALPHA + sum(BETAS[name] * values[name] for name in BETAS) + rng.normal(0, noise)
        price *= 1 + values["RF"] + excess
        closes[day] = price
        factors[day] = {name: Decimal(str(round(value, 8))) for name, value in values.items()}
    return factors, closes


# ── Pure steps ───────────────────────────────────────────────────────────────


class TestCurrencies:
    @pytest.mark.parametrize(
        "currency, region",
        [
            ("USD", "us"),
            ("EUR", "europe"),
            ("GBP", "europe"),
            ("CHF", "europe"),
            ("JPY", "developed"),
            (None, "developed"),
        ],
    )
    def test_the_region_follows_the_currency(self, currency, region):
        assert region_of(currency) == region

    @pytest.mark.parametrize(
        "currency, major", [("GBp", "GBP"), ("GBX", "GBP"), ("eur", "EUR"), (None, None)]
    )
    def test_a_minor_unit_is_its_major_currency(self, currency, major):
        assert major_currency(currency) == major


class TestDollars:
    def test_a_close_takes_the_rate_of_its_day(self):
        closes = {date(2026, 10, 8): 100.0}
        rates = {date(2026, 10, 7): Decimal("1.10"), date(2026, 10, 8): Decimal("1.12")}

        assert to_usd(closes, rates) == ({date(2026, 10, 8): pytest.approx(112.0)}, 0)

    def test_an_ecb_holiday_takes_the_last_rate(self):
        closes = {date(2026, 12, 28): 100.0}
        rates = {date(2026, 12, 24): Decimal("1.05")}

        assert to_usd(closes, rates)[0] == {date(2026, 12, 28): pytest.approx(105.0)}

    def test_a_rate_older_than_a_week_is_not_used(self):
        closes = {date(2026, 10, 1): 100.0, date(2026, 10, 20): 101.0}
        rates = {date(2026, 9, 30): Decimal("1.1")}

        converted, missing = to_usd(closes, rates)

        assert list(converted) == [date(2026, 10, 1)] and missing == 1

    def test_a_close_before_the_first_rate(self):
        assert to_usd({date(1998, 12, 31): 10.0}, {date(1999, 1, 4): Decimal("1.17")}) == ({}, 1)


class TestReturns:
    def test_a_month_is_its_last_close(self):
        closes = {date(2026, 7, 30): 10.0, date(2026, 7, 31): 11.0, date(2026, 8, 28): 12.1}

        assert period_closes(closes, "monthly") == {
            date(2026, 7, 31): 11.0,
            date(2026, 8, 31): 12.1,
        }

    def test_monthly_returns_between_consecutive_months_only(self):
        closes = {
            date(2026, 5, 31): 10.0,
            date(2026, 6, 30): 11.0,
            date(2026, 8, 31): 12.0,
            date(2026, 9, 30): 12.6,
        }

        returns = period_returns(closes, "monthly")

        assert returns == {
            date(2026, 6, 30): pytest.approx(0.1),
            date(2026, 9, 30): pytest.approx(0.05),
        }

    def test_daily_returns_across_a_week_end(self):
        closes = {date(2026, 10, 9): 100.0, date(2026, 10, 12): 101.0}

        assert period_closes(closes, "daily") == closes
        assert period_returns(closes, "daily") == {date(2026, 10, 12): pytest.approx(0.01)}

    def test_a_price_of_zero_gives_no_return(self):
        assert period_returns({date(2026, 1, 1): 0.0, date(2026, 1, 2): 1.0}, "daily") == {}


class TestAlignment:
    FACTORS = {
        date(2026, 6, 30): {"MKT_RF": Decimal("0.01"), "SMB": Decimal("0"), "RF": Decimal("0.003")},
        date(2026, 7, 31): {"MKT_RF": Decimal("0.02"), "RF": Decimal("0.003")},
        date(2026, 8, 31): {
            "MKT_RF": Decimal("-0.01"),
            "SMB": Decimal("0.01"),
            "RF": Decimal("0.003"),
        },
    }
    RETURNS = {
        date(2026, 6, 30): 0.02,
        date(2026, 7, 31): 0.03,
        date(2026, 8, 31): -0.01,
        date(2026, 9, 30): 0.01,
    }

    def test_excess_returns_on_the_periods_with_every_factor(self):
        rows = align(self.RETURNS, self.FACTORS, ("MKT_RF", "SMB"), window=60)

        assert [row[0] for row in rows] == [date(2026, 6, 30), date(2026, 8, 31)]
        assert rows[0][1] == pytest.approx(0.017)
        assert rows[1][2] == [-0.01, 0.01]

    def test_the_window_keeps_the_last_periods_up_to_the_end(self):
        rows = align(self.RETURNS, self.FACTORS, ("MKT_RF",), window=1, end=date(2026, 7, 31))

        assert [row[0] for row in rows] == [date(2026, 7, 31)]


class TestRegression:
    def test_known_betas_are_found_again(self):
        factors, closes = _market()
        names = ("MKT_RF", "SMB", "HML", "MOM")

        result = regress(align(period_returns(closes, "monthly"), factors, names, 72), names)

        assert result.periods == 72
        for name in names:
            assert result.betas[name].value == pytest.approx(BETAS[name], abs=0.02)
            assert abs(result.betas[name].t_stat) > 10
        assert result.alpha.value == pytest.approx(ALPHA, abs=0.001)
        assert result.r_squared > 0.98
        assert result.adj_r_squared < result.r_squared
        assert result.residual_std == pytest.approx(0.002, rel=0.3)

    def test_a_factor_left_out_is_absorbed_by_the_others(self):
        factors, closes = _market()
        names = ("MKT_RF", "SMB", "HML")

        result = regress(align(period_returns(closes, "monthly"), factors, names, 72), names)

        assert result.r_squared < 0.98  # momentum is now in the residuals


# ── The service ──────────────────────────────────────────────────────────────


class FakeLibrary:
    def __init__(self, factors, stored=True):
        self.factors = factors
        self.stored = stored
        self.asked = []

    async def status(self, dataset, frequency):
        periods = len(self.factors) if self.stored else 0
        return FactorLoad(dataset, frequency, "fresh", periods=periods) if self.stored else None

    async def refresh(self, dataset, frequency, force=False):
        return FactorLoad(dataset, frequency, "failed", reason="ConnectError: down")

    async def series(self, dataset, frequency, start=None, end=None):
        self.asked.append(dataset)
        names = {
            "3": ("MKT_RF", "SMB", "HML", "RF"),
            "5": ("MKT_RF", "SMB", "HML", "RF"),
            "mom": ("MOM",),
        }
        keep = names[dataset.rsplit("_", 1)[1]]
        return {
            day: {k: v for k, v in values.items() if k in keep}
            for day, values in self.factors.items()
        }


class FakeFx:
    def __init__(self, usd_per_unit=Decimal("1.10"), stored=True):
        self.usd_per_unit = usd_per_unit
        self.stored = stored
        self.refreshed = []

    async def refresh(self, currency, force=False):
        self.refreshed.append(currency)
        if not self.stored:
            return FxLoad(currency, "failed", reason="HTTPStatusError: 503")
        return FxLoad(currency, "fresh", first_day=date(1999, 1, 4), last_day=date(2026, 10, 9))

    async def rates(self, base, quote, start=None, end=None):
        days = [date(2019, 12, 1) + timedelta(days=i) for i in range(7 * 365)]
        return {day: self.usd_per_unit for day in days}


def _service(closes, factors, currency="USD", library=None, fx=None, listing=True):
    service = FactorExposure(None, library or FakeLibrary(factors), fx or FakeFx())

    async def listing_of(ticker, currency_, exchange, isin):
        if not listing:
            return None, None, {}
        identity = {
            "ticker": ticker,
            "isin": "NL0000235190",
            "currency": currency,
            "exchange": "XPAR",
        }
        return SimpleNamespace(listing_id=1), identity, closes

    service._listing = listing_of
    return service


class TestMeasure:
    async def test_a_dollar_listing_is_regressed_on_the_us_factors(self):
        factors, closes = _market()
        service = _service(closes, factors)

        result = await service.measure("ACME")

        assert (result.region, result.datasets, result.converted_from) == ("us", ["us_3"], None)
        assert (result.start, result.end) == (
            date(2021, 2, 28),
            date(2026, 1, 31),
        )  # last 60 months
        assert result.regression.periods == 60
        assert service.fx.refreshed == []

    async def test_a_euro_listing_is_converted_in_dollars_and_regressed_on_europe(self):
        factors, closes = _market()
        service = _service(closes, factors, currency="EUR")

        result = await service.measure("AIR", model="carhart", window=72)

        assert (result.region, result.converted_from) == ("europe", "EUR")
        assert result.datasets == ["europe_3", "europe_mom"]
        assert service.fx.refreshed == ["USD"]
        # A constant exchange rate leaves the returns, and the betas, unchanged.
        assert result.regression.betas["MOM"].value == pytest.approx(BETAS["MOM"], abs=0.02)

    async def test_pence_are_converted_as_pounds(self):
        factors, closes = _market()
        service = _service(closes, factors, currency="GBp")

        result = await service.measure("ULVR", region="developed")

        assert (result.converted_from, result.region) == ("GBP", "developed")
        assert sorted(service.fx.refreshed) == ["GBP", "USD"]

    async def test_a_short_history_is_measured_with_a_warning(self):
        factors, closes = _market(count=30)

        result = await _service(closes, factors).measure("ACME")

        assert result.regression.periods == 30
        assert result.warnings == ["Only 30 monthly periods: the estimates are imprecise."]

    async def test_closes_without_exchange_rate_are_counted(self):
        factors, closes = _market()
        closes[date(1990, 1, 31)] = 50.0  # before the first rate of the fake

        result = await _service(closes, factors, currency="EUR").measure("AIR")

        assert result.warnings == [
            "1 closes without an ECB rate of EUR within a week were left out."
        ]

    async def test_a_listing_without_currency(self):
        factors, closes = _market()

        result = await _service(closes, factors, currency=None).measure("ACME", region="us")

        assert "taken as US dollars" in result.warnings[0]

    @pytest.mark.parametrize(
        "case, status, message",
        [
            ("no listing", 404, "No listing found"),
            ("no prices", 404, "POST /historical/ingest"),
            ("too short", 422, "at least 24 are needed"),
            ("no factors", 503, "No factor returns stored for us_3"),
            ("no exchange rate", 503, "No ECB exchange rate stored for USD"),
        ],
    )
    async def test_what_cannot_be_measured(self, case, status, message):
        factors, closes = _market(count=10 if case == "too short" else 72)
        kwargs = {}
        if case == "no listing":
            kwargs["listing"] = False
        if case == "no prices":
            closes = {date(2026, 1, 30): 1.0}
        if case == "no factors":
            kwargs["library"] = FakeLibrary(factors, stored=False)
        if case == "no exchange rate":
            kwargs.update(currency="EUR", fx=FakeFx(stored=False))

        with pytest.raises(ExposureError, match=message) as error:
            await _service(closes, factors, **kwargs).measure("ACME")

        assert error.value.status == status


# ── The route ────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    names = ("factor_exposure", "factor_library")
    originals = {name: getattr(app.state, name, None) for name in names}
    factors, closes = _market()
    with TestClient(app) as test_client:
        app.state.factor_exposure = _service(closes, factors, currency="EUR")
        yield test_client
    for name, value in originals.items():
        setattr(app.state, name, value)


class TestRoute:
    def test_betas_alpha_and_fit(self, client):
        answer = client.get("/factors/exposure/AIR?model=carhart&window=72").json()

        assert (answer["model"], answer["region"], answer["return_currency"]) == (
            "carhart",
            "europe",
            "USD",
        )
        assert (answer["converted_from"], answer["periods"]) == ("EUR", 72)
        assert answer["listing"]["isin"] == "NL0000235190"
        assert set(answer["betas"]) == {"MKT_RF", "SMB", "HML", "MOM"}
        assert answer["betas"]["MKT_RF"]["value"] == pytest.approx(1.2, abs=0.02)
        assert answer["alpha"]["annualized"] == pytest.approx(answer["alpha"]["value"] * 12)
        assert answer["residual_volatility"] == pytest.approx(0.002 * 12**0.5, rel=0.3)
        assert "Kenneth R. French" in answer["source"]

    def test_what_cannot_be_measured_answers_its_status(self, client):
        response = client.get("/factors/exposure/AIR?end=2020-06-30")

        assert response.status_code == 422
        assert "5 monthly periods" in response.json()["detail"]

    @pytest.mark.parametrize("query", ["model=ff4", "window=10", "region=asia"])
    def test_parameters_out_of_range(self, client, query):
        assert client.get(f"/factors/exposure/AIR?{query}").status_code == 422

    def test_without_database(self, client):
        app.state.factor_exposure = None

        assert client.get("/factors/exposure/AIR").status_code == 503
