"""Usage analytics persistence."""

import logging

from sqlalchemy.exc import SQLAlchemyError

from database.component import DatabaseComponent
from models import UsageLog

logger = logging.getLogger(__name__)


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
        Enregistre un appel API pour l'analytics d'usage et la future facturation.
        """
        session = self.get_session()
        try:
            usage_log = UsageLog(
                api_key_id=api_key_id,
                endpoint=endpoint,
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
