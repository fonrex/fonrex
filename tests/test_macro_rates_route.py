"""``GET /macro/rates?currency=``: the rates of FRED (USD) and of the ECB (EUR).

The route used to give the US rate only. It now gives the rates of the currency
asked, or of every source, and the OpenBB tile shows one card per series.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from integrations.openbb.adapters import format_macro_rates_metric
from main import app
from schemas.macro import MacroRate, MacroRatesResponse

DAY = date(2026, 10, 8)
DGS10 = MacroRate(
    series_id="DGS10", value=Decimal("0.0412"), observation_date=DAY, source="fred", currency="USD"
)
EURO_10Y = MacroRate(
    series_id="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y",
    value=Decimal("0.035192"),
    observation_date=DAY,
    source="ecb",
    currency="EUR",
)
DEPOSIT = MacroRate(
    series_id="FM.D.U2.EUR.4F.KR.DFR.LEV",
    value=Decimal("0.02"),
    observation_date=DAY,
    source="ecb",
    currency="EUR",
    freshness="stale",
)
CISS = MacroRate(
    series_id="CISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX",
    value=Decimal("0.081234"),
    unit="index",
    observation_date=DAY,
    source="ecb",
    currency="EUR",
)


def _source(answer: MacroRatesResponse):
    service = MagicMock()
    service.get_rates = AsyncMock(return_value=answer)
    return service


@pytest.fixture
def sources():
    fred = _source(MacroRatesResponse(currency="USD", risk_free_rate=DGS10, rates=[DGS10]))
    ecb = _source(
        MacroRatesResponse(currency="EUR", risk_free_rate=EURO_10Y, rates=[EURO_10Y, DEPOSIT, CISS])
    )
    names = ("redis_client", "db_service", "db_available", "fred_service", "ecb_service")
    originals = {name: getattr(app.state, name, None) for name in names}
    with TestClient(app) as client:
        app.state.redis_client = AsyncMock(get=AsyncMock(return_value=None))
        app.state.db_service = MagicMock()
        app.state.db_available = True
        app.state.fred_service = fred
        app.state.ecb_service = ecb
        yield MagicMock(client=client, fred=fred, ecb=ecb)
    for name, value in originals.items():
        setattr(app.state, name, value)


def _series(answer):
    return [rate["series_id"] for rate in answer["rates"]]


class TestRoute:
    def test_without_currency_every_source_is_given_and_the_risk_free_rate_is_us(self, sources):
        answer = sources.client.get("/macro/rates").json()

        assert answer["currency"] is None
        assert answer["risk_free_rate"]["series_id"] == "DGS10"
        assert _series(answer) == [
            "DGS10",
            EURO_10Y.series_id,
            DEPOSIT.series_id,
            CISS.series_id,
        ]

    @pytest.mark.parametrize("asked", ["EUR", "eur"])
    def test_euro_rates_only_and_the_euro_risk_free_rate(self, sources, asked):
        answer = sources.client.get(f"/macro/rates?currency={asked}").json()

        assert answer["currency"] == "EUR"
        assert answer["risk_free_rate"]["series_id"] == EURO_10Y.series_id
        assert {rate["source"] for rate in answer["rates"]} == {"ecb"}
        sources.fred.get_rates.assert_not_awaited()

    def test_dollar_rates_only(self, sources):
        answer = sources.client.get("/macro/rates?currency=USD").json()

        assert _series(answer) == ["DGS10"]
        sources.ecb.get_rates.assert_not_awaited()

    def test_empty_currency_is_every_source(self, sources):
        # The "USD and EUR" choice of the OpenBB widget sends currency=.
        assert len(sources.client.get("/macro/rates?currency=").json()["rates"]) == 4

    def test_a_currency_without_source_is_refused(self, sources):
        response = sources.client.get("/macro/rates?currency=GBP")

        assert response.status_code == 422
        assert "EUR, USD" in response.json()["detail"]

    def test_a_source_not_started_leaves_the_others(self, sources):
        app.state.ecb_service = None

        answer = sources.client.get("/macro/rates").json()

        assert _series(answer) == ["DGS10"]
        assert sources.client.get("/macro/rates?currency=EUR").status_code == 503

    def test_the_risk_free_rate_is_empty_when_its_source_is_missing(self, sources):
        app.state.fred_service = None

        answer = sources.client.get("/macro/rates").json()

        assert answer["risk_free_rate"] is None
        assert len(answer["rates"]) == 3

    def test_no_source_at_all_is_unavailable(self, sources):
        app.state.fred_service = app.state.ecb_service = None

        assert sources.client.get("/macro/rates").status_code == 503


class TestOpenBBTile:
    def test_one_card_per_series(self, sources):
        cards = sources.client.get("/openbb/macro/rates").json()

        assert cards == [
            {"label": "US 10Y Treasury (2026-10-08)", "value": "4.12%", "delta": None},
            {"label": "Euro AAA 10Y (2026-10-08)", "value": "3.52%", "delta": None},
            {"label": "ECB deposit rate (2026-10-08, stale)", "value": "2.00%", "delta": None},
            {"label": "Euro stress index (CISS) (2026-10-08)", "value": "0.0812", "delta": None},
        ]

    def test_the_currency_is_passed_on(self, sources):
        cards = sources.client.get("/openbb/macro/rates?currency=USD").json()

        assert [card["label"] for card in cards] == ["US 10Y Treasury (2026-10-08)"]

    def test_an_unknown_series_keeps_its_label(self):
        (card,) = format_macro_rates_metric(
            {"rates": [{"series_id": "X", "label": "Other", "value": "0.01"}]}
        )
        assert card == {"label": "Other", "value": "1.00%", "delta": None}
