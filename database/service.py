"""Compatibility facade composing focused synchronous database components."""

import logging
import os
from configparser import Error as ConfigParserError

from alembic.util.exc import CommandError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import scoped_session, sessionmaker

from database.assets import AssetRepository
from database.fundamentals import FundamentalsRepository
from database.maintenance import DatabaseMaintenance
from database.migrations import MigrationInspector
from database.usage import UsageRepository

logger = logging.getLogger(__name__)


class DatabaseService:
    """Own the SQLAlchemy runtime and delegate operations to focused components.

    The facade preserves the historical public API while allowing new code to
    depend directly on the assets, fundamentals, maintenance or usage
    components.
    """

    def __init__(self, database_url=None):
        database_url = database_url or os.environ.get(
            "DATABASE_URL",
            "postgresql://fonrex:fonrex_password@localhost:5432/fonrex",
        )
        self.engine = create_engine(database_url)
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        self.migrations = MigrationInspector(self.engine, self.Session)
        self.assets = AssetRepository(self.engine, self.Session)
        self.fundamentals = FundamentalsRepository(self.engine, self.Session)
        self.maintenance = DatabaseMaintenance(self.engine, self.Session)
        self.usage = UsageRepository(self.engine, self.Session)

    def check_connection(self):
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            logger.info("✅ Connexion à la base de données vérifiée")
            return True
        except SQLAlchemyError as exc:
            logger.error("❌ Base de données indisponible: %s", exc)
            return False

    def check_migrations(self):
        """Return whether the connected schema is exactly at Alembic head."""
        try:
            status = self.migrations.get_status()
        except (SQLAlchemyError, CommandError, ConfigParserError, OSError) as exc:
            logger.error("❌ Impossible de vérifier la révision Alembic: %s", exc)
            return False
        if not status.is_current:
            logger.error(
                "❌ Schéma obsolète (révision=%s, attendue=%s). Exécutez `alembic upgrade head`.",
                status.current_heads or ("aucune",),
                status.expected_heads,
            )
            return False
        logger.info("✅ Schéma à jour à la révision Alembic %s", status.current_heads)
        return True

    def close(self):
        self.Session.remove()
        self.engine.dispose()

    def get_session(self):
        return self.Session()

    # Maintenance and statistics
    def get_model_counts(self):
        return self.maintenance.get_model_counts()

    def get_database_stats(self):
        return self.maintenance.get_database_stats()

    def get_cached_tickers(self):
        return self.maintenance.get_cached_tickers()

    def cleanup_old_data(self, days_to_keep=730):
        return self.maintenance.cleanup_old_data(days_to_keep)

    def _cleanup_old_data(self, days_to_keep=730):
        return self.maintenance._cleanup_old_data(days_to_keep)

    # Asset identity and profile
    def find_listings(self, *args, **kwargs):
        return self.assets.find_listings(*args, **kwargs)

    def get_listing_by_identity(self, *args, **kwargs):
        return self.assets.get_listing_by_identity(*args, **kwargs)

    def get_asset_by_identity(self, *args, **kwargs):
        return self.assets.get_asset_by_identity(*args, **kwargs)

    def get_asset_context(self, *args, **kwargs):
        return self.assets.get_asset_context(*args, **kwargs)

    def get_asset_mappings(self, *args, **kwargs):
        return self.assets.get_asset_mappings(*args, **kwargs)

    def get_asset_details(self, *args, **kwargs):
        return self.assets.get_asset_details(*args, **kwargs)

    def get_ticker_stats(self, ticker):
        return self.assets.get_ticker_stats(ticker)

    def update_asset_profile_from_metadata(self, *args, **kwargs):
        return self.assets.update_asset_profile_from_metadata(*args, **kwargs)

    @staticmethod
    def _normalize_ticker(value):
        return AssetRepository._normalize_ticker(value)

    @staticmethod
    def _normalize_optional_upper(value):
        return AssetRepository._normalize_optional_upper(value)

    @classmethod
    def _looks_like_isin(cls, value):
        return AssetRepository._looks_like_isin(value)

    @classmethod
    def _listing_enrichment_rank(cls, listing):
        return AssetRepository._listing_enrichment_rank(listing)

    @classmethod
    def _quote_type_rank(cls, quote_type):
        return AssetRepository._quote_type_rank(quote_type)

    @classmethod
    def _listing_identity_rank(cls, listing):
        return AssetRepository._listing_identity_rank(listing)

    @classmethod
    def _preferred_listing_for_asset(cls, asset, listings=None):
        return AssetRepository._preferred_listing_for_asset(asset, listings)

    @staticmethod
    def _listing_to_dict(listing, asset=None):
        return AssetRepository._listing_to_dict(listing, asset)

    @staticmethod
    def _asset_to_dict(asset, listing=None):
        return AssetRepository._asset_to_dict(asset, listing)

    @staticmethod
    def _first_present(*values):
        return AssetRepository._first_present(*values)

    @classmethod
    def _metadata_is_compatible_with_asset(cls, asset, metadata):
        return AssetRepository._metadata_is_compatible_with_asset(asset, metadata)

    @staticmethod
    def asset_profile_needs_enrichment(details):
        return AssetRepository.asset_profile_needs_enrichment(details)

    @staticmethod
    def metadata_has_profile_enrichment(metadata):
        return AssetRepository.metadata_has_profile_enrichment(metadata)

    @classmethod
    def asset_profile_enrichment_tickers(cls, details, request_ticker=None, limit=5):
        return AssetRepository.asset_profile_enrichment_tickers(details, request_ticker, limit)

    # Fundamental read models
    def get_fundamental_data(self, asset_id):
        return self.fundamentals.get_fundamental_data(asset_id)

    def get_deep_fundamentals(self, asset_id):
        return self.fundamentals.get_deep_fundamentals(asset_id)

    def get_deep_sections(self, asset_id, requested_sections, want_all):
        return self.fundamentals.get_deep_sections(asset_id, requested_sections, want_all)

    # Usage analytics
    def log_usage(self, *args, **kwargs):
        return self.usage.log_usage(*args, **kwargs)
