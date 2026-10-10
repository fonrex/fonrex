#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Redis cache service of the Fonrex API.
"""

import json
import logging
from datetime import date, datetime
from decimal import Decimal

import redis

logger = logging.getLogger(__name__)


class CacheService:
    """Redis cache service."""

    DEFAULT_TTLS = {
        "eod": 86400,
        "intraday": 3600,
        "fundamentals": 604800,
        "metadata": 2592000,
        "mappings": 7776000,
        "history": 86400,
        # Deep fundamentals (yfinance enrichment)
        "highlights": 86400,  # 24h — key metrics (price, ratios)
        "statements": 604800,  # 7d  — statements (change every quarter)
        "earnings": 86400,  # 24h — may change at earnings releases
        "ratings": 43200,  # 12h — analyst consensus
        # Phase 3 — new providers
        "insider_transactions": 43200,  # 12h — Form 4 SEC
        "etf_details": 86400,  # 24h — justETF ETF data
        "index_constituents": 604800,  # 7d  — index constituents
        "openfigi_mapping": 7776000,  # 90d — FIGIs rarely change
        # Technical indicators — by resolution
        "technical_1D": 3600,  # 1h  — EOD is stable during the day
        "technical_1W": 7200,  # 2h
        "technical_1M": 14400,  # 4h
        "technical_1min": 60,  # 60s — live intraday (moving data)
        "technical_5min": 120,  # 2min
        "technical_list": 86400,  # 24h — static list of indicators
        "technical_screen": 900,  # 15min — screener results
        "chart_data_1D": 3600,  # 1h  — EOD chart data
        "chart_data_1min": 60,  # 60s — intraday chart data
        # Financial news (Phase 10)
        "news": 1800,  # 30min — news per ticker
        "news_feed": 1800,  # 30min — global feed
        # Valuation DCF (Phase 11)
        "dcf": 21600,  # 6h — DCF per ticker
        "dcf_sensitivity": 21600,  # 6h — sensitivity matrix
        # Macro Rates (Phase 13)
        "macro_rates": 21600,  # 6h — FRED rates
    }

    def __init__(self, redis_url=None, ttl=300, ttl_by_type=None):
        """
        Initialise the cache service.

        Args:
            redis_url (str): Redis connection URL
            ttl (int): Cache lifetime in seconds (default: 300s)
        """
        self.redis_url = redis_url or "redis://localhost:6379/0"
        self.ttl = ttl
        self.ttl_by_type = {**self.DEFAULT_TTLS, **(ttl_by_type or {})}
        self.enabled = False
        self.client = None
        self._connect()

    @staticmethod
    def _json_default(value):
        """Convert the usual types of financial data to JSON."""
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        if hasattr(value, "item"):
            return value.item()
        raise TypeError(f"Type {type(value)} not serializable")

    @staticmethod
    def _sanitize_segment(value):
        value = str(value).strip()
        return value.replace(":", "_").replace(" ", "_")

    def get_ttl(self, cache_type="eod"):
        """The TTL of a type of data."""
        return self.ttl_by_type.get(cache_type, self.ttl)

    def _connect(self):
        """Connect to the Redis server."""
        try:
            self.client = redis.from_url(self.redis_url, decode_responses=False)
            # Connection test
            self.client.ping()
            self.enabled = True
            logger.info(f"✅ Redis connected: {self.redis_url}")
        except (redis.exceptions.RedisError, ValueError) as e:
            logger.warning(f"⚠️ Redis unavailable: {e}. Cache disabled.")
            self.client = None
            self.enabled = False

    def generate_key(self, ticker, period=None, cache_type="eod", **filters):
        """
        Build the cache key of a ticker and a period.

        Args:
            ticker (str): Ticker symbol
            period (str): Data period

        Returns:
            str: Cache key
        """
        key_parts = [cache_type, ticker.upper()]
        if period:
            key_parts.append(period)

        for key, value in sorted(filters.items()):
            if value is not None:
                key_parts.append(f"{key}-{value}")

        return ":".join(self._sanitize_segment(part) for part in key_parts)

    def get(self, cache_key):
        """
        Read data from the Redis cache.

        Args:
            cache_key (str): Cache key

        Returns:
            any: Cached data, or None when not found
        """
        if not self.enabled:
            # Try to reconnect if the cache was disabled
            self._connect()
            if not self.enabled:
                return None

        try:
            cached_data = self.client.get(cache_key)
            if cached_data:
                logger.info(f"🎯 Cache HIT for key: {cache_key}")
                # An entry is JSON, written by ``set``. Anything else is ignored:
                # it is never rebuilt with ``pickle``, which runs the code an
                # entry carries — whoever can write to Redis would run it here.
                if isinstance(cached_data, bytes):
                    cached_data = cached_data.decode("utf-8")
                return json.loads(cached_data)
            else:
                logger.info(f"❌ Cache MISS for key: {cache_key}")
                return None
        except redis.exceptions.ConnectionError:
            logger.warning("Redis connection lost. Reconnecting...")
            self._connect()
            return None
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning(f"Unreadable cache entry ignored ({cache_key}): {e}")
            return None
        except redis.exceptions.RedisError as e:
            logger.error(f"Cache read error: {e}")
            return None

    def set(self, cache_key, data, cache_type="eod", ttl=None):
        """
        Write data to the Redis cache.

        Args:
            cache_key (str): Cache key
            data (any): Data to cache

        Returns:
            bool: True on success, False otherwise
        """
        if not self.enabled:
            # Try to reconnect if the cache was disabled
            self._connect()
            if not self.enabled:
                return False

        try:
            expires_in = ttl or self.get_ttl(cache_type)
            serialized_data = json.dumps(data, default=self._json_default).encode("utf-8")
            # ``SET ... EX``: the Redis client flags ``setex`` as deprecated.
            self.client.set(cache_key, serialized_data, ex=expires_in)
            logger.info(f"💾 Data cached for {expires_in}s under key: {cache_key}")
            return True
        except redis.exceptions.ConnectionError:
            logger.warning("Redis connection lost. Reconnecting...")
            self._connect()
            return False
        except (redis.exceptions.RedisError, TypeError, ValueError) as e:
            logger.error(f"Cache write error: {e}")
            return False

    def clear(self, pattern="eod:*"):
        """
        Clear the Redis keys matching a pattern.

        Args:
            pattern (str): Pattern of the keys to delete (default: "eod:*")

        Returns:
            tuple: (success, deleted_count, error_message)
        """
        if not self.enabled:
            return False, 0, "Cache not enabled"

        try:
            keys = self.client.keys(pattern)
            if keys:
                deleted_count = self.client.delete(*keys)
                logger.info(f"🗑️ Cache cleared: {deleted_count} keys deleted")
                return True, deleted_count, None
            else:
                logger.info("Cache already empty")
                return True, 0, None
        except redis.exceptions.RedisError as e:
            error_msg = f"Cache clear error: {e}"
            logger.error(error_msg)
            return False, 0, error_msg

    def get_stats(self):
        """
        Statistics of the Redis cache.

        Returns:
            tuple: (success, stats_dict, error_message)
        """
        if not self.enabled:
            return False, None, "Cache not enabled"

        try:
            info = self.client.info()
            all_keys = self.client.keys("eod:*")

            # Count the keys per ticker
            ticker_stats = {}
            for key in all_keys:
                try:
                    # Expected key format: eod:TICKER:PERIOD
                    parts = key.decode("utf-8").split(":")
                    if len(parts) >= 3:
                        ticker = parts[1]
                        period = parts[2]

                        if ticker not in ticker_stats:
                            ticker_stats[ticker] = {"total": 0, "periods": {}}

                        ticker_stats[ticker]["total"] += 1
                        ticker_stats[ticker]["periods"][period] = True
                except (UnicodeError, AttributeError, IndexError):
                    continue

            # Turn the periods into lists
            for ticker in ticker_stats:
                ticker_stats[ticker]["periods"] = list(ticker_stats[ticker]["periods"].keys())

            stats = {
                "cache_enabled": True,
                "redis_version": info.get("redis_version", "unknown"),
                "connected_clients": info.get("connected_clients", 0),
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "eod_cache_keys": len(all_keys),
                "default_ttl_seconds": self.ttl,
                "ttl_by_type_seconds": self.ttl_by_type,
                "uptime_seconds": info.get("uptime_in_seconds", 0),
                "tickers_in_cache": len(ticker_stats),
                "ticker_stats": ticker_stats,
            }

            return True, stats, None
        except redis.exceptions.RedisError as e:
            error_msg = f"Cache statistics error: {e}"
            logger.error(error_msg)
            return False, None, error_msg

    def clear_ticker_cache(self, ticker, period=None):
        """
        Clear the Redis cache of a ticker, optionally of one period only.

        Args:
            ticker (str): Ticker symbol
            period (str, optional): Period to clear

        Returns:
            tuple: (success, deleted_count, error_message)
        """
        if not self.enabled:
            return False, 0, "Cache not enabled"

        try:
            pattern = f"eod:{ticker.upper()}:*"
            if period:
                pattern = f"eod:{ticker.upper()}:{period}"

            keys = self.client.keys(pattern)
            if keys:
                deleted_count = self.client.delete(*keys)
                logger.info(f"🗑️ Cache cleared for {ticker}: {deleted_count} keys deleted")
                return True, deleted_count, None
            else:
                logger.info(f"Cache already empty for {ticker}")
                return True, 0, None
        except redis.exceptions.RedisError as e:
            error_msg = f"Cache clear error for {ticker}: {e}"
            logger.error(error_msg)
            return False, 0, error_msg

    def get_status(self):
        """
        Status of the Redis connection.

        Returns:
            str: "connected", "error" or "disabled"
        """
        if not self.enabled:
            return "disabled"

        try:
            self.client.ping()
            return "connected"
        except redis.exceptions.RedisError:
            return "error"
