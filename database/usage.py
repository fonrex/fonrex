"""Usage analytics persistence."""

import logging
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError

from database.component import DatabaseComponent
from models import UsageLog, naive_utc_now

logger = logging.getLogger(__name__)

# Columns an entry of the usage log may set.
USAGE_FIELDS = (
    "endpoint",
    "method",
    "status_code",
    "latency_ms",
    "api_key_id",
    "provider_used",
    "cache_hit",
    "cost_bucket",
    "ip_address",
    "user_agent",
)

# Longest request path the endpoint column holds: a longer one would make the
# database reject the whole batch.
MAX_ENDPOINT_LENGTH = UsageLog.__table__.c.endpoint.type.length


def _usage_columns(entry):
    """Return the columns of a usage row, the endpoint capped to its column."""
    columns = {field: entry[field] for field in USAGE_FIELDS if field in entry}
    endpoint = columns.get("endpoint")
    if isinstance(endpoint, str):
        columns["endpoint"] = endpoint[:MAX_ENDPOINT_LENGTH]
    return columns


class UsageRepository(DatabaseComponent):
    def log_usage(
        self,
        endpoint,
        method,
        status_code,
        latency_ms,
        api_key_id=None,
        provider_used=None,
        cache_hit=False,
        cost_bucket=None,
        ip_address=None,
        user_agent=None,
    ):
        """
        Enregistre un appel API dans le journal d'usage local de l'instance.
        """
        session = self.get_session()
        try:
            usage_log = UsageLog(
                api_key_id=api_key_id,
                endpoint=_usage_columns({"endpoint": endpoint})["endpoint"],
                method=method,
                provider_used=provider_used,
                cache_hit=cache_hit,
                status_code=status_code,
                latency_ms=latency_ms,
                cost_bucket=cost_bucket,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            session.add(usage_log)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Erreur lors du logging usage: {e}")
            return False
        finally:
            session.close()

    def log_usage_batch(self, entries):
        """Write several usage entries in one transaction. Return the number written."""
        if not entries:
            return 0
        session = self.get_session()
        try:
            session.add_all(UsageLog(**_usage_columns(entry)) for entry in entries)
            session.commit()
            return len(entries)
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Erreur lors du logging usage (lot de {len(entries)}): {e}")
            return 0
        finally:
            session.close()

    def purge_older_than(self, days):
        """Delete the usage rows older than ``days`` days. Return the number deleted."""
        if days <= 0:
            return 0
        cutoff = naive_utc_now() - timedelta(days=days)
        session = self.get_session()
        try:
            deleted = (
                session.query(UsageLog)
                .filter(UsageLog.created_at < cutoff)
                .delete(synchronize_session=False)
            )
            session.commit()
            return deleted
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Erreur lors de la purge du journal d'usage: {e}")
            return 0
        finally:
            session.close()
