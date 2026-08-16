"""Asset identity, listing and profile persistence operations."""

import logging

from sqlalchemy import desc, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload

from database.component import DatabaseComponent
from models import Asset, AssetListing, IngestLog, PriceEOD, UsageLog

logger = logging.getLogger(__name__)


class AssetRepository(DatabaseComponent):
    @staticmethod
    def _normalize_ticker(value):
        return value.strip().upper() if value else None

    @staticmethod
    def _normalize_optional_upper(value):
        if value is None:
            return None
        return value.strip().upper()

    @classmethod
    def _looks_like_isin(cls, value):
        normalized = cls._normalize_ticker(value)
        if not normalized or len(normalized) != 12:
            return False
        return normalized[:2].isalpha() and normalized[2:11].isalnum() and normalized[-1].isdigit()

    @classmethod
    def _listing_enrichment_rank(cls, listing):
        if isinstance(listing, dict):
            currency = cls._normalize_optional_upper(listing.get("currency") or "")
            source = (listing.get("source") or "").lower()
            ticker = listing.get("ticker") or ""
            is_primary = bool(listing.get("is_primary"))
        else:
            currency = cls._normalize_optional_upper(getattr(listing, "currency", "") or "")
            source = (getattr(listing, "source", "") or "").lower()
            ticker = getattr(listing, "ticker", "") or ""
            is_primary = bool(getattr(listing, "is_primary", False))

        return (currency != "USD", source != "import_assets", not is_primary, ticker)

    @classmethod
    def _quote_type_rank(cls, quote_type):
        normalized = cls._normalize_ticker(quote_type)
        if normalized in ("STOCK", "EQUITY"):
            return 0
        if normalized in ("ETF", "ETP", "FUND"):
            return 1
        return 2

    @classmethod
    def _listing_identity_rank(cls, listing):
        asset = getattr(listing, "asset", None)
        quote_type = getattr(asset, "quote_type", None)
        currency = cls._normalize_optional_upper(getattr(listing, "currency", "") or "")
        source = (getattr(listing, "source", "") or "").lower()
        ticker = getattr(listing, "ticker", "") or ""
        exchange = getattr(listing, "exchange", "") or ""
        return (
            cls._quote_type_rank(quote_type),
            currency != "USD",
            source != "import_assets",
            not bool(getattr(listing, "is_primary", False)),
            ticker,
            currency,
            exchange,
        )

    @classmethod
    def _preferred_listing_for_asset(cls, asset, listings=None):
        """Choisit la cotation d'affichage la plus représentative pour un actif."""
        listings = list(listings or [])
        if not listings:
            return None

        active_listings = [listing for listing in listings if getattr(listing, "is_active", True)]
        candidates = active_listings or listings

        official_symbol = cls._normalize_ticker(getattr(asset, "official_symbol", None))
        if official_symbol:
            symbol_matches = [
                listing
                for listing in candidates
                if cls._normalize_ticker(getattr(listing, "ticker", None)) == official_symbol
            ]
            if symbol_matches:
                return sorted(symbol_matches, key=cls._listing_enrichment_rank)[0]

        primary = next(
            (listing for listing in candidates if getattr(listing, "is_primary", False)), None
        )
        if primary:
            return primary

        return sorted(
            candidates,
            key=lambda item: (item.ticker or "", item.currency or "", item.exchange or ""),
        )[0]

    @staticmethod
    def _listing_to_dict(listing, asset=None):
        """Convertit une cotation en dictionnaire API."""
        if not listing:
            return None

        asset = asset or listing.__dict__.get("asset")
        return {
            "id": listing.id,
            "asset_id": listing.asset_id,
            "ticker": listing.ticker,
            "exchange": listing.exchange or None,
            "currency": listing.currency or None,
            "isin": asset.isin if asset else None,
            "name": asset.name if asset else None,
            "source": listing.source,
            "is_primary": listing.is_primary,
            "is_active": listing.is_active,
        }

    @staticmethod
    def _asset_to_dict(asset, listing=None):
        """Convertit un Asset en dictionnaire stable pour les réponses API."""
        if not asset:
            return None

        data = {
            "asset_id": asset.id,
            "name": asset.name,
            "display_name": asset.display_name,
            "official_symbol": asset.official_symbol,
            "ticker": listing.ticker if listing else asset.ticker,
            "exchange": (listing.exchange or None) if listing else asset.exchange,
            "currency": (listing.currency or None) if listing else asset.currency,
            "sector": asset.sector,
            "industry": asset.industry,
            "quote_type": asset.quote_type,
            "isin": asset.isin,
            "logo_path": asset.logo_path,
            "ir_website": asset.ir_website,
            "long_business_summary": asset.long_business_summary,
            "listing_id": listing.id if listing else None,
        }

        loaded_listings = asset.__dict__.get("listings")
        if loaded_listings is not None:
            data["listings"] = [
                AssetRepository._listing_to_dict(item, asset=asset)
                for item in sorted(
                    loaded_listings,
                    key=lambda item: (
                        not item.is_primary,
                        item.ticker or "",
                        item.currency or "",
                        item.exchange or "",
                    ),
                )
            ]

        return data

    @staticmethod
    def asset_profile_needs_enrichment(details):
        """Détermine si le profil local a encore des champs métier manquants."""
        if not details:
            return False

        fields = (
            "display_name",
            "official_symbol",
            "exchange",
            "sector",
            "industry",
            "ir_website",
            "long_business_summary",
        )
        return any(not details.get(field) for field in fields)

    @staticmethod
    def metadata_has_profile_enrichment(metadata):
        """Vérifie qu'une réponse provider contient au moins un champ profil utile."""
        if not metadata:
            return False

        fields = (
            "display_name",
            "official_symbol",
            "exchange",
            "sector",
            "industry",
            "ir_website",
            "long_business_summary",
            "longName",
            "symbol",
            "description",
            "longBusinessSummary",
            "website",
            "irWebsite",
        )
        return any(metadata.get(field) for field in fields)

    @classmethod
    def asset_profile_enrichment_tickers(cls, details, request_ticker=None, limit=5):
        """Retourne les meilleurs tickers à tenter pour enrichir un profil via yfinance."""
        if not details:
            return []

        candidates = []
        seen = set()

        def add_candidate(value):
            normalized = cls._normalize_ticker(value)
            if not normalized or cls._looks_like_isin(normalized) or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(normalized)

        add_candidate(details.get("official_symbol"))

        listings = details.get("listings") or []
        for listing in sorted(listings, key=cls._listing_enrichment_rank):
            add_candidate(listing.get("ticker"))

        add_candidate(request_ticker)
        add_candidate(details.get("ticker"))

        for listing in listings:
            add_candidate(listing.get("ticker"))

        return candidates[:limit]

    @staticmethod
    def _first_present(*values):
        for value in values:
            if value is not None and value != "":
                return value
        return None

    @classmethod
    def _metadata_is_compatible_with_asset(cls, asset, metadata):
        asset_isin = cls._normalize_ticker(getattr(asset, "isin", None))
        metadata_isin = cls._normalize_ticker(metadata.get("isin"))
        if metadata_isin in ("-", "N/A", "NA", "NONE", "NULL"):
            metadata_isin = None

        if asset_isin and metadata_isin and asset_isin != metadata_isin:
            return False

        asset_quote_type = cls._normalize_ticker(getattr(asset, "quote_type", None))
        metadata_quote_type = cls._normalize_ticker(
            cls._first_present(metadata.get("quote_type"), metadata.get("quoteType"))
        )
        asset_rank = cls._quote_type_rank(asset_quote_type)
        metadata_rank = cls._quote_type_rank(metadata_quote_type)

        if asset_rank in (0, 1) and metadata_rank in (0, 1) and asset_rank != metadata_rank:
            return False

        return True

    def update_asset_profile_from_metadata(self, asset_id, metadata, listing_id=None):
        """
        Complète les champs asset/listing manquants à partir de métadonnées provider.
        Les valeurs existantes sont conservées.
        """
        if not asset_id or not metadata:
            return False

        session = self.get_session()
        try:
            asset = session.get(Asset, asset_id)
            if not asset:
                return False

            if not self._metadata_is_compatible_with_asset(asset, metadata):
                logger.info(
                    f"Enrichissement profil ignoré: metadata incompatible avec asset_id={asset_id}"
                )
                return False

            listing = session.get(AssetListing, listing_id) if listing_id else None

            field_values = {
                "name": self._first_present(
                    metadata.get("name"), metadata.get("shortName"), metadata.get("longName")
                ),
                "display_name": self._first_present(
                    metadata.get("display_name"), metadata.get("longName"), metadata.get("name")
                ),
                "official_symbol": self._first_present(
                    metadata.get("official_symbol"), metadata.get("symbol")
                ),
                "exchange": metadata.get("exchange"),
                "currency": metadata.get("currency"),
                "sector": metadata.get("sector"),
                "industry": metadata.get("industry"),
                "isin": metadata.get("isin"),
                "quote_type": self._first_present(
                    metadata.get("quote_type"), metadata.get("quoteType")
                ),
                "fund_family": self._first_present(
                    metadata.get("fund_family"), metadata.get("fundFamily")
                ),
                "long_business_summary": self._first_present(
                    metadata.get("long_business_summary"),
                    metadata.get("description"),
                    metadata.get("longBusinessSummary"),
                ),
                "ir_website": self._first_present(
                    metadata.get("ir_website"), metadata.get("website"), metadata.get("irWebsite")
                ),
                "country": metadata.get("country"),
                "country_code": metadata.get("country_code"),
                "profile": metadata.get("profile"),
            }

            updated = False
            for field, value in field_values.items():
                if value is None:
                    continue
                if not getattr(asset, field, None):
                    setattr(asset, field, value)
                    updated = True

            metadata_symbol = self._normalize_ticker(
                self._first_present(metadata.get("official_symbol"), metadata.get("symbol"))
            )
            target_listing = listing
            if metadata_symbol and (
                not listing
                or self._normalize_ticker(getattr(listing, "ticker", None)) != metadata_symbol
            ):
                target_listing = (
                    session.query(AssetListing)
                    .filter(
                        AssetListing.asset_id == asset.id, AssetListing.ticker == metadata_symbol
                    )
                    .order_by(AssetListing.currency.desc(), AssetListing.exchange.desc())
                    .first()
                )

            if target_listing:
                if field_values.get("exchange") and not target_listing.exchange:
                    target_listing.exchange = field_values["exchange"].strip().upper()
                    updated = True
                if field_values.get("currency") and not target_listing.currency:
                    target_listing.currency = field_values["currency"].strip().upper()
                    updated = True

            if updated:
                session.commit()
            return updated
        except SQLAlchemyError as e:
            session.rollback()
            logger.warning(f"Enrichissement profil asset ignoré pour asset_id={asset_id}: {e}")
            return False
        finally:
            session.close()

    def find_listings(
        self, ticker=None, isin=None, exchange=None, currency=None, active_only=True, limit=100
    ):
        """
        Recherche les cotations d'instruments par ticker, ISIN, exchange ou devise.

        Returns:
            list[AssetListing]: Cotations chargées avec l'instrument et les mappings.
        """
        session = self.get_session()
        try:
            normalized_ticker = self._normalize_ticker(ticker)
            normalized_isin = self._normalize_ticker(isin)
            normalized_exchange = self._normalize_optional_upper(exchange)
            normalized_currency = self._normalize_optional_upper(currency)

            query = (
                session.query(AssetListing)
                .join(Asset)
                .options(
                    joinedload(AssetListing.asset).joinedload(Asset.listings),
                    joinedload(AssetListing.asset).joinedload(Asset.mappings),
                    joinedload(AssetListing.mappings),
                )
            )

            if active_only:
                query = query.filter(AssetListing.is_active.is_(True))
            if normalized_ticker:
                query = query.filter(AssetListing.ticker == normalized_ticker)
            if normalized_isin:
                query = query.filter(Asset.isin == normalized_isin)
            if normalized_exchange is not None:
                query = query.filter(AssetListing.exchange == normalized_exchange)
            if normalized_currency is not None:
                query = query.filter(AssetListing.currency == normalized_currency)

            return (
                query.order_by(
                    AssetListing.is_primary.desc(),
                    AssetListing.ticker.asc(),
                    AssetListing.currency.asc(),
                    AssetListing.exchange.asc(),
                )
                .limit(limit)
                .all()
            )
        except SQLAlchemyError as e:
            logger.error(
                f"Erreur lors de la recherche listings ticker={ticker}, isin={isin}, exchange={exchange}, currency={currency}: {e}"
            )
            return []
        finally:
            session.close()

    def get_listing_by_identity(
        self, ticker=None, isin=None, exchange=None, currency=None, include_mappings=False
    ):
        """
        Récupère la cotation la plus précise possible pour une identité donnée.
        """
        bare_ticker_lookup = bool(ticker and not isin and not exchange and not currency)
        listings = self.find_listings(
            ticker=ticker,
            isin=isin,
            exchange=exchange,
            currency=currency,
            active_only=True,
            limit=25 if bare_ticker_lookup else (2 if ticker and not (exchange or currency) else 1),
        )
        if bare_ticker_lookup and listings:
            return sorted(listings, key=self._listing_identity_rank)[0]
        return listings[0] if listings else None

    def get_asset_by_identity(
        self, ticker=None, isin=None, exchange=None, currency=None, include_mappings=False
    ):
        """
        Récupère un actif via son ISIN ou une cotation ticker/exchange/currency.

        Args:
            ticker (str, optional): Symbole de marché.
            isin (str, optional): Identifiant ISIN.
            exchange (str, optional): Place de cotation.
            currency (str, optional): Devise de cotation.
            include_mappings (bool): Charge la relation Asset.mappings si nécessaire.

        Returns:
            Asset: L'objet Asset correspondant, ou None.
        """
        listing = self.get_listing_by_identity(
            ticker=ticker,
            isin=isin,
            exchange=exchange,
            currency=currency,
            include_mappings=include_mappings,
        )
        if listing:
            asset = listing.asset
            normalized_ticker = self._normalize_ticker(ticker)
            normalized_isin = self._normalize_ticker(isin)
            isin_only_lookup = (
                normalized_isin
                and not exchange
                and not currency
                and (not normalized_ticker or normalized_ticker == normalized_isin)
            )
            if isin_only_lookup:
                asset.selected_listing = (
                    self._preferred_listing_for_asset(
                        asset, asset.__dict__.get("listings") or [listing]
                    )
                    or listing
                )
            else:
                asset.selected_listing = listing
            asset.selected_mappings = [
                mapping
                for mapping in list(getattr(listing, "mappings", []) or [])
                + list(getattr(asset, "mappings", []) or [])
                if getattr(mapping, "asset_listing_id", None) in (None, asset.selected_listing.id)
            ]
            return asset

        session = self.get_session()
        try:
            query = session.query(Asset)
            if include_mappings:
                query = query.options(
                    joinedload(Asset.mappings),
                    joinedload(Asset.listings).joinedload(AssetListing.mappings),
                )

            normalized_isin = isin.strip().upper() if isin else None
            normalized_ticker = ticker.strip().upper() if ticker else None
            normalized_exchange = exchange.strip().upper() if exchange else None

            if normalized_isin:
                asset = query.filter(Asset.isin == normalized_isin).first()
                if asset:
                    return asset

            if normalized_ticker and normalized_exchange:
                return query.filter(
                    Asset.ticker == normalized_ticker, Asset.exchange == normalized_exchange
                ).first()

            if normalized_ticker:
                return (
                    query.filter(Asset.ticker == normalized_ticker)
                    .order_by(Asset.isin.desc(), Asset.exchange.asc())
                    .first()
                )

            return None
        except SQLAlchemyError as e:
            logger.error(
                f"Erreur lors de la récupération asset identity ticker={ticker}, isin={isin}, exchange={exchange}, currency={currency}: {e}"
            )
            return None
        finally:
            session.close()

    def get_asset_context(self, ticker=None, isin=None, exchange=None, currency=None):
        """
        Retourne un contexte complet instrument + cotation + mappings actifs.
        """
        asset = self.get_asset_by_identity(
            ticker=ticker, isin=isin, exchange=exchange, currency=currency, include_mappings=True
        )
        if not asset:
            return None

        listing = getattr(asset, "selected_listing", None)
        if not listing:
            listings = asset.__dict__.get("listings") or []
            listing = self._preferred_listing_for_asset(asset, listings)

        selected_mappings = getattr(asset, "selected_mappings", None)
        if selected_mappings is None:
            selected_mappings = [
                mapping
                for mapping in (asset.__dict__.get("mappings") or [])
                if getattr(mapping, "asset_listing_id", None)
                in (None, getattr(listing, "id", None))
            ]
            if listing:
                selected_mappings.extend(listing.__dict__.get("mappings") or [])

        deduped_mappings = {}
        for mapping in selected_mappings:
            if not getattr(mapping, "is_active", True):
                continue
            key = mapping.provider_name.lower()
            if key not in deduped_mappings or getattr(mapping, "asset_listing_id", None):
                deduped_mappings[key] = mapping

        return {
            "asset": asset,
            "listing": listing,
            "details": self._asset_to_dict(asset, listing),
            "mappings": deduped_mappings,
        }

    def get_asset_mappings(self, ticker=None, isin=None, exchange=None, currency=None):
        """Return an asset with its mapping relationships eagerly loaded.

        This compatibility query is intentionally thin: identity resolution
        remains centralized in :meth:`get_asset_by_identity`.
        """
        return self.get_asset_by_identity(
            ticker=ticker,
            isin=isin,
            exchange=exchange,
            currency=currency,
            include_mappings=True,
        )

    def get_asset_details(self, ticker=None, isin=None, exchange=None, currency=None):
        """
        Récupère les détails d'un actif (Asset) pour l'enrichissement.

        Args:
            ticker (str): Le symbole du ticker
            isin (str, optional): Code ISIN
            exchange (str, optional): Place de cotation

        Returns:
            dict: Dictionnaire avec les infos de l'asset ou None si pas trouvé
        """
        context = self.get_asset_context(
            ticker=ticker, isin=isin, exchange=exchange, currency=currency
        )
        return context["details"] if context else None

    def get_ticker_stats(self, ticker):
        """
        Récupère les statistiques pour un ticker spécifique.

        Args:
            ticker (str): Symbole du ticker

        Returns:
            tuple: (success, stats_dict, error_msg)
        """
        session = self.get_session()
        try:
            # Normalize ticker
            norm_ticker = ticker.strip().upper()

            # Find the asset
            asset = session.query(Asset).filter(func.upper(Asset.ticker) == norm_ticker).first()
            if not asset:
                return False, None, f"Ticker {ticker} non trouvé"

            # Query PriceEOD stats
            stats_row = (
                session.query(
                    func.min(PriceEOD.timestamp).label("earliest_date"),
                    func.max(PriceEOD.timestamp).label("latest_date"),
                    func.count(PriceEOD.timestamp).label("total_records"),
                )
                .filter(PriceEOD.asset_id == asset.id)
                .first()
            )

            if not stats_row or stats_row.total_records == 0:
                return False, None, f"Aucune donnée de prix trouvée pour {ticker}"

            # Query latest IngestLog
            latest_log = (
                session.query(IngestLog)
                .filter(func.upper(IngestLog.ticker) == norm_ticker)
                .order_by(desc(IngestLog.created_at))
                .first()
            )

            # Count endpoint requests containing this ticker
            request_count = (
                session.query(UsageLog).filter(UsageLog.endpoint.like(f"%/{ticker}%")).count()
            )

            stats = {
                "ticker": asset.ticker,
                "earliest_date": stats_row.earliest_date.date().isoformat()
                if stats_row.earliest_date
                else None,
                "latest_date": stats_row.latest_date.date().isoformat()
                if stats_row.latest_date
                else None,
                "total_records": stats_row.total_records,
                "last_sync_at": latest_log.created_at.isoformat() if latest_log else None,
                "last_sync_success": (latest_log.status == "success") if latest_log else True,
                "total_api_requests": request_count,
            }

            return True, stats, None

        except SQLAlchemyError as e:
            logger.error(f"Erreur lors de la récupération des stats du ticker {ticker}: {e}")
            return False, None, str(e)
        finally:
            session.close()
