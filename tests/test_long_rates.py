"""The latest 10-year rate of the OECD in the currency of a valuation (``macro/long_rates.py``).

The stored rates are replaced by a fake; storing and reading them from the tables
is tested on PostgreSQL (``tests/test_timescale_integration.py``).
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from factors.exposure import EUROPEAN_CURRENCIES
from macro.long_rates import (
    CACHED,
    LIVE,
    MAX_MONTHS_OLD,
    OECD_LONG_AREA_OF_CURRENCY,
    STALE,
    LongRate,
    LongRateRepository,
    long_rate_area,
)
from macro.risk_free_rates import OECD_AREA_OF_CURRENCY
from macro.short_rates import FAILED, FETCHED, FRESH, RatesLoad, StoredArea

D = date
TODAY = D(2026, 11, 25)
READ_AT = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)


class FakeLongRates:
    def __init__(self, stored=None, status=FRESH, reason=None):
        self.stored, self.status, self.reason = stored or {}, status, reason
        self.refreshed, self.asked = 0, []

    async def refresh(self):
        self.refreshed += 1
        return RatesLoad(self.status, READ_AT, reason=self.reason)

    async def series(self, area, start=None, end=None):
        self.asked.append((area, start, end))
        return [(month, rate) for month, rate in self.stored.get(area, []) if start <= month <= end]

    async def stored_areas(self):
        return [
            StoredArea(area, rates[0][0], rates[-1][0], READ_AT)
            for area, rates in sorted(self.stored.items())
            if rates
        ]


def months(*pairs):
    return [(D(2026, month, 1), Decimal(rate)) for month, rate in pairs]


def repository(**kwargs):
    fake = FakeLongRates(**kwargs)
    return LongRateRepository(fake), fake


class TestAreas:
    @pytest.mark.parametrize(
        ("currency", "area"),
        [
            # Germany, not the mean of the euro area (EA20): the yield closest to an AAA one.
            ("EUR", "DEU"),
            ("USD", "USA"),
            # The 10-year rate of the United Kingdom goes on, unlike its 3-month rate.
            ("GBP", "GBR"),
            ("GBX", "GBR"),
            ("GBp", "GBR"),
            ("CHF", "CHE"),
            ("chf", "CHE"),
            ("SEK", "SWE"),
            ("JPY", "JPN"),
            ("BRL", "BRA"),
            ("ZAc", "ZAF"),
            # No 10-year rate at the OECD.
            ("ISK", None),
            ("IDR", None),
            ("HKD", None),
            ("", None),
            (None, None),
        ],
    )
    def test_the_area_of_a_currency(self, currency, area):
        assert long_rate_area(currency) == area
        assert LongRateRepository(FakeLongRates()).has_source(currency) is (area is not None)

    def test_the_areas_of_the_3_month_rates_apart_from_the_differences(self):
        differences = {"EUR", "USD", "GBP", "BRL", "ISK", "IDR"}
        assert {
            currency: area
            for currency, area in OECD_LONG_AREA_OF_CURRENCY.items()
            if currency not in differences
        } == {
            currency: area
            for currency, area in OECD_AREA_OF_CURRENCY.items()
            if currency not in differences
        }

    def test_every_european_currency_of_the_factors_but_iceland(self):
        assert {
            currency for currency in EUROPEAN_CURRENCIES if long_rate_area(currency) is None
        } == {"ISK"}


class TestLatest:
    async def test_the_last_month_stored(self):
        repo, fake = repository(stored={"CHE": months((9, "0.0059"), (10, "0.0051"))})

        rate = await repo.latest("CHF", today=TODAY)

        assert rate == LongRate("CHF", Decimal("0.0051"), CACHED, "CHE", D(2026, 10, 1))
        assert fake.refreshed == 1
        assert fake.asked == [("CHE", D(2026, 8, 1), D(2026, 11, 1))]

    async def test_a_month_not_published_yet_takes_the_month_before(self):
        # China runs a month behind the other areas.
        repo, _ = repository(stored={"CHN": months((8, "0.0169"), (9, "0.0168"))})

        rate = await repo.latest("CNY", today=TODAY)

        assert (rate.value, rate.observation_date) == (Decimal("0.0168"), D(2026, 9, 1))

    async def test_a_minor_unit_is_read_as_its_major_one(self):
        repo, _ = repository(stored={"GBR": months((10, "0.053393"))})

        rate = await repo.latest("GBX", today=TODAY)

        assert (rate.currency, rate.area, rate.value) == ("GBP", "GBR", Decimal("0.053393"))

    async def test_at_most_three_months_old(self):
        assert MAX_MONTHS_OLD == 3
        repo, _ = repository(stored={"DEU": months((7, "0.0307"), (8, "0.0319"))})

        assert (await repo.latest("EUR", today=TODAY)).observation_date == D(2026, 8, 1)
        # A month later, August is four months before December: too old.
        late = await repo.latest("EUR", today=D(2026, 12, 1))
        assert late == LongRate(
            "EUR",
            None,
            None,
            "DEU",
            None,
            "no OECD 10-year rate of DEU since 2026-09 (the last month stored is 2026-08)",
        )

    @pytest.mark.parametrize(
        ("status", "reason", "source"),
        [
            (FETCHED, None, LIVE),
            (FRESH, None, CACHED),
            (FAILED, "the OECD did not answer", STALE),
        ],
    )
    async def test_the_freshness_follows_the_reading(self, status, reason, source):
        repo, _ = repository(stored={"USA": months((10, "0.0513"))}, status=status, reason=reason)

        rate = await repo.latest("USD", today=TODAY)

        assert (rate.source, rate.value, rate.reason) == (source, Decimal("0.0513"), None)

    async def test_a_failed_reading_without_rates_stored(self):
        repo, _ = repository(status=FAILED, reason="the OECD did not answer")

        rate = await repo.latest("SEK", today=TODAY)

        assert rate == LongRate(
            "SEK",
            None,
            None,
            "SWE",
            None,
            "no OECD 10-year rate of SWE since 2026-08 (nothing stored); "
            "the last reading failed: the OECD did not answer",
        )

    async def test_a_currency_without_area_is_not_read(self):
        repo, fake = repository(stored={"ISL": months((10, "0.07"))})

        assert await repo.latest("ISK", today=TODAY) == LongRate(
            "ISK", None, None, None, None, "no OECD 10-year rate for ISK"
        )
        assert await repo.latest(None, today=TODAY) == LongRate(
            None, None, None, None, None, "no OECD 10-year rate for no currency"
        )
        assert (fake.refreshed, fake.asked) == (0, [])
