from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from financials.enrichment.yfinance_enricher import YFinanceEnricher
from models import FinancialStatement, FundamentalsHighlights


class MockDBService:
    def __init__(self, session):
        self.session = session

    def get_session(self):
        return self.session


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def enricher(mock_session):
    return YFinanceEnricher(MockDBService(mock_session))


def test_solvency_ratios_calculation(enricher, mock_session):
    """Test the calculation of the 4 solvency ratios."""
    stmt = FinancialStatement(
        asset_id=1,
        period_type="annual",
        total_debt=Decimal("50000"),
        total_equity=Decimal("100000"),
        total_assets=Decimal("200000"),
        cash_and_equivalents=Decimal("10000"),
        ebitda=Decimal("20000"),
        interest_expense=Decimal("-5000"),
        operating_income=Decimal("15000"),
    )
    hl = FundamentalsHighlights(asset_id=1)

    # Setup mocks
    mock_query = mock_session.query.return_value
    mock_filter_stmt = mock_query.filter_by.return_value
    mock_order = mock_filter_stmt.order_by.return_value
    mock_limit = mock_order.limit.return_value
    mock_limit.all.return_value = [stmt]
    
    # Second query for highlights
    mock_filter_hl = mock_session.query.return_value.filter_by.return_value
    mock_filter_hl.first.return_value = hl

    enricher._fetch_solvency_ratios(1)

    # Assertions
    assert hl.debt_to_equity_ratio == Decimal("0.5")  # 50k / 100k
    assert hl.debt_to_assets_ratio == Decimal("0.25") # 50k / 200k
    assert hl.net_debt_to_ebitda == Decimal("2.0")    # (50k - 10k) / 20k
    assert hl.interest_coverage_ratio == Decimal("3.0") # 15k / 5k

    # Test that cost of debt is updated via weighted average
    # Only 1 statement, so weight is 1.0. cost = 5k / 50k = 10%
    assert hl.actual_cost_of_debt.quantize(Decimal("0.01")) == Decimal("0.10")
    assert hl.cost_of_debt_source == "calculated"


def test_weighted_average_cost_of_debt_multiple(enricher):
    """Test weighted average calculation over 3 years."""
    s1 = FinancialStatement(interest_expense=Decimal("-5000"), total_debt=Decimal("100000")) # 5%
    s2 = FinancialStatement(interest_expense=Decimal("-8000"), total_debt=Decimal("100000")) # 8%
    s3 = FinancialStatement(interest_expense=Decimal("-10000"), total_debt=Decimal("100000")) # 10%

    cost, source = enricher._weighted_average_cost_of_debt([s1, s2, s3])
    
    # 0.5 * 5% + 0.3 * 8% + 0.2 * 10% = 2.5% + 2.4% + 2.0% = 6.9%
    assert cost.quantize(Decimal("0.001")) == Decimal("0.069")
    assert source == "calculated"


def test_weighted_average_cost_of_debt_clamping(enricher):
    """Test cost of debt clamping between 0.5% and 25%."""
    # Too high: 50k interest on 100k debt = 50%
    s_high = FinancialStatement(interest_expense=Decimal("-50000"), total_debt=Decimal("100000"))
    cost, source = enricher._weighted_average_cost_of_debt([s_high])
    assert cost == Decimal("0.25")
    assert source == "calculated"

    # Too low: 10 interest on 100k debt = 0.01%
    s_low = FinancialStatement(interest_expense=Decimal("-10"), total_debt=Decimal("100000"))
    cost_low, source_low = enricher._weighted_average_cost_of_debt([s_low])
    assert cost_low == Decimal("0.005")
    assert source_low == "calculated"


def test_weighted_average_cost_of_debt_fallback(enricher):
    """Test fallback when no valid debt data."""
    # No debt
    s_no_debt = FinancialStatement(interest_expense=Decimal("-5000"), total_debt=Decimal("0"))
    cost, source = enricher._weighted_average_cost_of_debt([s_no_debt])
    assert cost is None
    assert source == "sector_estimate"

    # Empty list
    cost2, source2 = enricher._weighted_average_cost_of_debt([])
    assert cost2 is None
    assert source2 == "sector_estimate"
