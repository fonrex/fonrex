from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from models import Asset, FinancialStatement, FundamentalsHighlights
from schemas.dcf import WACCInput
from valuation.dcf_service import DCFService


class MockDBService:
    def __init__(self, session):
        self.session = session

    def get_session(self):
        return self.session


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def dcf_service(mock_session):
    return DCFService(MockDBService(mock_session))


def test_compute_wacc_sources_calculated_and_fred(dcf_service):
    """Test WACC priority: calculated Kd and fred Rf."""
    highlights = FundamentalsHighlights(
        beta=Decimal("1.0"),
        market_cap=Decimal("1000000"),
        actual_cost_of_debt=Decimal("0.065"),
        cost_of_debt_source="calculated"
    )
    statements = [
        FinancialStatement(
            interest_expense=Decimal("-1000"),  # Ignored because 'calculated' is used
            total_debt=Decimal("50000"),
            total_equity=Decimal("100000"),
            ebit=Decimal("10000"),
            tax_provision=Decimal("2500")
        )
    ]

    res = dcf_service._compute_wacc(
        highlights, statements, None, 
        rf_fred=Decimal("0.042"), rf_source="fred_live"
    )

    assert res.cost_of_debt == Decimal("0.065")
    assert res.cost_of_debt_source == "calculated"
    assert res.risk_free_rate_source == "fred_live"


def test_compute_wacc_sources_overrides(dcf_service):
    """Test WACC priority: client overrides."""
    highlights = FundamentalsHighlights(
        beta=Decimal("1.0"),
        market_cap=Decimal("1000000"),
        actual_cost_of_debt=Decimal("0.065"),
        cost_of_debt_source="calculated"
    )
    statements = [
        FinancialStatement(
            interest_expense=Decimal("-1000"),
            total_debt=Decimal("50000"),
            total_equity=Decimal("100000"),
            ebit=Decimal("10000"),
            tax_provision=Decimal("2500")
        )
    ]
    params = WACCInput(
        risk_free_rate=Decimal("0.03"),
        cost_of_debt_override=Decimal("0.08")
    )

    res = dcf_service._compute_wacc(
        highlights, statements, params, 
        rf_fred=Decimal("0.042"), rf_source="fred_live"
    )

    assert res.cost_of_debt == Decimal("0.0800")
    assert res.cost_of_debt_source == "client_override"
    assert res.risk_free_rate_source == "client_override"


def test_compute_wacc_sources_fallback(dcf_service):
    """Test WACC priority: sector estimate and env fallback."""
    highlights = FundamentalsHighlights(
        beta=Decimal("1.0"),
        market_cap=Decimal("1000000"),
        actual_cost_of_debt=None,
        cost_of_debt_source=None
    )
    statements = [
        FinancialStatement(
            interest_expense=Decimal("-5000"),
            total_debt=Decimal("100000"), # 5k / 100k = 5%
            total_equity=Decimal("100000"),
            ebit=Decimal("10000"),
            tax_provision=Decimal("2500")
        )
    ]

    res = dcf_service._compute_wacc(
        highlights, statements, None, 
        rf_fred=None, rf_source=None
    )

    assert res.cost_of_debt == Decimal("0.0500")
    assert res.cost_of_debt_source == "sector_estimate"
    assert res.risk_free_rate_source == "env_fallback"
