# -*- coding: utf-8 -*-
"""
Unit tests for FinancialsFormatter.
"""

from datetime import date
from decimal import Decimal

from financials.formatter import FinancialsFormatter


class MockPydanticModel:
    def __init__(self, data):
        self.data = data

    def model_dump(self):
        return self.data


class MockPydanticModelLegacy:
    def __init__(self, data):
        self.data = data

    def dict(self):
        return self.data


def test_safe_str():
    assert FinancialsFormatter._safe_str(None) is None
    assert FinancialsFormatter._safe_str(12) == "12"
    assert FinancialsFormatter._safe_str(12.34) == "12.34"
    assert FinancialsFormatter._safe_str(Decimal("120.50")) == "120.5"
    assert FinancialsFormatter._safe_str("test") == "test"


def test_to_eodhd_basic():
    results = {
        "asset_profile": {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "quote_type": "equity",
        },
        "highlights": {
            "market_cap": Decimal("3000000000000"),
            "dividend_rate": Decimal("0.96"),
        },
        "YahooFinance": {
            "longName": "Apple Inc.",
            "symbol": "AAPL",
            "holders": {
                "institutions": [
                    {
                        "Holder": "Vanguard Group",
                        "Date Reported": "2026-03-31",
                        "% Out": 0.08,
                        "Shares": 1200000000,
                        "Change": 1000000,
                        "Value": 240000000000,
                    }
                ],
                "funds": [],
            },
        },
        "raw_providers": {"YahooFinance": "https://finance.yahoo.com/quote/AAPL"},
    }

    formatted = FinancialsFormatter.to_eodhd(results)

    assert formatted["General"]["Code"] == "AAPL"
    assert formatted["General"]["Name"] == "Apple Inc."
    assert formatted["Highlights"]["MarketCapitalization"] == Decimal("3000000000000")
    assert formatted["Highlights"]["MarketCapitalizationMln"] == 3000000.0
    assert formatted["General"]["LogoURL"] == "/static/logos/UNKNOWN/AAPL.webp"
    assert formatted["General"]["Sector"] is None
    assert formatted["General"]["Industry"] is None

    assert formatted["Holders"]["Institutions"]["0"]["name"] == "Vanguard Group"


def test_to_eodhd_pydantic_handling():
    results = {
        "asset_profile": {
            "ticker": "MSFT",
            "name": "Microsoft Corp",
            "exchange": "NASDAQ",
            "quote_type": "equity",
            "logo_path": "/static/logos/NASDAQ/MSFT.webp",
        },
        "highlights": MockPydanticModelLegacy(
            {"market_cap": 2500000000000, "pe_ratio": 35.5, "beta": 1.15}
        ),
        "YahooFinance": MockPydanticModel({"symbol": "MSFT", "heldPercentInsiders": 0.01}),
    }

    formatted = FinancialsFormatter.to_eodhd(results)
    assert formatted["General"]["Code"] == "MSFT"
    assert formatted["General"]["LogoURL"] == "/static/logos/NASDAQ/MSFT.webp"
    assert formatted["Highlights"]["MarketCapitalization"] == 2500000000000
    assert formatted["Valuation"]["TrailingPE"] == 35.5
    assert formatted["Technicals"]["Beta"] == 1.15
    assert formatted["SharesStats"]["PercentInsiders"] == 0.01


def test_build_providers():
    results = {
        "raw_providers": {
            "YahooFinance": "http://yf",
            "ZoneBourse": "http://zb",
            "Boursorama": "http://br",
        },
        "YahooFinance": {"ticker": "AAPL", "isin": "US0378331005", "name": "Apple Inc."},
        "ZoneBourse": {"error": "Timeout"},
    }

    # Boursorama is absent from results, so it should fallback to no_data
    providers = FinancialsFormatter._build_providers(results)
    assert providers["YahooFinance"]["status"] == "ok"
    assert providers["YahooFinance"]["ticker"] == "AAPL"
    assert providers["YahooFinance"]["isin"] == "US0378331005"
    assert providers["ZoneBourse"]["status"] == "error"
    assert providers["ZoneBourse"]["error"] == "Timeout"
    assert providers["Boursorama"]["status"] == "no_data"


def test_build_insider_transactions():
    # US SEC Edgar path
    results_sec = {
        "SECEdgar": [
            {
                "date": "2026-05-10",
                "ownerName": "Cook Tim",
                "shares": 50000,
                "transactionCode": "S",
                "transactionAmount": -9000000,
                "transactionPrice": 180.0,
                "description": "Sale",
            }
        ]
    }
    txns_sec = FinancialsFormatter._build_insider_transactions(results_sec)
    assert txns_sec["0"]["ownerName"] == "Cook Tim"
    assert txns_sec["0"]["transactionCode"] == "S"

    # International WSJ path
    results_wsj = {
        "wallstreetjournal": {
            "insider_transactions": {
                "Transactions": [
                    {
                        "date": "2026-05-11",
                        "ownerName": "Lourd Jean",
                        "shares": 1000,
                        "description": "Buy",
                    }
                ]
            }
        }
    }
    txns_wsj = FinancialsFormatter._build_insider_transactions(results_wsj)
    assert txns_wsj["0"]["ownerName"] == "Lourd Jean"
    assert txns_wsj["0"]["transactionCode"] is None

    # WSJ with Pydantic object
    results_wsj_pydantic = {
        "wallstreetjournal": MockPydanticModel(
            {
                "insider_transactions": {
                    "Transactions": [
                        {
                            "date": "2026-05-12",
                            "ownerName": "Lourd Jean Pydantic",
                            "shares": 2000,
                            "description": "Buy",
                        }
                    ]
                }
            }
        )
    }
    txns_wsj_pyd = FinancialsFormatter._build_insider_transactions(results_wsj_pydantic)
    assert txns_wsj_pyd["0"]["ownerName"] == "Lourd Jean Pydantic"


def test_build_esg_scores():
    results = {
        "esg_scores": {
            "rating_date": date(2026, 1, 1),
            "total_esg": 78.5,
            "environment_score": 80.0,
            "social_score": 75.0,
            "governance_score": 81.0,
            "controversy_level": 2,
            "adult": False,
            "alcoholic": False,
            "animal_testing": True,
            "gambling": False,
            "tobacco": False,
        },
        "boursorama": {
            "esg_score": 79.0,
            "esg_controversy": 3,
            "esg_co2": "A",
            "esg_positive_impact": "high",
            "esg_negative_impact": "low",
        },
    }

    esg = FinancialsFormatter._build_esg_scores(results["esg_scores"], results)
    assert esg["RatingDate"] == "2026-01-01"
    assert esg["TotalEsg"] == 78.5
    assert esg["EnvironmentScore"] == 80.0
    assert esg["ActivitiesInvolved"]["AnimalTesting"] is True
    assert esg["CO2_Emissions"] == "A"

    # Fallback to boursorama
    results_fallback = {"boursorama": MockPydanticModel({"esg_score": 79.0, "esg_controversy": 3})}
    esg_fb = FinancialsFormatter._build_esg_scores(None, results_fallback)
    assert esg_fb["TotalEsg"] == 79.0
    assert esg_fb["ControversyLevel"] == 3


def test_build_earnings():
    history = [
        {
            "period_end": date(2026, 3, 31),
            "period": "Q1 2026",
            "eps_actual": 1.50,
            "eps_estimate": 1.45,
            "surprise": 0.05,
            "surprise_pct": 3.45,
        }
    ]
    trend = [
        {
            "period": "+1y",
            "eps_avg": 6.50,
            "eps_low": 6.20,
            "eps_high": 6.80,
            "eps_nb_analysts": 15,
            "eps_growth": 0.12,
            "revenue_avg": 100000,
            "revenue_low": 98000,
            "revenue_high": 102000,
            "revenue_nb_analysts": 15,
            "revenue_growth": 0.08,
        }
    ]

    earnings = FinancialsFormatter._build_earnings(history, trend)
    assert earnings["History"]["0"]["reportDate"] == "2026-03-31"
    assert earnings["History"]["0"]["epsActual"] == 1.50
    assert earnings["Trend"]["0"]["period"] == "+1y"
    assert earnings["Trend"]["0"]["earningsEstimateAvg"] == 6.50
    assert earnings["Trend"]["0"]["revenueEstimateGrowth"] == 0.08


def test_format_financials():
    statements = [
        {
            "statement_type": "income",
            "period_type": "annual",
            "period_end": date(2025, 12, 31),
            "revenue": Decimal("100000000"),
            "net_income": Decimal("20000000"),
        },
        {
            "statement_type": "balance",
            "period_type": "quarterly",
            "period_end": date(2026, 3, 31),
            "total_assets": Decimal("50000000"),
        },
    ]

    financials = FinancialsFormatter._format_financials(statements)
    assert financials["Income_Statement"]["yearly"]["2025-12-31"]["revenue"] == "100000000"
    assert financials["Income_Statement"]["yearly"]["2025-12-31"]["net_income"] == "20000000"
    assert financials["Balance_Sheet"]["quarterly"]["2026-03-31"]["total_assets"] == "50000000"


def test_build_etf_data():
    results = {
        "asset_profile": {"quote_type": "ETF"},
        "etf_details": {
            "inception_date": date(2020, 5, 1),
            "net_expense_ratio": 0.0007,
            "total_net_assets": 500000000,
            "alloc_cash": 0.01,
            "alloc_stock_us": 0.99,
            "alloc_bond": 0.0,
        },
        "etf_holdings": [
            {
                "holding_ticker": "AAPL",
                "holding_name": "Apple Inc.",
                "sector": "Technology",
                "country": "USA",
                "weight": 0.075,
            }
        ],
    }

    formatted = FinancialsFormatter.to_eodhd(results)
    assert "ETF_Data" in formatted
    assert formatted["ETF_Data"]["Inception_Date"] == "2020-05-01"
    assert formatted["ETF_Data"]["Net_Expense_Ratio"] == 0.0007
    assert formatted["ETF_Data"]["Top_10_Holdings"]["0"]["Symbol"] == "AAPL"
    assert formatted["ETF_Data"]["Top_10_Holdings"]["0"]["Assets_%"] == 0.075
