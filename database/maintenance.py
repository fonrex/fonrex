"""Database statistics and retention operations."""

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import desc, func
from sqlalchemy.exc import SQLAlchemyError

from database.component import DatabaseComponent
from models import Asset, AssetListing, IngestLog, PriceEOD, PriceIntraday, UsageLog

logger = logging.getLogger(__name__)


class DatabaseMaintenance(DatabaseComponent):
    def get_model_counts(self):
        """Récupère les comptes des principaux modèles."""
        session = self.get_session()
        try:
            return {
                "prices_eod": session.query(PriceEOD).count(),
                "prices_intraday": session.query(PriceIntraday).count(),
                "assets": session.query(Asset).count(),
                "listings": session.query(AssetListing).count(),
                "ingest_logs": session.query(IngestLog).count(),
                "usage_logs": session.query(UsageLog).count(),
            }
        finally:
            session.close()

    def get_database_stats(self):
        """
        Récupère les statistiques de la base de données.

        Returns:
            tuple: (success, data_or_error_message)
                success (bool): True si succès, False sinon
                data (dict): Statistiques de la base de données
                error_msg (str): Message d'erreur si échec
        """
        session = self.get_session()
        try:
            total_records = session.query(PriceEOD).count()
            total_tickers = session.query(AssetListing).count()
            total_usage_logs = session.query(UsageLog).count()

            # Statistiques par endpoint (dernières 24h)
            yesterday = datetime.now(UTC) - timedelta(days=1)

            endpoint_stats = (
                session.query(UsageLog.endpoint, func.count(UsageLog.id).label("count"))
                .filter(UsageLog.created_at >= yesterday)
                .group_by(UsageLog.endpoint)
                .all()
            )

            # Top 5 tickers demandés par ingest_log (dernières 24h)
            top_ingests = (
                session.query(IngestLog.ticker, func.count(IngestLog.id).label("count"))
                .filter(IngestLog.created_at >= yesterday)
                .group_by(IngestLog.ticker)
                .order_by(desc("count"))
                .limit(5)
                .all()
            )

            stats = {
                "total_records": total_records,
                "total_tickers": total_tickers,
                "total_requests": total_usage_logs,
                "total_usage_logs": total_usage_logs,
                "sources_24h": {endpoint: count for endpoint, count in endpoint_stats},
                "top_tickers_24h": [{"ticker": t, "requests": c} for t, c in top_ingests],
            }

            return True, stats, None

        except SQLAlchemyError as e:
            logger.error(f"Erreur lors de la récupération des stats BDD: {e}")
            return False, None, str(e)
        finally:
            session.close()

    def get_cached_tickers(self):
        """
        Récupère la liste des tickers en cache avec leurs métadonnées.

        Returns:
            tuple: (success, data_or_error_message)
        """
        session = self.get_session()
        try:
            # Query min, max, count grouped by Asset.ticker
            results = (
                session.query(
                    Asset.ticker,
                    func.min(PriceEOD.timestamp).label("earliest_date"),
                    func.max(PriceEOD.timestamp).label("latest_date"),
                    func.count(PriceEOD.timestamp).label("total_records"),
                )
                .join(PriceEOD, PriceEOD.asset_id == Asset.id)
                .group_by(Asset.ticker)
                .order_by(Asset.ticker)
                .all()
            )

            # Query the latest IngestLog for each ticker to get last_sync_at and last_sync_success
            subq = (
                session.query(IngestLog.ticker, func.max(IngestLog.created_at).label("max_created"))
                .group_by(IngestLog.ticker)
                .subquery()
            )

            latest_logs = (
                session.query(IngestLog)
                .join(
                    subq,
                    (IngestLog.ticker == subq.c.ticker)
                    & (IngestLog.created_at == subq.c.max_created),
                )
                .all()
            )

            logs_by_ticker = {log.ticker.upper(): log for log in latest_logs}

            tickers_data = []
            for r in results:
                ticker_upper = r.ticker.upper()
                log = logs_by_ticker.get(ticker_upper)

                tickers_data.append(
                    {
                        "ticker": r.ticker,
                        "earliest_date": r.earliest_date.date().isoformat()
                        if r.earliest_date
                        else None,
                        "latest_date": r.latest_date.date().isoformat() if r.latest_date else None,
                        "total_records": r.total_records,
                        "last_sync_at": log.created_at.isoformat() if log else None,
                        "last_sync_success": (log.status == "success") if log else True,
                    }
                )

            return True, tickers_data, None
        except SQLAlchemyError as e:
            logger.error(f"Erreur lors de la récupération des tickers: {e}")
            return False, None, str(e)
        finally:
            session.close()

    def cleanup_old_data(self, days_to_keep=730):
        """
        Nettoie les anciennes données.

        Args:
            days_to_keep (int): Nombre de jours de données à conserver

        Returns:
            tuple: (success, result_dict, error_msg)
        """
        return self._cleanup_old_data(days_to_keep)

    def _cleanup_old_data(self, days_to_keep=730):
        """Implémentation interne du nettoyage."""
        session = self.get_session()
        try:
            cutoff_date = datetime.combine(
                date.today() - timedelta(days=days_to_keep), datetime.min.time()
            )

            # Supprimer les données anciennes de prices_eod
            deleted_count = (
                session.query(PriceEOD)
                .filter(PriceEOD.timestamp < cutoff_date)
                .delete(synchronize_session=False)
            )

            # Supprimer les logs anciens (garder 30 jours)
            log_cutoff = datetime.now(UTC) - timedelta(days=30)
            deleted_usage_logs = (
                session.query(UsageLog)
                .filter(UsageLog.created_at < log_cutoff)
                .delete(synchronize_session=False)
            )

            deleted_ingest_logs = (
                session.query(IngestLog)
                .filter(IngestLog.created_at < log_cutoff)
                .delete(synchronize_session=False)
            )

            session.commit()

            logger.info(
                f"🧹 Nettoyage BDD: {deleted_count} prix EOD, {deleted_usage_logs} logs d'usage, et {deleted_ingest_logs} logs d'ingestion supprimés"
            )

            return (
                True,
                {
                    "status": "success",
                    "deleted_records": deleted_count,
                    "deleted_requests": deleted_usage_logs,
                    "deleted_usage_logs": deleted_usage_logs,
                    "deleted_ingest_logs": deleted_ingest_logs,
                    "cutoff_date": cutoff_date.date().isoformat(),
                },
                None,
            )

        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Erreur lors du nettoyage BDD: {e}")
            return False, None, str(e)
        finally:
            session.close()
