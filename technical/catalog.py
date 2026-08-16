"""Declarative catalog and cache policy for technical indicators."""

from technical.contracts import IndicatorDefinition, IndicatorParams

INDICATOR_REGISTRY: dict[str, IndicatorDefinition] = {
    "sma": {"func": "sma", "params": ["length"], "cols": ["SMA_{length}"], "category": "trend"},
    "ema": {"func": "ema", "params": ["length"], "cols": ["EMA_{length}"], "category": "trend"},
    "wma": {"func": "wma", "params": ["length"], "cols": ["WMA_{length}"], "category": "trend"},
    "dema": {"func": "dema", "params": ["length"], "cols": ["DEMA_{length}"], "category": "trend"},
    "tema": {"func": "tema", "params": ["length"], "cols": ["TEMA_{length}"], "category": "trend"},
    "vwap": {"func": "vwap", "params": [], "cols": ["VWAP_D"], "category": "trend"},
    "rsi": {"func": "rsi", "params": ["length"], "cols": ["RSI_{length}"], "category": "momentum"},
    "macd": {
        "func": "macd",
        "params": ["fast", "slow", "signal"],
        "cols": [
            "MACD_{fast}_{slow}_{signal}",
            "MACDh_{fast}_{slow}_{signal}",
            "MACDs_{fast}_{slow}_{signal}",
        ],
        "category": "momentum",
    },
    "stoch": {
        "func": "stoch",
        "params": ["k", "d", "smooth_k"],
        "cols": ["STOCHk_{k}_{d}_{smooth_k}", "STOCHd_{k}_{d}_{smooth_k}"],
        "category": "momentum",
    },
    "cci": {
        "func": "cci",
        "params": ["length"],
        "cols": ["CCI_{length}_0.015"],
        "category": "momentum",
    },
    "roc": {"func": "roc", "params": ["length"], "cols": ["ROC_{length}"], "category": "momentum"},
    "mom": {"func": "mom", "params": ["length"], "cols": ["MOM_{length}"], "category": "momentum"},
    "bbands": {
        "func": "bbands",
        "params": ["length", "std"],
        "cols": [
            "BBL_{length}_{std}_{ddof}",
            "BBM_{length}_{std}_{ddof}",
            "BBU_{length}_{std}_{ddof}",
            "BBB_{length}_{std}_{ddof}",
            "BBP_{length}_{std}_{ddof}",
        ],
        "category": "volatility",
    },
    "atr": {
        "func": "atr",
        "params": ["length"],
        "cols": ["ATRr_{length}"],
        "category": "volatility",
    },
    "kc": {
        "func": "kc",
        "params": ["length"],
        "cols": ["KCLe_{length}_2", "KCBe_{length}_2", "KCUe_{length}_2"],
        "category": "volatility",
    },
    "obv": {"func": "obv", "params": [], "cols": ["OBV"], "category": "volume"},
    "ad": {"func": "ad", "params": [], "cols": ["AD"], "category": "volume"},
    "mfi": {"func": "mfi", "params": ["length"], "cols": ["MFI_{length}"], "category": "volume"},
}

INDICATOR_DEFAULTS: dict[str, IndicatorParams] = {
    "sma": {"length": 20},
    "ema": {"length": 20},
    "wma": {"length": 20},
    "dema": {"length": 20},
    "tema": {"length": 20},
    "rsi": {"length": 14},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "stoch": {"k": 14, "d": 3, "smooth_k": 3},
    "cci": {"length": 20},
    "roc": {"length": 10},
    "mom": {"length": 10},
    "bbands": {"length": 20, "std": 2.0, "ddof": 2.0},
    "atr": {"length": 14},
    "kc": {"length": 20},
    "mfi": {"length": 14},
    "obv": {},
    "vwap": {},
    "ad": {},
}

CACHE_TTL: dict[str, int] = {
    "1D": 3600,
    "1W": 7200,
    "1M": 14400,
    "1min": 60,
    "5min": 120,
}
