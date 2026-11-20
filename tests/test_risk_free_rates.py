"""The risk-free rate of each day in the currency of a listing (``macro/risk_free_rates.py``).

The sources are replaced by fakes holding stored rates; reading them from the
tables is tested on PostgreSQL (``tests/test_timescale_integration.py``).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from macro.risk_free_rates import (
    MAX_DAILY_GAP,
    MAX_MONTHS_CARRIED,
    OECD_AREA_OF_CURRENCY,
    TREASURY_SOURCE,
    RiskFreeRateRepository,
    RiskFreeRates,
    align_daily,
    align_monthly,
    major_currency,
)
from macro.short_rates import RatesLoad
from macro.treasury_curves import CurveLoad

D = date


class FakeTreasury:
    def __init__(self, series, reason=None):
        self.series, self.reason, self.ensured, self.asked = series, reason, [], []

    async def ensure(self, curve, start, end=None):
        self.ensured.append((curve, start, end))
        return CurveLoad(curve, status="failed" if self.reason else "fresh", reason=self.reason)

    async def history(self, curve, tenors, start=None, end=None):
        self.asked.append((curve, list(tenors), start, end))
        return {"3M": [(day, rate) for day, rate in self.series if start <= day <= end]}


class FakeShortRates:
    def __init__(self, series, reason=None):
        self.stored, self.reason, self.refreshed, self.asked = series, reason, 0, []

    async def refresh(self):
        self.refreshed += 1
        return RatesLoad("failed" if self.reason else "fresh", reason=self.reason)

    async def series(self, area, start=None, end=None):
        self.asked.append((area, start, end))
        return [(month, rate) for month, rate in self.stored.get(area, []) if start <= month <= end]


def repository(treasury=(), oecd=None, treasury_reason=None, oecd_reason=None):
    fake_treasury = FakeTreasury(list(treasury), treasury_reason)
    fake_oecd = FakeShortRates(oecd or {}, oecd_reason)
    return RiskFreeRateRepository(fake_treasury, fake_oecd), fake_treasury, fake_oecd


class TestCurrencies:
    @pytest.mark.parametrize(
        ("currency", "major"),
        [
            ("EUR", "EUR"),
            ("eur", "EUR"),
            ("GBp", "GBP"),
            ("GBX", "GBP"),
            ("ZAc", "ZAR"),
            ("ILA", "ILS"),
            ("", None),
            (None, None),
        ],
    )
    def test_a_minor_unit_is_read_as_its_major_one(self, currency, major):
        assert major_currency(currency) == major

    def test_the_sources(self):
        assert RiskFreeRateRepository.source_of("USD") == TREASURY_SOURCE
        assert RiskFreeRateRepository.source_of("EUR") == "OECD, 3-month interbank rate (EA20)"
        assert RiskFreeRateRepository.source_of("GBp") == "OECD, 3-month interbank rate (GBR)"
        assert RiskFreeRateRepository.source_of("HKD") is None
        assert RiskFreeRateRepository.source_of(None) is None

    def test_the_european_currencies_of_the_factors_all_have_an_area(self):
        from factors.exposure import EUROPEAN_CURRENCIES

        assert EUROPEAN_CURRENCIES <= set(OECD_AREA_OF_CURRENCY)
        # The areas are those of the answer of the OECD (tests/fixtures/oecd/README.md).
        assert OECD_AREA_OF_CURRENCY["EUR"] == "EA20"
        assert all(len(area) == 3 for code, area in OECD_AREA_OF_CURRENCY.items() if code != "EUR")


class TestAlignDaily:
    SERIES = [(D(2026, 11, 13), Decimal("0.0380")), (D(2026, 11, 16), Decimal("0.0378"))]

    def test_a_day_takes_its_rate_or_the_last_one_before(self):
        aligned, missing = align_daily(
            [D(2026, 11, 16), D(2026, 11, 13), D(2026, 11, 15)], self.SERIES
        )
        assert missing is None
        assert aligned == {
            D(2026, 11, 13): (D(2026, 11, 13), 0.038),
            # A Sunday takes the Friday.
            D(2026, 11, 15): (D(2026, 11, 13), 0.038),
            D(2026, 11, 16): (D(2026, 11, 16), 0.0378),
        }

    def test_a_rate_is_carried_a_week_at_most(self):
        assert MAX_DAILY_GAP == 7
        last = D(2026, 11, 16)
        assert align_daily([last + timedelta(days=7)], self.SERIES)[1] is None
        assert align_daily([last + timedelta(days=8)], self.SERIES)[1] == last + timedelta(days=8)

    def test_a_day_before_the_first_rate(self):
        aligned, missing = align_daily([D(2026, 11, 12), D(2026, 11, 13)], self.SERIES)
        assert (aligned, missing) == ({}, D(2026, 11, 12))


class TestAlignMonthly:
    SERIES = [(D(2026, 9, 1), Decimal("0.0264")), (D(2026, 10, 1), Decimal("0.0263"))]

    def test_a_day_takes_the_rate_of_its_month(self):
        aligned, missing = align_monthly(
            [D(2026, 9, 30), D(2026, 10, 1), D(2026, 10, 31)], self.SERIES
        )
        assert missing is None
        assert aligned == {
            D(2026, 9, 30): (D(2026, 9, 1), 0.0264),
            D(2026, 10, 1): (D(2026, 10, 1), 0.0263),
            D(2026, 10, 31): (D(2026, 10, 1), 0.0263),
        }

    def test_a_month_not_published_yet_takes_the_last_one_within_three_months(self):
        assert MAX_MONTHS_CARRIED == 3
        aligned, missing = align_monthly([D(2026, 11, 20), D(2027, 1, 31)], self.SERIES)
        assert missing is None
        assert aligned[D(2027, 1, 31)] == (D(2026, 10, 1), 0.0263)
        assert align_monthly([D(2027, 2, 1)], self.SERIES)[1] == D(2027, 2, 1)

    def test_a_day_before_the_first_month(self):
        assert align_monthly([D(2026, 8, 31)], self.SERIES) == ({}, D(2026, 8, 31))


def days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(count)]


class TestRepository:
    async def test_dollar_rates_of_the_treasury(self):
        series = [(D(2026, 11, 13), Decimal("0.0380")), (D(2026, 11, 16), Decimal("0.0378"))]
        repo, treasury, oecd = repository(treasury=series)

        rates = await repo.rates("USD", days(D(2026, 11, 13), 4))

        assert rates == RiskFreeRates(
            "USD",
            TREASURY_SOURCE,
            {
                D(2026, 11, 13): 0.038,
                D(2026, 11, 14): 0.038,
                D(2026, 11, 15): 0.038,
                D(2026, 11, 16): 0.0378,
            },
            first_observation=D(2026, 11, 13),
            last_observation=D(2026, 11, 16),
        )
        # The curve is read from a week before the first day, so the first day has a rate.
        assert treasury.ensured == [("nominal", D(2026, 11, 6), D(2026, 11, 16))]
        assert treasury.asked == [("nominal", ["3M"], D(2026, 11, 6), D(2026, 11, 16))]
        assert oecd.refreshed == 0

    async def test_euro_rates_of_the_oecd(self):
        series = {"EA20": [(D(2026, 9, 1), Decimal("0.0264")), (D(2026, 10, 1), Decimal("0.0263"))]}
        repo, treasury, oecd = repository(oecd=series)

        rates = await repo.rates("EUR", [D(2026, 9, 30), D(2026, 11, 20)])

        assert (rates.currency, rates.source, rates.reason) == (
            "EUR",
            "OECD, 3-month interbank rate (EA20)",
            None,
        )
        assert rates.rates == {D(2026, 9, 30): 0.0264, D(2026, 11, 20): 0.0263}
        assert rates.mean == pytest.approx(0.02635)
        assert (rates.first_observation, rates.last_observation) == (D(2026, 9, 1), D(2026, 10, 1))
        assert oecd.refreshed == 1
        assert oecd.asked == [("EA20", D(2026, 6, 1), D(2026, 11, 1))]
        assert treasury.ensured == []

    async def test_pence_are_pounds(self):
        repo, _, oecd = repository(oecd={"GBR": [(D(2026, 2, 1), Decimal("0.0362"))]})
        rates = await repo.rates("GBp", [D(2026, 2, 27)])
        assert (rates.currency, rates.rates) == ("GBP", {D(2026, 2, 27): 0.0362})
        assert oecd.asked[0][0] == "GBR"

    async def test_a_series_the_oecd_stopped(self):
        # The United Kingdom stops in February 2026: no rate after May.
        repo, _, _ = repository(
            oecd={"GBR": [(D(2026, 1, 1), Decimal("0.0371")), (D(2026, 2, 1), Decimal("0.0362"))]}
        )

        rates = await repo.rates("GBP", [D(2026, 5, 29), D(2026, 6, 1)])

        assert rates.rates is None
        assert rates.reason == (
            "no OECD, 3-month interbank rate (GBR) for 2026-06-01 (the rates read run from "
            "2026-02-01 to 2026-02-01)"
        )
        assert rates.mean is None

    async def test_a_failed_reading_is_given_with_the_reason(self):
        repo, _, _ = repository(oecd={}, oecd_reason="the OECD did not answer")

        rates = await repo.rates("CHF", [D(2026, 10, 30)])

        assert rates.rates is None
        assert rates.reason == (
            "no OECD, 3-month interbank rate (CHE) for 2026-10-30 (nothing stored); "
            "the last reading failed: the OECD did not answer"
        )

    async def test_a_failed_reading_with_rates_stored_is_not_an_error(self):
        repo, _, _ = repository(
            treasury=[(D(2026, 10, 30), Decimal("0.039"))],
            treasury_reason="the Treasury did not answer",
        )
        rates = await repo.rates("USD", [D(2026, 10, 30)])
        assert (rates.rates, rates.reason) == ({D(2026, 10, 30): 0.039}, None)

    @pytest.mark.parametrize(
        ("currency", "label"),
        [("HKD", "HKD"), (None, "an unknown currency"), ("", "an unknown currency")],
    )
    async def test_a_currency_without_source(self, currency, label):
        repo, treasury, oecd = repository()
        rates = await repo.rates(currency, [D(2026, 10, 30)])
        assert (rates.source, rates.rates, rates.reason) == (
            None,
            None,
            f"no 3-month rate source for {label}",
        )
        assert (treasury.ensured, oecd.refreshed) == ([], 0)

    async def test_without_reading_the_sources(self):
        repo, treasury, oecd = repository(oecd={"EA20": [(D(2026, 10, 1), Decimal("0.0263"))]})
        await repo.rates("EUR", [D(2026, 10, 30)], refresh=False)
        await repo.rates("USD", [D(2026, 10, 30)], refresh=False)
        assert (oecd.refreshed, treasury.ensured) == (0, [])

    async def test_no_day(self):
        repo, _, _ = repository()
        assert await repo.rates("EUR", []) == RiskFreeRates(
            "EUR", "OECD, 3-month interbank rate (EA20)", {}
        )
