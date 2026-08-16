#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Service de gestion du cache Redis pour FonRex API.
"""

import json
import logging
import pickle
from datetime import date, datetime
from decimal import Decimal

import redis

logger = logging.getLogger(__name__)


class CacheService:
    """Service de gestion du cache Redis."""

    DEFAULT_TTLS = {
        "eod": 86400,
        "intraday": 3600,
        "fundamentals": 604800,
        "metadata": 2592000,
        "mappings": 7776000,
        "history": 86400,
        # Deep fundamentals (yfinance enrichment)
        "highlights": 86400,  # 24h — métriques clés (prix, ratios)
        "statements": 604800,  # 7j  — bilans (changent par trimestre)
        "earnings": 86400,  # 24h — peut changer lors des publications
        "ratings": 43200,  # 12h — consensus analystes
        # Phase 3 — nouveaux providers
        "insider_transactions": 43200,  # 12h — Form 4 SEC
        "etf_details": 86400,  # 24h — données ETF justETF
        "index_constituents": 604800,  # 7j  — composants indices
        "openfigi_mapping": 7776000,  # 90j — les FIGIs changent rarement
        # Indicateurs techniques — selon résolution
        "technical_1D": 3600,  # 1h  — EOD stable pendant la journée
        "technical_1W": 7200,  # 2h
        "technical_1M": 14400,  # 4h
        "technical_1min": 60,  # 60s — intraday live (données qui bougent)
        "technical_5min": 120,  # 2min
        "technical_list": 86400,  # 24h — liste statique des indicateurs
        "technical_screen": 900,  # 15min — résultats du screener
        "chart_data_1D": 3600,  # 1h  — données graphiques EOD
        "chart_data_1min": 60,  # 60s — données graphiques intraday
        # News financières (Phase 10)
        "news": 1800,  # 30min — news par ticker
        "news_feed": 1800,  # 30min — feed global
        # Valuation DCF (Phase 11)
        "dcf": 21600,  # 6h — DCF par ticker
        "dcf_sensitivity": 21600,  # 6h — matrice sensibilité
    }

    def __init__(self, redis_url=None, ttl=300, ttl_by_type=None):
        """
        Initialise le service de cache.

        Args:
            redis_url (str): URL de connexion Redis
            ttl (int): Durée de vie du cache en secondes (défaut: 300s)
        """
        self.redis_url = redis_url or "redis://localhost:6379/0"
        self.ttl = ttl
        self.ttl_by_type = {**self.DEFAULT_TTLS, **(ttl_by_type or {})}
        self.enabled = False
        self.client = None
        self._connect()

    @staticmethod
    def _json_default(value):
        """Convertit les types courants de données financières en JSON."""
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
        """Retourne le TTL adapté au type de donnée."""
        return self.ttl_by_type.get(cache_type, self.ttl)

    def _connect(self):
        """Établit la connexion au serveur Redis."""
        try:
            self.client = redis.from_url(self.redis_url, decode_responses=False)
            # Test de connexion
            self.client.ping()
            self.enabled = True
            logger.info(f"✅ Redis connecté: {self.redis_url}")
        except (redis.exceptions.RedisError, ValueError) as e:
            logger.warning(f"⚠️ Redis indisponible: {e}. Cache désactivé.")
            self.client = None
            self.enabled = False

    def generate_key(self, ticker, period=None, cache_type="eod", **filters):
        """
        Génère une clé de cache unique pour un ticker et une période.

        Args:
            ticker (str): Symbole du ticker
            period (str): Période de données

        Returns:
            str: Clé de cache hashée
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
        Récupère des données du cache Redis.

        Args:
            cache_key (str): Clé de cache

        Returns:
            any: Données en cache ou None si non trouvées
        """
        if not self.enabled:
            # Tenter de se reconnecter si le cache était désactivé
            self._connect()
            if not self.enabled:
                return None

        try:
            cached_data = self.client.get(cache_key)
            if cached_data:
                logger.info(f"🎯 Cache HIT pour la clé: {cache_key}")
                if isinstance(cached_data, bytes):
                    try:
                        return json.loads(cached_data.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return pickle.loads(cached_data)
                return json.loads(cached_data)
            else:
                logger.info(f"❌ Cache MISS pour la clé: {cache_key}")
                return None
        except redis.exceptions.ConnectionError:
            logger.warning("Connexion Redis perdue. Tentative de reconnexion...")
            self._connect()
            return None
        except (
            redis.exceptions.RedisError,
            json.JSONDecodeError,
            pickle.PickleError,
            EOFError,
        ) as e:
            logger.error(f"Erreur lors de la lecture du cache: {e}")
            return None

    def set(self, cache_key, data, cache_type="eod", ttl=None):
        """
        Sauvegarde des données dans le cache Redis.

        Args:
            cache_key (str): Clé de cache
            data (any): Données à mettre en cache

        Returns:
            bool: True si succès, False sinon
        """
        if not self.enabled:
            # Tenter de se reconnecter si le cache était désactivé
            self._connect()
            if not self.enabled:
                return False

        try:
            expires_in = ttl or self.get_ttl(cache_type)
            serialized_data = json.dumps(data, default=self._json_default).encode("utf-8")
            self.client.setex(cache_key, expires_in, serialized_data)
            logger.info(f"💾 Données mises en cache pour {expires_in}s avec la clé: {cache_key}")
            return True
        except redis.exceptions.ConnectionError:
            logger.warning("Connexion Redis perdue. Tentative de reconnexion...")
            self._connect()
            return False
        except (redis.exceptions.RedisError, TypeError, ValueError) as e:
            logger.error(f"Erreur lors de l'écriture dans le cache: {e}")
            return False

    def clear(self, pattern="eod:*"):
        """
        Vide le cache Redis pour un motif donné.

        Args:
            pattern (str): Motif des clés à supprimer (défaut: "eod:*")

        Returns:
            tuple: (success, deleted_count, error_message)
        """
        if not self.enabled:
            return False, 0, "Cache non activé"

        try:
            keys = self.client.keys(pattern)
            if keys:
                deleted_count = self.client.delete(*keys)
                logger.info(f"🗑️ Cache vidé: {deleted_count} clés supprimées")
                return True, deleted_count, None
            else:
                logger.info("Cache déjà vide")
                return True, 0, None
        except redis.exceptions.RedisError as e:
            error_msg = f"Erreur lors du vidage du cache: {e}"
            logger.error(error_msg)
            return False, 0, error_msg

    def get_stats(self):
        """
        Récupère les statistiques du cache Redis.

        Returns:
            tuple: (success, stats_dict, error_message)
        """
        if not self.enabled:
            return False, None, "Cache non activé"

        try:
            info = self.client.info()
            all_keys = self.client.keys("eod:*")

            # Analyser les clés par ticker
            ticker_stats = {}
            for key in all_keys:
                try:
                    # Format de clé attendu: eod:TICKER:PERIOD
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

            # Convertir les périodes en listes
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
            error_msg = f"Erreur lors de la récupération des stats: {e}"
            logger.error(error_msg)
            return False, None, error_msg

    def clear_ticker_cache(self, ticker, period=None):
        """
        Vide le cache Redis pour un ticker spécifique et optionnellement une période.

        Args:
            ticker (str): Symbole du ticker
            period (str, optional): Période spécifique à vider

        Returns:
            tuple: (success, deleted_count, error_message)
        """
        if not self.enabled:
            return False, 0, "Cache non activé"

        try:
            pattern = f"eod:{ticker.upper()}:*"
            if period:
                pattern = f"eod:{ticker.upper()}:{period}"

            keys = self.client.keys(pattern)
            if keys:
                deleted_count = self.client.delete(*keys)
                logger.info(f"🗑️ Cache vidé pour {ticker}: {deleted_count} clés supprimées")
                return True, deleted_count, None
            else:
                logger.info(f"Cache déjà vide pour {ticker}")
                return True, 0, None
        except redis.exceptions.RedisError as e:
            error_msg = f"Erreur lors du vidage du cache pour {ticker}: {e}"
            logger.error(error_msg)
            return False, 0, error_msg

    def get_status(self):
        """
        Récupère le statut de connexion Redis.

        Returns:
            str: "connected", "error" ou "disabled"
        """
        if not self.enabled:
            return "disabled"

        try:
            self.client.ping()
            return "connected"
        except redis.exceptions.RedisError:
            return "error"
