"""The risk of a listing (``risk/service.py``) and its route ``GET /risk/{ticker}``.

The listing and its closes are given by a fake reader, the risk-free rates by a
fake repository: the metrics themselves are tested in ``tests/test_risk_metrics.py``.
"""

import math
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from macro.risk_free_rates import RiskFreeRates
from main import app
from risk.metrics import MIN_RETURNS, simple_returns
from risk.service import RiskError, RiskService, window

LAST = date(2026, 11, 20)


def closes_of(count: int, last: date = LAST) -> dict[date, float]:
    """``count`` daily closes ending on ``last``, rising and falling."""
    first = last - timedelta(days=count - 1)
    return {
        first + timedelta(days=i): 100 * (1 + 0.0004 * i) + 3 * math.sin(i / 5)
        for i in range(count)
    }


class FakeRates:
    def __init__(self, rate=0.03, reason=None, source="OECD, 3-month interbank rate (GBR)"):
        self.rate, self.reason, self.source, self.asked = rate, reason, source, []

    async def rates(self, currency, days, *, refresh=True):
        days = list(days)
        self.asked.append((currency, days))
        if self.reason:
            return RiskFreeRates("GBP", self.source, None, self.reason)
        return RiskFreeRates(
            "EUR",
            "OECD, 3-month interbank rate (EA20)",
            {day: self.rate for day in days},
            first_observation=date(2023, 11, 1),
            last_observation=date(2026, 10, 1),
        )


def service(closes, rates=None, listing=True, currency="EUR") -> RiskService:
    risk = RiskService(None, rates or FakeRates())  # type: ignore[arg-type]

    async def listing_of(ticker, currency_, exchange, isin):
        if not listing:
            return None, {}
        identity = {
            "ticker": ticker,
            "isin": "FR0000120073",
            "currency": currency,
            "exchange": "XPAR",
        }
        return identity, closes

    risk._listing = listing_of  # type: ignore[method-assign]
    return risk


class TestWindow:
    def test_the_last_years_before_the_last_close(self):
        closes = closes_of(2000)
        kept = window(closes, 3)
        assert max(kept) == LAST
        assert min(kept) == LAST - timedelta(days=1096)  # 3 × 365.25 days, rounded
        assert window({}, 3) == {}


class TestMeasure:
    async def test_every_metric_with_the_rate_of_the_currency(self):
        closes, rates = closes_of(1500), FakeRates(0.03)

        result = await service(closes, rates).measure("AI", years=2, confidence=0.99)

        kept = window(closes, 2)
        assert (result.metrics.start, result.metrics.end) == (min(kept), LAST)
        assert result.metrics.returns == len(kept) - 1
        assert result.metrics.confidence == 0.99
        assert result.metrics.sharpe_ratio is not None
        assert result.metrics.mean_risk_free_rate == pytest.approx(0.03)
        assert result.warnings == []
        # The rate is asked for the day of each return, in the currency of the listing.
        currency, days = rates.asked[0]
        assert currency == "EUR"
        assert days == [day for day, _ in simple_returns(kept)]

    async def test_without_rate_sharpe_and_sortino_are_left_out(self):
        rates = FakeRates(reason="no OECD, 3-month interbank rate (GBR) for 2026-06-01")

        result = await service(closes_of(800), rates, currency="GBX").measure("SHEL")

        assert (result.metrics.sharpe_ratio, result.metrics.sortino_ratio) == (None, None)
        assert result.metrics.annualized_volatility is not None
        assert result.warnings == [
            "Sharpe and Sortino not computed: no OECD, 3-month interbank rate (GBR) for 2026-06-01"
        ]
        assert rates.asked[0][0] == "GBX"

    async def test_no_listing(self):
        with pytest.raises(RiskError, match="No listing found for ACME") as error:
            await service({}, listing=False).measure("ACME")
        assert error.value.status == 404

    async def test_too_few_prices(self):
        with pytest.raises(RiskError, match=f"at least {MIN_RETURNS + 1} are needed") as error:
            await service(closes_of(MIN_RETURNS)).measure("AI")
        assert error.value.status == 404

    async def test_a_confidence_the_metrics_refuse(self):
        with pytest.raises(RiskError, match="confidence") as error:
            await service(closes_of(200)).measure("AI", confidence=1.5)
        assert error.value.status == 422


class AsyncStore:
    def __init__(self):
        self.entries: dict[str, str] = {}

    async def get(self, key):
        return self.entries.get(key)

    async def setex(self, key, ttl, value):
        self.entries[key] = value


@pytest.fixture
def client():
    names = ("risk_service", "redis_client")
    originals = {name: getattr(app.state, name, None) for name in names}
    with TestClient(app) as test_client:
        started = {name: getattr(app.state, name, None) for name in names}
        app.state.risk_service = service(closes_of(1500))
        app.state.redis_client = AsyncStore()
        yield test_client
        # The shutdown closes the Redis client it started, not the fake one.
        for name, value in started.items():
            setattr(app.state, name, value)
    for name, value in originals.items():
        setattr(app.state, name, value)


class TestRoute:
    def test_the_metrics_and_the_rate_used(self, client):
        answer = client.get("/risk/AI?years=2").json()

        assert answer["ticker"] == "AI"
        assert answer["listing"]["currency"] == "EUR"
        assert (answer["years"], answer["end"], answer["confidence"]) == (2, "2026-11-20", 0.95)
        assert answer["unit"] == "ratio"
        assert answer["historical_var"] < 0 and answer["max_drawdown"]["value"] < 0
        assert answer["sharpe_ratio"] is not None
        assert answer["risk_free_rate"] == {
            "currency": "EUR",
            "source": "OECD, 3-month interbank rate (EA20)",
            "mean": pytest.approx(0.03),
            "first_observation": "2023-11-01",
            "last_observation": "2026-10-01",
            "reason": None,
        }
        assert answer["warnings"] == []

    def test_cached_then_measured_again_on_demand(self, client):
        first = client.get("/risk/AI").json()
        app.state.risk_service = service(closes_of(1500), FakeRates(0.05))

        assert client.get("/risk/AI").json() == first
        again = client.get("/risk/AI?force_refresh=true").json()
        assert again["risk_free_rate"]["mean"] == pytest.approx(0.05)

    def test_an_answer_without_the_rate_of_its_source_is_not_kept(self, client):
        """The OECD over its quota gives no rate: the next request must read it again,
        not serve the answer without Sharpe and Sortino for 6 hours."""
        failed = FakeRates(reason="no OECD, 3-month interbank rate (GBR) for 2026-06-01")
        app.state.risk_service = service(closes_of(1500), failed)

        degraded = client.get("/risk/AI").json()
        assert degraded["sharpe_ratio"] is None
        assert app.state.redis_client.entries == {}

        app.state.risk_service = service(closes_of(1500), FakeRates(0.03))
        assert client.get("/risk/AI").json()["sharpe_ratio"] is not None
        assert len(app.state.redis_client.entries) == 1

    def test_a_currency_without_source_is_kept(self, client):
        """No source at all gives the same answer every time."""
        none = FakeRates(reason="no 3-month rate source for HKD", source=None)
        app.state.risk_service = service(closes_of(1500), none, currency="HKD")

        assert client.get("/risk/AI").json()["sharpe_ratio"] is None
        assert len(app.state.redis_client.entries) == 1

    @pytest.mark.parametrize("other", ["years=5", "confidence=0.99", "currency=USD"])
    def test_two_requests_differing_by_one_parameter_are_not_served_one_answer(self, client, other):
        first = client.get("/risk/AI").json()
        second = client.get(f"/risk/AI?{other}").json()
        assert len(app.state.redis_client.entries) == 2
        assert (first["years"], first["confidence"]) == (3, 0.95)
        if other.startswith("years"):
            assert second["years"] == 5
        if other.startswith("confidence"):
            assert second["confidence"] == 0.99

    def test_an_entry_of_an_older_shape_is_measured_again(self, client):
        app.state.redis_client.entries["risk:AI:3y:0.95:::"] = '{"ticker": "AI"}'
        assert client.get("/risk/AI").json()["returns"] > 0

    def test_without_redis(self, client):
        app.state.redis_client = None
        assert client.get("/risk/AI").status_code == 200

    @pytest.mark.parametrize("query", ["years=0", "years=11", "confidence=0.4", "confidence=1"])
    def test_parameters_refused(self, client, query):
        assert client.get(f"/risk/AI?{query}").status_code == 422

    def test_what_cannot_be_measured(self, client):
        app.state.risk_service = service({}, listing=False)
        response = client.get("/risk/ACME")
        assert (response.status_code, response.json()["detail"]) == (
            404,
            "No listing found for ACME",
        )

    def test_without_database(self, client):
        app.state.risk_service = None
        assert client.get("/risk/AI").status_code == 503
