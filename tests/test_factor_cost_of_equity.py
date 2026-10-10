"""Cost of equity from the Fama/French factors in the valuation (``factors/cost_of_equity.py``)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import valuation.dcf_service as dcf_module
from factors.cost_of_equity import FactorCostOfEquity, factor_cost_of_equity
from factors.exposure import Coefficient, ExposureError, ExposureResult, Regression
from schemas.dcf import DCFRequest, WACCInput, WACCResult
from valuation.dcf_service import DCFService

US_HISTORY = {
    date(2026, 6, 30): {
        "MKT_RF": Decimal("0.01"),
        "SMB": Decimal("0.002"),
        "HML": Decimal("-0.001"),
        "RF": Decimal("0.003"),
    },
    date(2026, 7, 31): {
        "MKT_RF": Decimal("0.00"),
        "SMB": Decimal("0.000"),
        "HML": Decimal("0.003"),
        "RF": Decimal("0.003"),
    },
}


def _coefficient(value):
    return Coefficient(value, 0.05, value / 0.05)


def _exposure(converted_from=None, error=None):
    regression = Regression(
        alpha=_coefficient(0.001),
        betas={"MKT_RF": _coefficient(1.2), "SMB": _coefficient(-0.3), "HML": _coefficient(0.5)},
        r_squared=0.62,
        adj_r_squared=0.6,
        residual_std=0.04,
        periods=60,
    )
    result = ExposureResult(
        ticker="ACME",
        listing={},
        model="ff3",
        region="us",
        frequency="monthly",
        datasets=["us_3"],
        price_currency="USD",
        converted_from=converted_from,
        start=date(2021, 8, 31),
        end=date(2026, 7, 31),
        regression=regression,
        warnings=["Only 30 monthly periods: the estimates are imprecise."]
        if converted_from
        else [],
    )
    library = SimpleNamespace(series=AsyncMock(return_value=US_HISTORY))
    return SimpleNamespace(
        measure=AsyncMock(side_effect=error, return_value=result), library=library
    )


class TestFactorCostOfEquity:
    async def test_betas_of_the_listing_and_long_run_premia_of_its_region(self):
        exposure = _exposure()

        factor = await factor_cost_of_equity(exposure, "ACME", "ff3")

        exposure.measure.assert_awaited_once_with("ACME", model="ff3", frequency="monthly")
        exposure.library.series.assert_awaited_once_with("us_3", "monthly")
        # Mean monthly return × 12: MKT_RF (0.01 + 0) / 2 × 12 = 0.06
        assert factor.premia == {
            "MKT_RF": Decimal("0.0600"),
            "SMB": Decimal("0.0120"),
            "HML": Decimal("0.0120"),
        }
        assert factor.betas == {
            "MKT_RF": Decimal("1.2000"),
            "SMB": Decimal("-0.3000"),
            "HML": Decimal("0.5000"),
        }
        # 1.2 × 0.06 − 0.3 × 0.012 + 0.5 × 0.012 = 0.0744
        assert factor.premium == Decimal("0.0744")
        assert (factor.market_beta, factor.periods, factor.r_squared) == (
            Decimal("1.2000"),
            60,
            0.62,
        )
        assert factor.warnings == []

    async def test_a_listing_in_another_currency_is_an_approximation(self):
        factor = await factor_cost_of_equity(_exposure(converted_from="EUR"), "AIR", "ff3")

        assert len(factor.warnings) == 2
        assert "approximation" in factor.warnings[1]

    async def test_an_exposure_that_cannot_be_measured_stops_the_valuation(self):
        exposure = _exposure(error=ExposureError(404, "No daily prices stored for ACME"))

        with pytest.raises(
            ValueError, match="Factor cost of equity unavailable for ACME: No daily"
        ):
            await factor_cost_of_equity(exposure, "ACME", "ff3")

    async def test_a_factor_without_history(self):
        exposure = _exposure()
        exposure.library.series.return_value = {}

        with pytest.raises(ValueError, match="No stored history of the factors MKT_RF, SMB, HML"):
            await factor_cost_of_equity(exposure, "ACME", "ff3")


# ── In the WACC ──────────────────────────────────────────────────────────────

CAPM = WACCResult(
    wacc=Decimal("0.0900"),
    cost_of_equity=Decimal("0.1000"),
    cost_of_debt=Decimal("0.0400"),
    tax_rate=Decimal("0.2500"),
    weight_equity=Decimal("0.8000"),
    weight_debt=Decimal("0.2000"),
    beta_used=Decimal("1.0000"),
    cost_of_debt_source="calculated",
    risk_free_rate=Decimal("0.0400"),
    risk_free_rate_source="fred_live",
)


def _factor(premium_betas=None, warnings=None):
    return FactorCostOfEquity(
        model="ff3",
        region="us",
        betas=premium_betas
        or {"MKT_RF": Decimal("1.2"), "SMB": Decimal("-0.3"), "HML": Decimal("0.5")},
        premia={"MKT_RF": Decimal("0.06"), "SMB": Decimal("0.012"), "HML": Decimal("0.006")},
        start=date(2021, 8, 31),
        end=date(2026, 7, 31),
        periods=60,
        r_squared=0.6234,
        warnings=warnings or [],
    )


class TestWacc:
    def test_the_cost_of_equity_is_the_risk_free_rate_plus_the_factor_premia(self):
        wacc, warnings = DCFService._with_factor_cost_of_equity(CAPM, _factor(), None)

        assert wacc.cost_of_equity == Decimal("0.1114")  # 0.04 + 0.0714
        # 0.8 × 0.1114 + 0.2 × 0.04 × (1 − 0.25)
        assert wacc.wacc == Decimal("0.0951")
        assert (wacc.beta_used, wacc.cost_of_equity_model) == (Decimal("1.2"), "ff3")
        detail = wacc.factor_cost_of_equity
        assert (detail.premium, detail.periods, detail.r_squared) == (Decimal("0.0714"), 60, 0.6234)
        assert wacc.risk_free_rate_source == "fred_live"  # kept
        assert warnings == []

    def test_capm_overrides_are_said_to_be_unused(self):
        params = WACCInput(beta_override=Decimal("1.5"), cost_of_equity_model="ff3")

        _, warnings = DCFService._with_factor_cost_of_equity(CAPM, _factor(), params)

        assert "not used with the ff3 cost of equity" in warnings[0]

    def test_negative_exposures_below_the_risk_free_rate(self):
        betas = {"MKT_RF": Decimal("0.1"), "SMB": Decimal("-2"), "HML": Decimal("-5")}

        wacc, warnings = DCFService._with_factor_cost_of_equity(CAPM, _factor(betas), None)

        assert wacc.cost_of_equity < CAPM.risk_free_rate
        assert wacc.wacc == Decimal("0.0500")  # kept within 5–20 %
        assert "below the risk-free rate" in warnings[-1]

    def test_the_warnings_of_the_measure_are_kept(self):
        _, warnings = DCFService._with_factor_cost_of_equity(
            CAPM, _factor(warnings=["short"]), None
        )

        assert warnings == ["short"]


class TestValuation:
    async def test_the_capm_does_not_measure_any_exposure(self, monkeypatch):
        exposure = MagicMock()
        service = DCFService(MagicMock(), factor_exposure=exposure)
        monkeypatch.setattr(service, "_currency_of", lambda ticker: "USD")
        monkeypatch.setattr(service, "_risk_free_rate", AsyncMock(return_value=None))
        compute = MagicMock(return_value="result")
        monkeypatch.setattr(service, "_compute_dcf_sync", compute)

        assert await service.compute_dcf("ACME", DCFRequest()) == "result"
        assert compute.call_args.args[3] is None
        exposure.measure.assert_not_called()

    async def test_a_factor_model_is_measured_and_passed_to_the_valuation(self, monkeypatch):
        service = DCFService(MagicMock(), factor_exposure=MagicMock())
        monkeypatch.setattr(service, "_currency_of", lambda ticker: "USD")
        monkeypatch.setattr(service, "_risk_free_rate", AsyncMock(return_value=None))
        compute = MagicMock(return_value="result")
        monkeypatch.setattr(service, "_compute_dcf_sync", compute)
        measured = AsyncMock(return_value=_factor())
        monkeypatch.setattr(dcf_module, "factor_cost_of_equity", measured)

        request = DCFRequest(wacc_params=WACCInput(cost_of_equity_model="carhart"))
        await service.compute_dcf("ACME", request)

        assert measured.await_args.args[1:] == ("ACME", "carhart")
        assert compute.call_args.args[3] == _factor()

    async def test_without_database_a_factor_model_is_refused(self, monkeypatch):
        service = DCFService(MagicMock())
        monkeypatch.setattr(service, "_currency_of", lambda ticker: "USD")
        monkeypatch.setattr(service, "_risk_free_rate", AsyncMock(return_value=None))

        with pytest.raises(ValueError, match="database is not configured"):
            await service.compute_dcf(
                "ACME", DCFRequest(wacc_params=WACCInput(cost_of_equity_model="ff5"))
            )

    def test_an_unknown_model_is_refused(self):
        with pytest.raises(ValueError):
            WACCInput(cost_of_equity_model="ff7")
