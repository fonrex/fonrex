#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Module containing the FonRex API documentation.
"""


def get_api_documentation():
    """
    Returns the complete API documentation.

    Returns:
        dict: Complete API documentation
    """
    eod_doc = {
        "url": "/eod/{ticker}",
        "method": "GET",
        "parameters": {
            "period": {
                "description": "Period of data to retrieve",
                "required": True,
                "values": [
                    "1d",
                    "5d",
                    "1mo",
                    "3mo",
                    "6mo",
                    "1y",
                    "2y",
                    "5y",
                    "10y",
                    "ytd",
                    "max",
                    "daily",
                    "weekly",
                    "monthly",
                ],
            },
            "fmt": {
                "description": "Output format",
                "required": False,
                "values": ["json", "csv"],
                "default": "json",
            },
            "order": {
                "description": "Sort order",
                "required": False,
                "values": ["a", "d"],
                "default": "a",
            },
            "from": {
                "description": "Start date in YYYY-MM-DD format",
                "required": False,
                "format": "YYYY-MM-DD",
            },
            "to": {
                "description": "End date in YYYY-MM-DD format",
                "required": False,
                "format": "YYYY-MM-DD",
            },
        },
        "examples": [
            "/eod/TSLA?period=5d",
            "/eod/AAPL?period=1mo&fmt=csv",
            "/eod/MSFT?period=daily&from=2023-01-01&to=2023-01-31",
        ],
    }

    doc = {
        "service": "FonRex API",
        "description": "REST API to retrieve stock EOD (End of Day) data",
        "version": "1.0.0",
        "endpoints": {
            "eod": eod_doc,
            "health": {
                "url": "/health",
                "method": "GET",
                "description": "Check the API and cache status",
            },
            "cache_clear": {
                "url": "/cache/clear",
                "method": "POST",
                "description": "Clear the Redis cache",
            },
            "cache_clear_ticker": {
                "url": "/cache/clear/<ticker>",
                "method": "POST",
                "description": "Clear the Redis cache for a specific ticker",
                "parameters": {
                    "period": {
                        "description": "Specific period to clear (optional)",
                        "required": False,
                        "example": "1d, 5d, 1mo, etc.",
                    }
                },
                "examples": ["/cache/clear/TSLA", "/cache/clear/AAPL?period=5d"],
            },
            "cache_stats": {
                "url": "/cache/stats",
                "method": "GET",
                "description": "Get cache statistics",
            },
            "database_stats": {
                "url": "/database/stats",
                "method": "GET",
                "description": "Get database statistics",
            },
            "database_tickers": {
                "url": "/database/tickers",
                "method": "GET",
                "description": "List all cached tickers",
            },
            "database_cleanup": {
                "url": "/database/cleanup",
                "method": "POST",
                "description": "Clean up old data (optional parameter: days_to_keep)",
            },
            "ticker_stats": {
                "url": "/database/ticker/<ticker>",
                "method": "GET",
                "description": "Get statistics for a specific ticker",
            },
            "fundamental": {
                "url": "/fundamental",
                "method": "GET",
                "description": "Retrieves all financial information for a ticker or an ISIN via multiple providers",
                "parameters": {
                    "ticker": {"description": "Stock symbol (e.g., AAPL)", "required": False},
                    "isin": {"description": "ISIN code (e.g., FR0004125920)", "required": False},
                    "provider": {
                        "description": "Comma-separated list of providers (e.g., zonebourse,googlefinance)",
                        "required": False,
                    },
                },
            },
            "stocks_market_overview": {
                "url": "/stocks",
                "method": "GET",
                "description": "Market overview with a list of popular tickers",
            },
            "stocks_financials": {
                "url": "/stocks/<ticker>/financials",
                "method": "GET",
                "description": "Aggregated financial data for a ticker",
            },
            "ticker_history": {
                "url": "/ticker/<symbol>/history",
                "method": "GET",
                "description": "Price history for a ticker (extracted from the prices_eod time-series table)",
                "parameters": {
                    "start_date": {"description": "Start date (YYYY-MM-DD)", "required": False},
                    "end_date": {"description": "End date (YYYY-MM-DD)", "required": False},
                    "interval": {
                        "description": "Data resolution (1D, 1W, 1M, daily, weekly, monthly)",
                        "required": False,
                        "default": "1D",
                    },
                },
            },
            "historical_ingest": {
                "url": "/historical/ingest",
                "method": "POST",
                "description": "Triggers EOD historical ingestion for an individual asset",
                "parameters": {
                    "ticker": {"description": "Asset ticker", "required": True},
                    "resolution": {
                        "description": "Resolution (1D, 1W, 1M)",
                        "required": False,
                        "default": "1D",
                    },
                    "source": {
                        "description": "Source (auto, yfinance, tradingview)",
                        "required": False,
                        "default": "auto",
                    },
                    "force_refresh": {
                        "description": "Force full ingestion without gap detection",
                        "required": False,
                        "default": False,
                    },
                    "from_date": {
                        "description": "Custom start date (YYYY-MM-DD)",
                        "required": False,
                    },
                    "to_date": {"description": "Custom end date (YYYY-MM-DD)", "required": False},
                },
            },
            "historical_ingest_bulk": {
                "url": "/historical/ingest/bulk",
                "method": "POST",
                "description": "Triggers parallel historical ingestion for multiple tickers",
                "body": {
                    "tickers": ["AAPL", "MSFT"],
                    "resolution": "1D",
                    "source": "auto",
                    "force_refresh": False,
                    "concurrency": 5,
                },
            },
        },
        "data_fields": [
            "time (Candle timestamp)",
            "open (Opening price)",
            "high (Maximum price)",
            "low (Minimum price)",
            "close (Closing price)",
            "adj_close (Adjusted price)",
            "volume (Trading volume)",
            "resolution (Candle resolution: 1D, 1W, 1M)",
            "source (Data source: yfinance, tradingview)",
        ],
        "notes": [
            "All data is retrieved WITHOUT rounding for maximum precision",
            "EOD historical data is centralized in a TimescaleDB hypertable (prices_eod)",
            "1W and 1M interval queries leverage TimescaleDB continuous aggregate views for better performance",
            "Support for multiple data sources: Yahoo Finance (via yfinance) and TradingView as a WebSocket fallback",
            "Automatic temporal gap detection (Gap Detection) during ingestions to avoid redundant requests",
            "High-performance Redis cache for all history reads (TTL configurable by resolution)",
            "CLI script available for bulk/large ingestions (scripts/ingest_all.py)",
        ],
        "caching_system": {
            "description": "Multi-level caching system and temporal persistence",
            "cache_levels": [
                {
                    "level": 1,
                    "type": "Redis Cache",
                    "description": "Ultra-fast memory cache by ticker and resolution, first source consulted during read queries",
                    "ttl": "Configurable (e.g. 24h for EOD/history, clear manually via /cache/clear)",
                },
                {
                    "level": 2,
                    "type": "TimescaleDB Hypertable",
                    "description": "Time-series database persisting all historical data with automatic compression beyond 14 days",
                    "tables": [
                        "prices_eod (Base Hypertable)",
                        "prices_weekly (Continuous aggregate for 1W resolution)",
                        "prices_monthly (Continuous aggregate for 1M resolution)",
                    ],
                },
                {
                    "level": 3,
                    "type": "Financial providers (Yahoo Finance / TradingView)",
                    "description": "External data source called during ingestion or history refresh",
                },
            ],
            "cache_management": [
                "Fine-grained Redis cache invalidation during writes / ingestions",
                "Endpoint /cache/clear to manually clear all Redis cache",
                "Endpoint /cache/clear/<ticker> to clear the Redis cache of a specific ticker",
            ],
        },
    }

    # record service removed — static documentation only
    doc["endpoints"]["record"] = {
        "url": "/record/{ticker}",
        "method": "GET",
        "description": "Triggers EOD historical ingestion in the background",
        "parameters": {
            "period": {"description": "Period (optional)", "required": False},
            "from": {"description": "Start date YYYY-MM-DD", "required": False},
            "to": {"description": "End date YYYY-MM-DD", "required": False},
        },
    }

    return doc
