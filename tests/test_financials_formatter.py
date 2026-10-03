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


# Yahoo payload of Apple on 4 October 2026 (the fields the document reads).
YAHOO_AAPL = {
    "symbol": "AAPL",
    "marketCap": 4869931925504,
    "trailingPE": 38.31114,
    "dividendYield": 0.32,  # percent
    "dividendRate": 1.08,
    "payoutRatio": 0.1204,
    "trailingEps": 8.71,
}


def test_yahoo_dividend_yield_is_rendered_as_a_ratio():
    """Yahoo publishes 0.32 for 0.32 %: copied as is, the document said 32 %."""
    formatted = FinancialsFormatter.to_eodhd({"YahooFinance": YAHOO_AAPL})

    assert formatted["Highlights"]["DividendYield"] == 0.0032
    assert formatted["SplitsDividends"]["ForwardAnnualDividendYield"] == 0.0032
    assert formatted["SplitsDividends"]["PayoutRatio"] == 0.1204


def test_each_figure_names_its_source():
    results = {
        "highlights": {
            "fetched_at": "2026-10-01T06:00:00+00:00",
            "pe_ratio": 37.0,
            "peg_ratio": 2.1,
            "market_cap": Decimal("4800000000000"),
        },
        "YahooFinance": YAHOO_AAPL,
        "Barrons": {"pe_ratio": 38.25, "eps": 8.72, "dividend_yield": 0.32},
    }

    formatted = FinancialsFormatter.to_eodhd(results)

    # The answer of this request first: a stored figure is an older answer.
    assert formatted["Highlights"]["PERatio"] == 38.31114
    assert formatted["Highlights"]["MarketCapitalization"] == 4869931925504
    # What Yahoo did not give this time comes from the stored figures, dated.
    assert formatted["Highlights"]["PEGRatio"] == 2.1
    assert formatted["Sources"]["Highlights"] == {
        "MarketCapitalization": "YahooFinance",
        "PERatio": "YahooFinance",
        "PEGRatio": "database (2026-10-01)",
        "DividendShare": "YahooFinance",
        "DividendYield": "YahooFinance",
        "EarningsShare": "YahooFinance",
        "DilutedEpsTTM": "YahooFinance",
    }
    assert formatted["Sources"]["Valuation"] == {"TrailingPE": "YahooFinance"}
    # A figure nobody gave has no source.
    assert formatted["Highlights"]["BookValue"] is None
    assert "BookValue" not in formatted["Sources"]["Highlights"]


def test_stored_figures_answer_when_yahoo_is_not_asked():
    """A listing without a verified Yahoo symbol still shows what the database holds."""
    results = {
        "highlights": {"pe_ratio": 37.0, "dividend_yield": Decimal("0.0032"), "beta": 1.1},
        "YahooFinance": {"error": "No Yahoo symbol quoted in CHF"},
    }

    formatted = FinancialsFormatter.to_eodhd(results)

    assert formatted["Highlights"]["PERatio"] == 37.0
    assert formatted["Highlights"]["DividendYield"] == Decimal("0.0032")
    assert formatted["Sources"]["Technicals"] == {"Beta": "database"}


def test_scraped_figures_fill_what_yahoo_does_not_give():
    """Without Yahoo, the trailing figures of the scraped pages are used, as ratios."""
    results = {
        "YahooFinance": {"error": "No Yahoo symbol quoted in EUR"},
        "GoogleFinance": {"pe_ratio": 25.21, "eps": 7.51, "revenue": 17830000000.0},
        "Barrons": {"error": "Provider timeout", "pe_ratio": 1.0},
        "Marketwatch": {"pe_ratio": 25.3, "eps": 7.5, "dividend_yield": 1.45},
        # Estimates for the current fiscal year: another quantity, never a fallback.
        "Boursorama": {"pe_ratio": 31.27, "eps": 6.89, "dividend_yield": 1.49},
        "ZoneBourse": {"pe_ratio": 25.5, "dividend_yield": 1.81, "revenue": 80945151250.0},
    }

    formatted = FinancialsFormatter.to_eodhd(results)

    assert formatted["Highlights"]["PERatio"] == 25.21
    assert formatted["Valuation"]["TrailingPE"] == 25.21
    assert formatted["Highlights"]["EarningsShare"] == 7.51
    # 1.45 displayed in percent on the page, 0.0145 in the document.
    assert formatted["Highlights"]["DividendYield"] == 0.0145
    assert formatted["Sources"]["Highlights"] == {
        "PERatio": "GoogleFinance",
        "EarningsShare": "GoogleFinance",
        "DividendYield": "Marketwatch",
    }
    # Revenue of the last quarter (GoogleFinance) is not the revenue of twelve months.
    assert formatted["Highlights"]["RevenueTTM"] is None


def test_zero_is_a_value_not_a_missing_figure():
    results = {
        "highlights": {"dividend_rate": 1.04, "beta": 1.1},
        "YahooFinance": {"dividendRate": 0, "beta": 0.0},
    }

    formatted = FinancialsFormatter.to_eodhd(results)

    assert formatted["Highlights"]["DividendShare"] == 0
    assert formatted["Technicals"]["Beta"] == 0.0
    assert formatted["Sources"]["Technicals"] == {"Beta": "YahooFinance"}


def test_unusable_values_are_skipped_not_rendered():
    results = {
        "highlights": {"pe_ratio": Decimal("NaN"), "market_cap": 0},
        "YahooFinance": {"dividendYield": "N/A", "trailingPE": float("inf"), "beta": float("nan")},
        "GoogleFinance": {"pe_ratio": float("inf")},
        "Marketwatch": {"pe_ratio": 25.3, "dividend_yield": True},
    }

    formatted = FinancialsFormatter.to_eodhd(results)

    assert formatted["Highlights"]["PERatio"] == 25.3
    assert formatted["Highlights"]["DividendYield"] is None
    assert formatted["Technicals"]["Beta"] is None
    assert formatted["Highlights"]["MarketCapitalization"] == 0
    assert formatted["Highlights"]["MarketCapitalizationMln"] == 0.0


def test_wall_street_journal_insiders_are_found_under_the_name_of_the_provider():
    results = {
        "wallStreetJournal": {
            "insider_transactions": {
                "Transactions": [{"date": "2026-05-11", "ownerName": "Lourd Jean", "shares": 1000}]
            }
        }
    }

    transactions = FinancialsFormatter.to_eodhd(results)["InsiderTransactions"]

    assert transactions["0"]["ownerName"] == "Lourd Jean"


def test_insider_transactions_of_the_sec_provider_are_rendered():
    """The provider returns one document holding ``transactions``, not a list."""
    results = {
        "SECEdgar": {
            "ticker": "AAPL",
            "cik": "0000320193",
            "transactions": [
                {
                    "filing_date": "2026-10-01",
                    "insider_name": "Newstead Jennifer",
                    "insider_title": "SVP, GC and Government Affairs",
                    "transaction_date": "2026-09-29",
                    "transaction_type": "Sell",
                    "transaction_code": "S",
                    "shares": 2399,
                    "price_per_share": 336.18,
                    "total_value": 806495.82,
                    "shares_owned_after": 41992,
                    "sec_filing_url": "https://www.sec.gov/Archives/edgar/data/320193/x-index.htm",
                }
            ],
        }
    }

    transactions = FinancialsFormatter.to_eodhd(results)["InsiderTransactions"]

    assert transactions == {
        "0": {
            "date": "2026-09-29",
            "ownerName": "Newstead Jennifer",
            "ownerTitle": "SVP, GC and Government Affairs",
            "shares": 2399,
            "transactionCode": "S",
            "transactionAmount": 806495.82,
            "transactionPrice": 336.18,
            "postTransactionAmount": 41992,
            "description": "Sell",
            "secLink": "https://www.sec.gov/Archives/edgar/data/320193/x-index.htm",
        }
    }


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
