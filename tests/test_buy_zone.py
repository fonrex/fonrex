"""Dividend buy zone of ``/fundamental``: a share whose net yield reaches a target.

The figures are the ones of two theScreener reports: Amundi at 91.85 EUR with a
dividend of 4.25 EUR, TF1 at 6.17 EUR with a dividend of 0.63 EUR.
"""

import json
from types import SimpleNamespace

import pytest

from financials.buy_zone import SECTION, apply_buy_zone, dividend_buy_zone
from financials.formatter import FinancialsFormatter
from routers.fundamentals import get_all_information


def test_a_share_whose_net_yield_reaches_the_target_is_in_the_zone():
    zone = dividend_buy_zone(price=6.17, dividend=0.63, target_yield=0.065)

    assert zone["GrossYield"] == pytest.approx(0.63 / 6.17, abs=1e-6)
    assert zone["NetYield"] == zone["GrossYield"]
    assert zone["InBuyZone"] is True
    assert zone["MaxBuyPrice"] == pytest.approx(0.63 / 0.065, abs=1e-4)
    assert zone["YieldGap"] == pytest.approx((0.63 / 6.17 - 0.065) / 0.065, abs=1e-6)


def test_a_share_whose_net_yield_is_below_the_target_is_not():
    zone = dividend_buy_zone(price=91.85, dividend=4.25, target_yield=0.065)

    assert zone["InBuyZone"] is False
    assert zone["YieldGap"] < 0
    assert zone["MaxBuyPrice"] == pytest.approx(4.25 / 0.065, abs=1e-4)


def test_the_tax_lowers_the_net_yield_and_the_highest_buy_price():
    zone = dividend_buy_zone(price=6.17, dividend=0.63, target_yield=0.065, dividend_tax=0.3)

    assert zone["NetDividendShare"] == pytest.approx(0.441)
    assert zone["NetYield"] == pytest.approx(0.63 * 0.7 / 6.17, abs=1e-6)
    assert zone["MaxBuyPrice"] == pytest.approx(0.441 / 0.065, abs=1e-4)


def test_the_highest_buy_price_follows_the_unit_of_the_price():
    # A London listing: price in pence, dividend in pounds, yield published by Yahoo.
    zone = dividend_buy_zone(price=2500.0, dividend=1.0, gross_yield=0.04, target_yield=0.05)

    assert zone["GrossYield"] == 0.04
    assert zone["MaxBuyPrice"] == pytest.approx(2000.0)


def test_without_a_target_only_the_yields_are_rendered():
    zone = dividend_buy_zone(price=6.17, dividend=0.63)

    assert zone["NetYield"] is not None
    assert zone["TargetYield"] is None
    assert zone["InBuyZone"] is None and zone["MaxBuyPrice"] is None and zone["YieldGap"] is None


def test_without_a_price_the_highest_buy_price_comes_from_the_dividend():
    zone = dividend_buy_zone(dividend=0.63, target_yield=0.065)

    assert zone["GrossYield"] is None and zone["InBuyZone"] is None
    assert zone["MaxBuyPrice"] == pytest.approx(0.63 / 0.065, abs=1e-4)


@pytest.mark.parametrize("value", [None, "6.17", float("nan"), True])
def test_unusable_figures_are_no_figures(value):
    zone = dividend_buy_zone(price=value, dividend=value, gross_yield=value, target_yield=0.065)

    assert zone["Price"] is None and zone["GrossYield"] is None and zone["MaxBuyPrice"] is None


def test_the_document_holds_the_zone_and_names_its_sources():
    document = FinancialsFormatter.to_eodhd(
        {
            "YahooFinance": {
                "currentPrice": 6.17,
                "dividendRate": 0.63,
                "dividendYield": 10.21,
                "payoutRatio": 1.522,
            }
        }
    )

    zone = document[SECTION]
    assert zone["Price"] == 6.17
    assert zone["DividendShare"] == 0.63
    assert zone["GrossYield"] == pytest.approx(0.1021)
    assert zone["PayoutRatio"] == 1.522
    assert zone["TargetYield"] is None
    assert document["Sources"][SECTION] == {
        "Price": "YahooFinance",
        "DividendShare": "YahooFinance",
        "GrossYield": "YahooFinance",
        "PayoutRatio": "YahooFinance",
    }


def test_a_document_without_the_zone_is_left_unchanged():
    document = {"General": {}}

    assert apply_buy_zone(document, 0.065) is document


class AsyncStore:
    def __init__(self):
        self.entries: dict[str, str] = {}

    async def get(self, key):
        return self.entries.get(key)

    async def setex(self, key, ttl, value):
        self.entries[key] = value


@pytest.mark.asyncio
async def test_one_cached_document_serves_every_target_yield():
    store = AsyncStore()
    cached = {SECTION: dividend_buy_zone(price=6.17, dividend=0.63)}
    store.entries["fundamental:TFI.PA:None:None:eodhd:all"] = json.dumps(cached)
    request = SimpleNamespace(state=SimpleNamespace())

    async def ask(**parameters):
        return await get_all_information(
            request=request,
            ticker="TFI.PA",
            isin=None,
            exchange=None,
            currency=None,
            provider=None,
            fmt="eodhd",
            nocache=False,
            database=None,
            redis_client=store,
            providers={},
            validation_layer=None,
            sec_edgar_provider=None,
            **parameters,
        )

    low = await ask(target_yield=0.065, dividend_tax=0.0)
    high = await ask(target_yield=0.12, dividend_tax=0.3)

    assert low[SECTION]["InBuyZone"] is True
    assert high[SECTION]["InBuyZone"] is False
    assert high[SECTION]["DividendTax"] == 0.3
    # The cache keeps the document without a target.
    assert json.loads(store.entries["fundamental:TFI.PA:None:None:eodhd:all"]) == cached
