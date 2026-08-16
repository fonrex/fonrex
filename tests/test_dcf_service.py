# -*- coding: utf-8 -*-
"""
Unit tests for the DCF Valuation service.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from models import Asset, EarningsTrend, FinancialStatement, FundamentalsHighlights
from schemas.dcf import WACCInput
from valuation.dcf_service import DCFService


class MockDBService:
    def __init__(self, session):
        self.session = session

    def get_session(self):
        return self.session


@pytest.fixture
def mock_session():
    """Mock for the SQLAlchemy session."""
    session = MagicMock()
    return session


@pytest.fixture
def dcf_service(mock_session):
    db_service = MockDBService(mock_session)
    return DCFService(db_service)


def test_wacc_calculation(dcf_service):
    """Verifies the nominal WACC calculation and user overrides."""
    highlights = FundamentalsHighlights(
        beta=Decimal("1.2"),
        market_cap=Decimal("100000000"),  # 100M
    )
    statements = [
        FinancialStatement(
            interest_expense=Decimal("2000000"),  # 2M
            ebit=Decimal("10000000"),  # 10M
            tax_provision=Decimal("2500000"),  # 2.5M (tax rate 25%)
            total_debt=Decimal("40000000"),  # 40M
            total_equity=Decimal("60000000"),
        )
    ]

    # Without overrides
    res = dcf_service._compute_wacc(highlights, statements, None)

    # Ke = Rf + Beta * ERP = 4% + 1.2 * 5.5% = 10.6%
    # Kd = Interest / Debt = 2M / 40M = 5%
    # Tax Rate = 2.5M / 10M = 25%
    # We = 100M / 140M = 71.428%
    # Wd = 40M / 140M = 28.571%
    # WACC = We * Ke + Wd * Kd * (1 - t) = 0.71428 * 0.106 + 0.28571 * 0.05 * 0.75 = 7.5714% + 1.0714% = 8.64%
    assert res.wacc == Decimal("0.0864")
    assert res.cost_of_equity == Decimal("0.1060")
    assert res.cost_of_debt == Decimal("0.0500")
    assert res.tax_rate == Decimal("0.2500")
    assert res.weight_equity.quantize(Decimal("0.0001")) == Decimal("0.7143")
    assert res.weight_debt.quantize(Decimal("0.0001")) == Decimal("0.2857")

    # With user overrides
    params = WACCInput(
        risk_free_rate=Decimal("0.05"),
        equity_risk_premium=Decimal("0.06"),
        beta_override=Decimal("1.5"),
        cost_of_debt_override=Decimal("0.06"),
        tax_rate_override=Decimal("0.30"),
    )
    res_override = dcf_service._compute_wacc(highlights, statements, params)
    # Ke = 5% + 1.5 * 6% = 14%
    # Kd = 6%
    # Tax Rate = 30%
    # We = 100M / 140M = 71.428%
    # Wd = 40M / 140M = 28.571%
    # WACC = 0.71428 * 0.14 + 0.28571 * 0.06 * 0.70 = 10% + 1.2% = 11.2%
    assert res_override.wacc == Decimal("0.1120")
    assert res_override.cost_of_equity == Decimal("0.1400")
    assert res_override.cost_of_debt == Decimal("0.0600")
    assert res_override.tax_rate == Decimal("0.3000")


def test_wacc_zero_debt(dcf_service):
    """Verifies that WACC equals Ke when there is no debt."""
    highlights = FundamentalsHighlights(beta=Decimal("1.0"), market_cap=Decimal("100000000"))
    statements = [
        FinancialStatement(
            interest_expense=Decimal("0"),
            ebit=Decimal("10000000"),
            tax_provision=Decimal("2500000"),
            total_debt=Decimal("0"),
            total_equity=Decimal("100000000"),
        )
    ]
    res = dcf_service._compute_wacc(highlights, statements, None)
    # Ke = 4% + 1.0 * 5.5% = 9.5%
    # WACC = Ke = 9.5%
    assert res.wacc == Decimal("0.0950")
    assert res.weight_equity == Decimal("1.0000")
    assert res.weight_debt == Decimal("0.0000")


def test_wacc_clamping(dcf_service):
    """Verifies that WACC is clamped between 5% and 20%."""
    # Very low case (computed WACC ~3.5%)
    highlights = FundamentalsHighlights(beta=Decimal("0.1"), market_cap=Decimal("100000000"))
    statements = [FinancialStatement(total_debt=Decimal("0"))]
    res_low = dcf_service._compute_wacc(
        highlights,
        statements,
        WACCInput(risk_free_rate=Decimal("0.01"), equity_risk_premium=Decimal("0.01")),
    )
    assert res_low.wacc == Decimal("0.0500")  # clamped to 5%

    # Very high case (computed WACC ~25%)
    res_high = dcf_service._compute_wacc(
        highlights,
        statements,
        WACCInput(
            risk_free_rate=Decimal("0.15"),
            equity_risk_premium=Decimal("0.10"),
            beta_override=Decimal("2.5"),
        ),
    )
    assert res_high.wacc == Decimal("0.2000")  # clamped to 20%


def test_dcf_fcf_basic(dcf_service):
    """Verifies the nominal FCF DCF calculation."""
    statements = [
        FinancialStatement(
            free_cashflow=Decimal("10000000"),  # 10M base FCF
            total_debt=Decimal("20000000"),  # 20M debt
            cash_and_equivalents=Decimal("10000000"),  # 10M cash
            revenue=Decimal("100000000"),
        ),
        FinancialStatement(free_cashflow=Decimal("9000000"), revenue=Decimal("90000000")),
    ]

    # WACC = 10%
    # 5-year projections at 5% growth
    # Base FCF = (10M + 9M) / 2 = 9.5M
    # Projected FCFs:
    # Y1 : 9.5M * 1.05 = 9.975M
    # Y2 : 10.47375M
    # Y3 : 10.9974375M
    # Y4 : 11.54730938M
    # Y5 : 12.12467484M
    # PVs (10% discount) :
    # PV1 = 9.068M
    # PV2 = 8.656M
    # PV3 = 8.263M
    # PV4 = 7.887M
    # PV5 = 7.528M
    # Sum PVs = 41.402M
    # TV = 12.12467484M * 1.025 / (0.10 - 0.025) = 12.427791711M / 0.075 = 165.703889M
    # PV(TV) = 165.703889M / 1.61051 = 102.889078M
    # EV = Sum PVs + PV(TV) = 144.29M
    # Net Debt = 20M - 10M = 10M
    # Equity Value = 144.29M - 10M = 134.29M
    # Shares = 10,000,000 (10M)
    # Intrinsic Value = 134.29M / 10M = 13.43
    res = dcf_service._dcf_fcf(
        statements=statements,
        wacc=Decimal("0.10"),
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.025"),
        growth_override=Decimal("0.05"),
    )

    assert res.intrinsic_value_per_share > 0
    assert abs(res.intrinsic_value_per_share - Decimal("13.43")) < Decimal("0.05")


def test_dcf_eps_basic(dcf_service):
    """Verifies the nominal EPS DCF calculation."""
    highlights = FundamentalsHighlights(eps_trailing=Decimal("2.0"), pe_ratio=Decimal("15.0"))
    statements = [FinancialStatement(eps_diluted=Decimal("2.0"))]
    trend_dict = {}

    res = dcf_service._dcf_eps(
        statements=statements,
        trend_dict=trend_dict,
        wacc=Decimal("0.10"),
        cost_of_equity=Decimal("0.10"),
        highlights=highlights,
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.025"),
        growth_override=Decimal("0.05"),
    )
    assert res.intrinsic_value_per_share > 0
    assert len(res.projected_values) == 5


def test_dcf_ddm_no_dividend(dcf_service):
    """Verifies that a ValueError is raised when no dividend is available for DDM."""
    highlights = FundamentalsHighlights(dividend_rate=Decimal("0"))
    statements = [FinancialStatement(dividends_paid=Decimal("0"))]

    with pytest.raises(ValueError) as exc:
        dcf_service._dcf_ddm(
            highlights=highlights,
            statements=statements,
            cost_of_equity=Decimal("0.10"),
            projection_years=5,
            terminal_growth=Decimal("0.025"),
            growth_override=None,
        )
    assert "dividend" in str(exc.value).lower()


def test_dcf_ddm_basic(dcf_service):
    """Verifies the nominal DDM calculation with a dividend."""
    highlights = FundamentalsHighlights(dividend_rate=Decimal("1.5"))
    statements = [FinancialStatement(dividends_paid=Decimal("-15000000"))]

    res = dcf_service._dcf_ddm(
        highlights=highlights,
        statements=statements,
        cost_of_equity=Decimal("0.10"),
        projection_years=5,
        terminal_growth=Decimal("0.025"),
        growth_override=Decimal("0.04"),
    )
    assert res.intrinsic_value_per_share > 0
    assert len(res.projected_values) == 5


def test_terminal_growth_exceeds_wacc(dcf_service):
    """Verifies the guard: terminal_growth must be < WACC."""
    statements = [
        FinancialStatement(
            free_cashflow=Decimal("10000000"),
            total_debt=Decimal("0"),
            cash_and_equivalents=Decimal("0"),
        )
    ]

    # terminal_growth (4.9%) >= WACC (5%) - 0.5% (= 4.5%)
    # so terminal_growth must be clamped to WACC - 0.5% = 4.5%
    # expected: terminal growth clamped to 4.5%
    res = dcf_service._dcf_fcf(
        statements=statements,
        wacc=Decimal("0.05"),
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.049"),  # nearly equal to wacc
        growth_override=Decimal("0.03"),
    )
    # Verify there is a warning about clamping
    assert any("clamped" in w.lower() or "terminal" in w.lower() for w in res.warnings)


def test_division_by_zero_protection(dcf_service):
    """Verifies that the service does not crash on zero-value inputs (shares, debt)."""
    highlights = FundamentalsHighlights(beta=Decimal("1.0"), market_cap=Decimal("0"))
    statements = [
        FinancialStatement(
            interest_expense=Decimal("0"),
            ebit=Decimal("0"),
            tax_provision=Decimal("0"),
            total_debt=Decimal("0"),
            total_equity=Decimal("0"),
        )
    ]
    # Must not crash (zero divisions handled in compute_wacc)
    wacc_res = dcf_service._compute_wacc(highlights, statements, None)
    # Ke = Rf + Beta * ERP = 4% + 1.0 * 5.5% = 9.5%
    # WACC = Ke = 9.5%
    assert wacc_res.wacc == Decimal("0.0950")


def test_sensitivity_matrix_shape(mock_session):
    """Verifies the shape of the generated sensitivity matrix."""
    db_service = MockDBService(mock_session)
    service = DCFService(db_service)

    # Mock DB queries
    asset = Asset(id=1, ticker="AAPL", currency="USD")
    highlights = FundamentalsHighlights(
        asset_id=1,
        market_cap=Decimal("100000000"),
        shares_outstanding=10000000,
        beta=Decimal("1.0"),
        week_52_high=Decimal("160.0"),
        week_52_low=Decimal("140.0"),
    )
    statement = FinancialStatement(
        asset_id=1,
        free_cashflow=Decimal("10000000"),
        total_debt=Decimal("0"),
        cash_and_equivalents=Decimal("0"),
    )

    mock_result_asset = MagicMock()
    mock_result_asset.scalars.return_value.first.return_value = asset

    mock_result_hl = MagicMock()
    mock_result_hl.scalars.return_value.first.return_value = highlights

    mock_result_stmt = MagicMock()
    mock_result_stmt.scalars.return_value.all.return_value = [statement]

    mock_result_price = MagicMock()
    mock_result_price.scalar.return_value = Decimal("150.0")

    mock_session.execute.side_effect = [
        mock_result_asset,
        mock_result_hl,
        mock_result_stmt,
        mock_result_price,
    ]

    wacc_range = [Decimal("0.08"), Decimal("0.10"), Decimal("0.12")]
    growth_range = [Decimal("0.02"), Decimal("0.03")]

    res = service.compute_sensitivity("AAPL", "fcf", wacc_range, growth_range)

    assert res.ticker == "AAPL"
    assert res.model == "fcf"
    assert len(res.matrix) == 3  # 3 WACCs
    assert len(res.matrix[0]) == 2  # 2 growth rates
    assert res.matrix[0][0].wacc == Decimal("0.08")
    assert res.matrix[0][0].terminal_growth == Decimal("0.02")


def test_dcf_fcf_fallback_ocf_capex(dcf_service):
    """Test _dcf_fcf fallback when free_cashflow is absent using ocf and capex."""
    statements = [
        FinancialStatement(
            free_cashflow=None,
            operating_cashflow=Decimal("15000000"),
            capex=Decimal("-3000000"),  # capex usually negative
            total_debt=Decimal("0"),
            cash_and_equivalents=Decimal("0"),
            revenue=Decimal("100000000"),
        )
    ]
    # Base FCF should be 15M - 3M = 12M
    res = dcf_service._dcf_fcf(
        statements=statements,
        wacc=Decimal("0.10"),
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=Decimal("0.05"),
    )
    assert res.intrinsic_value_per_share > 0
    assert any("computed via Operating Cash Flow" in w for w in res.warnings)


def test_dcf_fcf_fallback_revenue_cagr(dcf_service):
    """Test _dcf_fcf fallback to revenue CAGR when FCF CAGR is not computable."""
    statements = [
        FinancialStatement(
            free_cashflow=Decimal("10000000"),
            total_debt=Decimal("0"),
            cash_and_equivalents=Decimal("0"),
            revenue=Decimal("121000000"),
        ),
        FinancialStatement(
            free_cashflow=None,  # break CAGR path for FCF
            revenue=Decimal("100000000"),
        ),
    ]
    # Revenue CAGR is (121M/100M)^(1/1) - 1 = 21%, clamped to 20%
    res = dcf_service._dcf_fcf(
        statements=statements,
        wacc=Decimal("0.25"),  # higher wacc so tv doesn't diverge
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=None,
    )
    assert res.intrinsic_value_per_share > 0
    assert any("using Revenue CAGR" in w for w in res.warnings)


def test_dcf_fcf_negative_base_warning(dcf_service):
    """Test _dcf_fcf warns when base FCF is negative."""
    statements = [
        FinancialStatement(
            free_cashflow=Decimal("-5000000"),
            total_debt=Decimal("0"),
            cash_and_equivalents=Decimal("0"),
            revenue=Decimal("100000000"),
        )
    ]
    res = dcf_service._dcf_fcf(
        statements=statements,
        wacc=Decimal("0.10"),
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=Decimal("0.02"),
    )
    assert any("negative" in w.lower() for w in res.warnings)


def test_dcf_fcf_negative_intrinsic_value_clamped(dcf_service):
    """Test _dcf_fcf clamps negative intrinsic value to 0."""
    statements = [
        FinancialStatement(
            free_cashflow=Decimal("-10000000"),
            total_debt=Decimal("500000000"),  # huge debt relative to cash/FCF
            cash_and_equivalents=Decimal("0"),
            revenue=Decimal("100000000"),
        )
    ]
    res = dcf_service._dcf_fcf(
        statements=statements,
        wacc=Decimal("0.10"),
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=Decimal("0.02"),
    )
    assert res.intrinsic_value_per_share == Decimal("0")
    assert any("negative. Set to 0" in w for w in res.warnings)


def test_dcf_eps_analyst_trend_0y(dcf_service):
    """Test _dcf_eps using analyst trend eps_growth (0y)."""
    highlights = FundamentalsHighlights(eps_trailing=Decimal("2.0"), pe_ratio=Decimal("15.0"))
    statements = [FinancialStatement(eps_diluted=Decimal("2.0"))]
    trend_0y = EarningsTrend()
    trend_0y.eps_growth = Decimal("0.15")
    trend_dict = {"0y": trend_0y}
    res = dcf_service._dcf_eps(
        statements=statements,
        trend_dict=trend_dict,
        wacc=Decimal("0.10"),
        cost_of_equity=Decimal("0.10"),
        highlights=highlights,
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=None,
    )
    assert res.intrinsic_value_per_share > 0


def test_dcf_eps_analyst_trend_1y(dcf_service):
    """Test _dcf_eps using analyst trend eps_growth (+1y)."""
    highlights = FundamentalsHighlights(eps_trailing=Decimal("2.0"), pe_ratio=Decimal("15.0"))
    statements = [FinancialStatement(eps_diluted=Decimal("2.0"))]
    trend_1y = EarningsTrend()
    trend_1y.eps_growth = Decimal("0.12")
    trend_dict = {"+1y": trend_1y}
    res = dcf_service._dcf_eps(
        statements=statements,
        trend_dict=trend_dict,
        wacc=Decimal("0.10"),
        cost_of_equity=Decimal("0.10"),
        highlights=highlights,
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=None,
    )
    assert res.intrinsic_value_per_share > 0


def test_dcf_eps_historical_cagr(dcf_service):
    """Test _dcf_eps fallback to historical EPS CAGR."""
    highlights = FundamentalsHighlights(eps_trailing=Decimal("2.0"), pe_ratio=Decimal("15.0"))
    statements = [
        FinancialStatement(eps_diluted=Decimal("2.42")),
        FinancialStatement(eps_diluted=Decimal("2.00")),
    ]
    # CAGR = (2.42/2.0)^(1/1) - 1 = 21%, clamped to 20%
    res = dcf_service._dcf_eps(
        statements=statements,
        trend_dict={},
        wacc=Decimal("0.10"),
        cost_of_equity=Decimal("0.10"),
        highlights=highlights,
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=None,
    )
    assert res.intrinsic_value_per_share > 0


def test_dcf_eps_pe_clamping(dcf_service):
    """Test _dcf_eps clamps PE ratio between 10 and 30."""
    highlights_low_pe = FundamentalsHighlights(eps_trailing=Decimal("2.0"), pe_ratio=Decimal("5.0"))
    highlights_high_pe = FundamentalsHighlights(
        eps_trailing=Decimal("2.0"), pe_ratio=Decimal("50.0")
    )
    statements = [FinancialStatement(eps_diluted=Decimal("2.0"))]

    # Should clamp low PE to 10
    res_low = dcf_service._dcf_eps(
        statements=statements,
        trend_dict={},
        wacc=Decimal("0.10"),
        cost_of_equity=Decimal("0.10"),
        highlights=highlights_low_pe,
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=Decimal("0.05"),
    )
    # Should clamp high PE to 30
    res_high = dcf_service._dcf_eps(
        statements=statements,
        trend_dict={},
        wacc=Decimal("0.10"),
        cost_of_equity=Decimal("0.10"),
        highlights=highlights_high_pe,
        shares_outstanding=10000000,
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=Decimal("0.05"),
    )
    assert res_high.intrinsic_value_per_share > res_low.intrinsic_value_per_share


def test_dcf_ddm_reconstruction(dcf_service):
    """Test _dcf_ddm reconstructing dividend from yield, PE, and EPS."""
    highlights = FundamentalsHighlights(
        dividend_rate=Decimal("0"),
        dividend_yield=Decimal("0.02"),
        pe_ratio=Decimal("15.0"),
        eps_trailing=Decimal("3.0"),
    )
    statements = [FinancialStatement(dividends_paid=Decimal("0"))]
    # reconstructed dividend = 0.02 * 15 * 3 = 0.90
    res = dcf_service._dcf_ddm(
        highlights=highlights,
        statements=statements,
        cost_of_equity=Decimal("0.10"),
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=Decimal("0.03"),
    )
    assert res.intrinsic_value_per_share > 0


def test_dcf_ddm_dividends_paid_fallback(dcf_service):
    """Test _dcf_ddm fallback using dividends_paid / shares."""
    highlights = FundamentalsHighlights(dividend_rate=Decimal("0"), shares_outstanding=10000000)
    statements = [
        FinancialStatement(dividends_paid=Decimal("-10000000"))
    ]  # negative dividends paid
    res = dcf_service._dcf_ddm(
        highlights=highlights,
        statements=statements,
        cost_of_equity=Decimal("0.10"),
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=Decimal("0.03"),
    )
    assert res.intrinsic_value_per_share > 0


def test_dcf_ddm_historical_cagr(dcf_service):
    """Test _dcf_ddm historical dividend CAGR."""
    highlights = FundamentalsHighlights(dividend_rate=Decimal("1.5"))
    statements = [
        FinancialStatement(dividends_paid=Decimal("-12100000")),
        FinancialStatement(dividends_paid=Decimal("-10000000")),
    ]
    # CAGR = (12.1M / 10.0M)^(1/1) - 1 = 21%, clamped to 15%
    res = dcf_service._dcf_ddm(
        highlights=highlights,
        statements=statements,
        cost_of_equity=Decimal("0.10"),
        projection_years=5,
        terminal_growth=Decimal("0.02"),
        growth_override=None,
    )
    assert res.intrinsic_value_per_share > 0


def test_wacc_invalid_tax_rate_clamping(dcf_service):
    """Test _compute_wacc clamps tax rate to 0.25 if outside [0, 1]."""
    highlights = FundamentalsHighlights(beta=Decimal("1.0"), market_cap=Decimal("100000000"))
    # tax rate = 15M / 10M = 1.5 (> 1)
    statements_high = [
        FinancialStatement(
            interest_expense=Decimal("0"),
            ebit=Decimal("10000000"),
            tax_provision=Decimal("15000000"),
            total_debt=Decimal("50000000"),
            total_equity=Decimal("50000000"),
        )
    ]
    res_high = dcf_service._compute_wacc(highlights, statements_high, None)
    assert res_high.tax_rate == Decimal("0.2500")

    # tax rate = -1M / 10M = -0.1 (< 0)
    statements_low = [
        FinancialStatement(
            interest_expense=Decimal("0"),
            ebit=Decimal("10000000"),
            tax_provision=Decimal("-1000000"),
            total_debt=Decimal("50000000"),
            total_equity=Decimal("50000000"),
        )
    ]
    res_low = dcf_service._compute_wacc(highlights, statements_low, None)
    assert res_low.tax_rate == Decimal("0.2500")


def test_wacc_zero_market_cap_fallback(dcf_service):
    """Test _compute_wacc falls back to total_equity or minimal value if market_cap <= 0."""
    highlights = FundamentalsHighlights(beta=Decimal("1.0"), market_cap=Decimal("0"))
    statements = [
        FinancialStatement(
            interest_expense=Decimal("0"),
            ebit=Decimal("10000000"),
            tax_provision=Decimal("2500000"),
            total_debt=Decimal("0"),
            total_equity=Decimal("50000000"),  # falls back to 50M
        )
    ]
    res = dcf_service._compute_wacc(highlights, statements, None)
    assert res.weight_equity == Decimal("1.0000")

    # Both market cap and total equity are 0/negative
    statements_zero_eq = [
        FinancialStatement(
            interest_expense=Decimal("0"),
            ebit=Decimal("10000000"),
            tax_provision=Decimal("2500000"),
            total_debt=Decimal("0"),
            total_equity=Decimal("0"),  # falls back to 1M
        )
    ]
    res_zero = dcf_service._compute_wacc(highlights, statements_zero_eq, None)
    assert res_zero.weight_equity == Decimal("1.0000")


def test_sensitivity_matrix_models(mock_session):
    """Test compute_sensitivity for eps and ddm models."""
    db_service = MockDBService(mock_session)
    service = DCFService(db_service)

    asset = Asset(id=1, ticker="AAPL", currency="USD")
    highlights = FundamentalsHighlights(
        asset_id=1,
        market_cap=Decimal("100000000"),
        shares_outstanding=10000000,
        beta=Decimal("1.0"),
        eps_trailing=Decimal("2.0"),
        pe_ratio=Decimal("15.0"),
        dividend_rate=Decimal("1.5"),
        week_52_high=Decimal("160.0"),
        week_52_low=Decimal("140.0"),
    )
    statement = FinancialStatement(
        asset_id=1,
        free_cashflow=Decimal("10000000"),
        total_debt=Decimal("0"),
        cash_and_equivalents=Decimal("0"),
        eps_diluted=Decimal("2.0"),
        dividends_paid=Decimal("-15000000"),
    )

    mock_result_asset = MagicMock()
    mock_result_asset.scalars.return_value.first.return_value = asset

    mock_result_hl = MagicMock()
    mock_result_hl.scalars.return_value.first.return_value = highlights

    mock_result_stmt = MagicMock()
    mock_result_stmt.scalars.return_value.all.return_value = [statement]

    mock_result_price = MagicMock()
    mock_result_price.scalar.return_value = Decimal("150.0")

    mock_session.execute.side_effect = [
        # For EPS test
        mock_result_asset,
        mock_result_hl,
        mock_result_stmt,
        mock_result_price,
        # For DDM test
        mock_result_asset,
        mock_result_hl,
        mock_result_stmt,
        mock_result_price,
    ]

    wacc_range = [Decimal("0.08"), Decimal("0.10")]
    growth_range = [Decimal("0.02")]

    # Test EPS Sensitivity
    res_eps = service.compute_sensitivity("AAPL", "eps", wacc_range, growth_range)
    assert res_eps.model == "eps"
    assert len(res_eps.matrix) == 2

    # Test DDM Sensitivity
    res_ddm = service.compute_sensitivity("AAPL", "ddm", wacc_range, growth_range)
    assert res_ddm.model == "ddm"
    assert len(res_ddm.matrix) == 2


def test_decimal_conversions_and_division(dcf_service):
    """Test internal helper functions _dec and _safe_div."""
    assert dcf_service._dec(None) == Decimal("0")
    assert dcf_service._dec(Decimal("10.5")) == Decimal("10.5")
    assert dcf_service._dec("5.2") == Decimal("5.2")

    assert dcf_service._safe_div(Decimal("10"), Decimal("0"), fallback=Decimal("9")) == Decimal("9")
    assert dcf_service._safe_div(Decimal("10"), Decimal("2")) == Decimal("5")
