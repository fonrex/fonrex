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
from risk.service import RiskError, RiskService, closes_on_one_basis, window

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


def bars_of(closes, adjusted=True):
    """Daily bars ``(adj_close, close)``; without ``adjusted``, no adj_close at all."""
    return {day: (price if adjusted else None, price) for day, price in closes.items()}


def service(closes, rates=None, listing=True, currency="EUR", bars=None) -> RiskService:
    risk = RiskService(None, rates or FakeRates())  # type: ignore[arg-type]
    bars = bars if bars is not None else bars_of(closes)

    async def listing_of(ticker, currency_, exchange, isin):
        if not listing:
            return None, {}
        identity = {
            "ticker": ticker,
            "isin": "FR0000120073",
            "currency": currency,
            "exchange": "XPAR",
        }
        return identity, bars

    risk._listing = listing_of  # type: ignore[method-assign]
    return risk


class TestWindow:
    def test_the_last_years_before_the_last_close(self):
        closes = closes_of(2000)
        kept = window(closes, 3)
        assert max(kept) == LAST
        assert min(kept) == LAST - timedelta(days=1096)  # 3 × 365.25 days, rounded
        assert window({}, 3) == {}


class TestOneBasis:
    """Every close of a window on one basis: an adjusted close of 95 followed by a
    traded close of 100 would make a return of 5.26 % that never happened."""

    def test_the_adjusted_closes_when_every_day_has_one(self):
        bars = {date(2026, 11, 19): (95.0, 100.0), date(2026, 11, 20): (96.0, 101.0)}
        assert closes_on_one_basis(bars) == (
            {date(2026, 11, 19): 95.0, date(2026, 11, 20): 96.0},
            None,
        )

    def test_the_traded_closes_when_one_day_lacks_it(self):
        bars = {date(2026, 11, 19): (95.0, 100.0), date(2026, 11, 20): (None, 101.0)}
        closes, warning = closes_on_one_basis(bars)
        assert closes == {date(2026, 11, 19): 100.0, date(2026, 11, 20): 101.0}
        assert warning == (
            "1 of 2 daily closes have no adjusted close: every return is measured on the "
            "traded closes, dividends not included"
        )

    def test_a_day_with_no_traded_close_is_left_out_of_that_basis(self):
        bars = {date(2026, 11, 18): (94.0, None), date(2026, 11, 19): (None, 100.0)}
        assert closes_on_one_basis(bars)[0] == {date(2026, 11, 19): 100.0}

    async def test_only_the_window_decides(self):
        """Old bars without adj_close, before the window, change nothing."""
        closes = closes_of(1500)
        bars = bars_of(closes)
        for day in sorted(closes)[:100]:
            bars[day] = (None, closes[day] * 1.1)

        result = await service(closes, bars=bars).measure("AI", years=2)

        assert result.warnings == []

    async def test_a_mixed_window_is_measured_on_the_traded_closes(self):
        closes = closes_of(800)
        traded = {day: price * 1.05 for day, price in closes.items()}
        bars = {day: (closes[day], traded[day]) for day in closes}
        bars[LAST] = (None, traded[LAST])

        result = await service(closes, bars=bars).measure("AI", years=2)

        measured = await service(traded, bars=bars_of(traded)).measure("AI", years=2)
        assert result.metrics.annualized_return == pytest.approx(measured.metrics.annualized_return)
        assert result.warnings[0].startswith("1 of 731 daily closes have no adjusted close")


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
    names = ("risk_service", "redis_client", "db_available")
    originals = {name: getattr(app.state, name, None) for name in names}
    with TestClient(app) as test_client:
        started = {name: getattr(app.state, name, None) for name in names}
        app.state.risk_service = service(closes_of(1500))
        app.state.redis_client = AsyncStore()
        # The startup finds no database: the route is tested with one available.
        app.state.db_available = True
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
        none = FakeRates(reason="no short-term rate source for HKD", source=None)
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

    def test_without_risk_service(self, client):
        app.state.risk_service = None
        response = client.get("/risk/AI")
        assert (response.status_code, response.json()["detail"]) == (
            503,
            "Risk metrics unavailable (no database, or OECD or Treasury not started)",
        )

    def test_a_database_down_or_not_at_the_head_is_not_read(self, client):
        """The service exists even when the startup refused the database (rule 24)."""
        app.state.db_available = False
        response = client.get("/risk/AI")
        assert (response.status_code, response.json()["detail"]) == (
            503,
            "Database schema unavailable or outdated",
        )

    def test_the_openbb_widget_shares_the_answer_of_the_route(self, client):
        rows = client.get("/openbb/risk/AI?years=2&confidence=0.99").json()

        by_metric = {row["Metric"]: row for row in rows}
        assert by_metric["Period"]["Value"].endswith("→ 2026-11-20")
        assert "Historical VaR (99.0%)" in by_metric
        assert by_metric["Risk-free rate (EUR, mean)"]["Value"] == "3.00%"
        # One measure for the route and the widget: the same cache key.
        assert list(app.state.redis_client.entries) == ["risk:AI:2y:0.99:::"]
        assert client.get("/risk/AI?years=2&confidence=0.99").json()["years"] == 2
        assert len(app.state.redis_client.entries) == 1

    def test_the_openbb_widget_without_risk_service(self, client):
        app.state.risk_service = None
        assert client.get("/openbb/risk/AI").status_code == 503

    def test_two_confidences_equal_to_six_digits_are_two_answers(self, client):
        first = client.get("/risk/AI?confidence=0.9500001").json()
        second = client.get("/risk/AI?confidence=0.9500002").json()
        assert (first["confidence"], second["confidence"]) == (0.9500001, 0.9500002)
        assert len(app.state.redis_client.entries) == 2
