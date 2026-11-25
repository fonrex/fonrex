"""The cache of the routes ``GET /dcf/{ticker}`` and ``GET /dcf/{ticker}/compare``.

A valuation is computed by a fake service: the valuation itself is tested in
``tests/test_dcf_service.py`` and ``tests/test_dcf_currency.py``.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from main import app
from schemas.dcf import DCFModelResult, DCFResult, WACCResult


class AsyncStore:
    def __init__(self):
        self.entries: dict[str, str] = {}

    async def get(self, key):
        return self.entries.get(key)

    async def setex(self, key, ttl, value):
        self.entries[key] = value


def valuation(currency: str, source: str, rate: str = "0.04") -> DCFResult:
    fcf = DCFModelResult(
        model_name="FCF",
        intrinsic_value_per_share=Decimal("100"),
        projected_values=[],
        terminal_value=Decimal("0"),
        present_values=[],
        pv_terminal=Decimal("0"),
    )
    return DCFResult(
        ticker="ACME",
        currency=currency,
        wacc=WACCResult(
            wacc=Decimal("0.08"),
            cost_of_equity=Decimal("0.095"),
            cost_of_debt=Decimal("0.05"),
            tax_rate=Decimal("0.25"),
            weight_equity=Decimal("0.8"),
            weight_debt=Decimal("0.2"),
            beta_used=Decimal("1"),
            risk_free_rate=Decimal(rate),
            risk_free_rate_source=source,
        ),
        models={"fcf": fcf, "eps": fcf, "ddm": fcf},
    )


class FakeDCF:
    """Gives the valuations in turn, the last one repeated; currencies in ``sources``
    have a source of their rate."""

    def __init__(self, *results: DCFResult, sources=("EUR", "USD", "CHF", "GBP")):
        self.results, self.sources, self.computed = list(results), set(sources), 0

    async def compute_dcf(self, ticker, request):
        self.computed += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def has_rate_source(self, currency):
        return currency in self.sources


@pytest.fixture
def client():
    names = ("dcf_service", "redis_client")
    originals = {name: getattr(app.state, name, None) for name in names}
    with TestClient(app) as test_client:
        started = {name: getattr(app.state, name, None) for name in names}
        app.state.redis_client = AsyncStore()
        yield test_client
        for name, value in started.items():
            setattr(app.state, name, value)
    for name, value in originals.items():
        setattr(app.state, name, value)


@pytest.mark.parametrize("path", ["/dcf/ACME", "/dcf/ACME/compare"])
class TestCache:
    def test_a_valuation_on_the_rate_of_its_source_is_kept(self, client, path):
        app.state.dcf_service = FakeDCF(valuation("CHF", "oecd_cached", "0.0051"))

        first = client.get(path).json()
        again = client.get(path).json()

        assert first["wacc"]["risk_free_rate_source"] == "oecd_cached"
        assert again == first
        assert app.state.dcf_service.computed == 1
        assert len(app.state.redis_client.entries) == 1

    def test_a_valuation_on_the_configured_rate_is_not_kept_when_a_source_exists(
        self, client, path
    ):
        """The OECD over its quota for a moment: the next request computes again, rather
        than serving a valuation on DCF_RISK_FREE_RATE for 6 hours."""
        app.state.dcf_service = FakeDCF(
            valuation("CHF", "env_fallback"), valuation("CHF", "oecd_live", "0.0051")
        )

        degraded = client.get(path).json()
        assert degraded["wacc"]["risk_free_rate_source"] == "env_fallback"
        assert app.state.redis_client.entries == {}

        assert client.get(path).json()["wacc"]["risk_free_rate_source"] == "oecd_live"
        assert app.state.dcf_service.computed == 2
        assert len(app.state.redis_client.entries) == 1

    def test_a_currency_without_source_is_kept(self, client, path):
        """No source at all gives the same valuation every time."""
        app.state.dcf_service = FakeDCF(valuation("HKD", "env_fallback"))

        client.get(path)
        client.get(path)

        assert app.state.dcf_service.computed == 1
        assert len(app.state.redis_client.entries) == 1
