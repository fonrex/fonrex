"""Unit tests for OpenBB Workspace adapters and formatters."""

from datetime import date, datetime
from decimal import Decimal

from integrations.openbb.adapters import (
    format_batch_quotes_table,
    format_candlestick_chart,
    format_dcf_compare_table,
    format_dcf_sensitivity_table,
    format_dcf_table,
    format_etf_details_table,
    format_fundamentals_deep_table,
    format_fundamentals_table,
    format_indicator_chart,
    format_macro_rates_metric,
    format_quote_metric,
    format_technical_chart_overlay,
    format_technical_multi_chart,
)


def test_format_quote_metric():
    quote_data = {
        "ticker": "AAPL",
        "price": Decimal("150.25"),
        "change": Decimal("1.50"),
        "change_pct": Decimal("1.01"),
        "volume": 55000000,
        "high": Decimal("151.00"),
        "low": Decimal("149.00"),
        "previous_close": Decimal("148.75"),
    }
    metrics = format_quote_metric(quote_data)
    assert isinstance(metrics, list)
    assert len(metrics) >= 1

    primary = metrics[0]
    assert primary["label"] == "AAPL Price"
    assert primary["value"] == 150.25
    assert primary["delta"] == 1.01

    labels = {m["label"] for m in metrics}
    assert "Change" in labels
    assert "Previous Close" in labels
    for m in metrics:
        assert "label" in m
        assert "value" in m
        assert "delta" in m


def test_format_macro_rates_metric():
    macro_data = {
        "risk_free_rate": {
            "series_id": "DGS10",
            "label": "10-Year Treasury Constant Maturity Rate",
            "value": Decimal("4.25"),
            "unit": "percent",
            "observation_date": date(2026, 9, 25),
        }
    }
    metrics = format_macro_rates_metric(macro_data)
    assert isinstance(metrics, list)
    assert len(metrics) == 1
    item = metrics[0]
    assert "DGS10" in item["label"] or "10-Year" in item["label"]
    assert item["value"] == "4.25%"
    assert item["delta"] is None


def test_format_candlestick_chart():
    records = [
        {
            "Date": "2026-09-22",
            "Open": 150.0,
            "High": 155.0,
            "Low": 149.0,
            "Close": 154.0,
            "Volume": 1000000,
        },
        {
            "Date": "2026-09-23",
            "Open": 154.0,
            "High": 156.0,
            "Low": 152.0,
            "Close": 153.0,
            "Volume": 1200000,
        },
    ]
    fig = format_candlestick_chart("AAPL", records)
    assert "data" in fig
    assert "layout" in fig
    assert isinstance(fig["data"], list)
    assert len(fig["data"]) == 1

    trace = fig["data"][0]
    assert trace["type"] == "candlestick"
    assert trace["name"] == "AAPL"
    assert trace["x"] == ["2026-09-22", "2026-09-23"]
    assert trace["open"] == [150.0, 154.0]
    assert trace["high"] == [155.0, 156.0]
    assert trace["low"] == [149.0, 152.0]
    assert trace["close"] == [154.0, 153.0]
    assert fig["layout"]["xaxis"]["rangeslider"]["visible"] is False


def test_format_indicator_chart():
    series_list = [
        {
            "name": "rsi",
            "values": [
                {"t": datetime(2026, 9, 22, 12, 0), "v": Decimal("45.2")},
                {"t": datetime(2026, 9, 23, 12, 0), "v": Decimal("52.8")},
            ],
        }
    ]
    fig = format_indicator_chart("AAPL", "rsi", series_list)
    assert "data" in fig
    assert "layout" in fig
    assert len(fig["data"]) == 1
    trace = fig["data"][0]
    assert trace["type"] == "scatter"
    assert trace["mode"] == "lines"
    assert trace["y"] == [45.2, 52.8]
    assert "RSI" in fig["layout"]["title"]


def test_format_technical_multi_chart():
    payload = {
        "ticker": "AAPL",
        "indicators": {
            "sma_20": {
                "series": [
                    {
                        "name": "sma_20",
                        "values": [{"t": "2026-09-22", "v": 151.0}],
                    }
                ]
            },
            "ema_50": {
                "series": [
                    {
                        "name": "ema_50",
                        "values": [{"t": "2026-09-22", "v": 148.5}],
                    }
                ]
            },
        },
    }
    fig = format_technical_multi_chart("AAPL", payload)
    assert "data" in fig
    assert len(fig["data"]) == 2
    names = {tr["name"] for tr in fig["data"]}
    assert "sma_20" in names
    assert "ema_50" in names


def test_format_technical_chart_overlay():
    chart_payload = {
        "ticker": "AAPL",
        "timestamps": ["2026-09-22", "2026-09-23"],
        "ohlcv": {
            "open": [150.0, 152.0],
            "high": [153.0, 155.0],
            "low": [149.0, 151.0],
            "close": [152.0, 154.0],
            "volume": [1000, 2000],
        },
        "indicators": {
            "sma_20": [151.0, 153.0],
        },
    }
    fig = format_technical_chart_overlay("AAPL", chart_payload)
    assert "data" in fig
    assert len(fig["data"]) == 2
    assert fig["data"][0]["type"] == "candlestick"
    assert fig["data"][1]["type"] == "scatter"


def test_format_fundamentals_table():
    eodhd_data = {
        "General": {
            "Code": "AAPL",
            "Name": "Apple Inc",
            "Sector": "Technology",
        },
        "Highlights": {
            "MarketCapitalization": 3000000000000,
            "PERatio": 28.5,
        },
        "Valuation": {
            "TrailingPE": 28.5,
            "ForwardPE": 25.2,
        },
    }
    rows = format_fundamentals_table(eodhd_data)
    assert isinstance(rows, list)
    assert len(rows) > 0
    for r in rows:
        assert "category" in r
        assert "metric" in r
        assert "value" in r

    categories = {r["category"] for r in rows}
    assert "General" in categories
    assert "Highlights" in categories
    assert "Valuation" in categories


def test_format_fundamentals_deep_table():
    deep_data = {
        "analyst_ratings": {
            "target_price": 200.0,
            "recommendation": "buy",
        },
        "meta": {"source": "yfinance"},
    }
    rows = format_fundamentals_deep_table(deep_data)
    assert isinstance(rows, list)
    assert len(rows) == 2
    fields = {r["field"] for r in rows}
    assert "target_price" in fields
    assert "recommendation" in fields


def test_format_dcf_table():
    dcf_data = {
        "ticker": "AAPL",
        "currency": "USD",
        "current_price": Decimal("150.00"),
        "consensus_value": Decimal("175.50"),
        "consensus_upside_pct": Decimal("17.00"),
        "models": {
            "fcf": {
                "model_name": "Free Cash Flow to Firm",
                "intrinsic_value_per_share": Decimal("180.00"),
                "upside_pct": Decimal("20.00"),
                "terminal_value": Decimal("500000000000"),
            }
        },
        "wacc": {
            "wacc": Decimal("0.0825"),
            "cost_of_equity": Decimal("0.0910"),
            "cost_of_debt": Decimal("0.0400"),
            "beta_used": Decimal("1.15"),
        },
    }
    rows = format_dcf_table(dcf_data)
    assert isinstance(rows, list)
    metrics = {r["metric"] for r in rows}
    assert "Current Price" in metrics
    assert "Consensus Fair Value" in metrics
    assert "Free Cash Flow to Firm — Intrinsic Value" in metrics


def test_format_dcf_compare_table():
    dcf_data = {
        "ticker": "AAPL",
        "currency": "USD",
        "current_price": 150.0,
        "models": {
            "fcf": {
                "model_name": "Free Cash Flow",
                "intrinsic_value_per_share": 180.0,
                "upside_pct": 20.0,
            },
            "eps": {
                "model_name": "EPS Growth",
                "intrinsic_value_per_share": 165.0,
                "upside_pct": 10.0,
            },
        },
    }
    rows = format_dcf_compare_table(dcf_data)
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["model_name"] == "Free Cash Flow"
    assert rows[0]["intrinsic_value"] == 180.0
    assert rows[1]["model_name"] == "EPS Growth"


def test_format_dcf_sensitivity_table():
    sensitivity_data = {
        "ticker": "AAPL",
        "model": "fcf",
        "matrix": [
            [
                {
                    "wacc": Decimal("0.08"),
                    "terminal_growth": Decimal("0.02"),
                    "intrinsic_value": Decimal("175.25"),
                    "upside_pct": Decimal("16.83"),
                },
                {
                    "wacc": Decimal("0.08"),
                    "terminal_growth": Decimal("0.03"),
                    "intrinsic_value": Decimal("190.50"),
                    "upside_pct": Decimal("27.00"),
                },
            ]
        ],
    }
    rows = format_dcf_sensitivity_table(sensitivity_data)
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["wacc"] == "8.0%"
    assert rows[0]["terminal_growth"] == "2.0%"
    assert rows[0]["intrinsic_value"] == 175.25
    assert rows[0]["upside_pct"] == 16.83


def test_format_batch_quotes_table():
    batch_dict = {
        "AAPL": {
            "data": {"close": 150.0, "change": 1.5, "change_pct": 1.01, "volume": 1000},
            "is_realtime": True,
            "source": "tradingview",
        },
        "MSFT": None,
    }
    rows = format_batch_quotes_table(batch_dict)
    assert isinstance(rows, list)
    assert len(rows) == 2
    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    assert aapl["price"] == 150.0
    assert aapl["is_realtime"] is True
    msft = next(r for r in rows if r["ticker"] == "MSFT")
    assert msft["price"] is None
    assert msft["source"] == "unavailable"


def test_format_etf_details_table():
    etf_data = {
        "isin": "IE00B4L5Y983",
        "name": "iShares Core MSCI World UCITS ETF",
        "net_expense_ratio": Decimal("0.0020"),
        "total_net_assets": Decimal("50000000000"),
        "domicile": "Ireland",
    }
    rows = format_etf_details_table(etf_data)
    assert isinstance(rows, list)
    assert len(rows) >= 4
    metrics = {r["metric"]: r["value"] for r in rows}
    assert metrics["ISIN"] == "IE00B4L5Y983"
    assert metrics["Domicile"] == "Ireland"
