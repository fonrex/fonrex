"""Canary assets and lazy provider registry."""

from __future__ import annotations

import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

CANARY_ASSETS: dict[str, dict[str, tuple[float, float]]] = {
    "AAPL": {
        "pe_ratio": (20.0, 45.0),
        "dividend_yield": (0.003, 0.01),
        "beta": (0.8, 1.5),
        "price": (100.0, 500.0),
    },
    "AIR.PA": {
        "pe_ratio": (15.0, 60.0),
        "dividend_yield": (0.005, 0.04),
        "beta": (0.8, 1.8),
        "price": (80.0, 300.0),
    },
    "BNP.PA": {
        "pe_ratio": (4.0, 15.0),
        "dividend_yield": (0.04, 0.12),
        "pb_ratio": (0.3, 1.5),
        "price": (30.0, 100.0),
    },
    "MSFT": {
        "pe_ratio": (25.0, 50.0),
        "beta": (0.7, 1.3),
        "price": (200.0, 600.0),
    },
    "TSLA": {
        "pe_ratio": (30.0, 300.0),
        "beta": (1.5, 3.5),
        "price": (100.0, 600.0),
    },
}

MONITORED_PROVIDERS = [
    "ZoneBourse",
    "GoogleFinance",
    "Boursorama",
    "Barrons",
    "wallStreetJournal",
    "Marketwatch",
    "MorningStar",
    "Investing",
    "Gurufocus",
    "Fortuneo",
    "BourseDirect",
    "Msn",
    "InvestirLesEchos",
    "YahooFinance",
]

_EU_ONLY_PROVIDERS = {
    "InvestirLesEchos",
    "Boursorama",
    "Fortuneo",
    "BourseDirect",
}
_EU_TICKERS = {"AIR.PA", "BNP.PA"}

_PROVIDER_IMPORTS = {
    "ZoneBourse": ("financials.providers.ZoneBourse_provider", "ZoneBourseProvider"),
    "GoogleFinance": ("financials.providers.GoogleFinance_provider", "GoogleFinanceProvider"),
    "Boursorama": ("financials.providers.boursorama_provider", "BoursoramaProvider"),
    "Barrons": ("financials.providers.Barrons_provider", "BarronsProvider"),
    "wallStreetJournal": (
        "financials.providers.wallStreetJournal_provider",
        "WallStreetJournalProvider",
    ),
    "Marketwatch": ("financials.providers.Marketwatch_provider", "MarketwatchProvider"),
    "MorningStar": ("financials.providers.MorningStar_provider", "MorningStarProvider"),
    "Investing": ("financials.providers.Investing_provider", "InvestingProvider"),
    "Gurufocus": ("financials.providers.Gurufocus_provider", "GurufocusProvider"),
    "Fortuneo": ("financials.providers.Fortuneo_provider", "FortuneoProvider"),
    "BourseDirect": ("financials.providers.BourseDirect_provider", "BourseDirectProvider"),
    "Msn": ("financials.providers.Msn_provider", "MsnProvider"),
    "InvestirLesEchos": (
        "financials.providers.InvestirLesEchos_provider",
        "InvestirLesEchosProvider",
    ),
    "YahooFinance": ("financials.providers.yfinance_provider", "YFinanceProvider"),
}


def get_provider_class(provider_name: str) -> Any | None:
    """Resolve a provider class lazily to avoid import cycles."""
    entry = _PROVIDER_IMPORTS.get(provider_name)
    if not entry:
        return None
    module_path, class_name = entry
    try:
        module = importlib.import_module(module_path)
        return getattr(module, class_name, None)
    except (ImportError, AttributeError) as exc:
        logger.warning("Cannot import provider %s: %s", provider_name, exc)
        return None


def is_compatible(provider_name: str, ticker: str) -> bool:
    """Return whether a provider supports the ticker's market."""
    if provider_name in _EU_ONLY_PROVIDERS:
        return ticker in _EU_TICKERS
    return True
