"""Read models for standard and deep fundamental data."""

import logging
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from database.component import DatabaseComponent
from models import (
    AnalystRatings,
    Asset,
    EarningsHistory,
    EarningsTrend,
    ESGScores,
    ETFDetails,
    ETFHolding,
    FinancialStatement,
    Fundamental,
    FundamentalsHighlights,
    OutstandingSharesHistory,
)

logger = logging.getLogger(__name__)


def _decimal_to_float(value):
    return float(value) if isinstance(value, Decimal) else value


class FundamentalsRepository(DatabaseComponent):
    def get_fundamental_data(self, asset_id: int) -> dict | None:
        """
        Retourne les données fondamentales d'un actif.

        Priorité : fundamentals_highlights (nouvelle table) > fundamentals (legacy).
        Format de retour compatible avec les endpoints existants (/fundamental).

        Args:
            asset_id: ID de l'actif dans la table assets

        Returns:
            dict avec les clés market_cap, pe_ratio, dividend_yield, extra_metrics
            ou None si aucune donnée trouvée.
        """
        session = self.get_session()
        try:
            # 1. Essayer fundamentals_highlights en premier
            highlight = (
                session.query(FundamentalsHighlights)
                .filter_by(asset_id=asset_id)
                .order_by(FundamentalsHighlights.fetched_at.desc())
                .first()
            )

            if highlight:
                return {
                    "asset_id": asset_id,
                    "timestamp": highlight.fetched_at,
                    "market_cap": float(highlight.market_cap) if highlight.market_cap else None,
                    "pe_ratio": float(highlight.pe_ratio) if highlight.pe_ratio else None,
                    "dividend_yield": float(highlight.dividend_yield)
                    if highlight.dividend_yield
                    else None,
                    "extra_metrics": {
                        "roe": float(highlight.roe) if highlight.roe else None,
                        "roa": float(highlight.roa) if highlight.roa else None,
                        "forward_pe": float(highlight.pe_forward) if highlight.pe_forward else None,
                        "pb_ratio": float(highlight.pb_ratio) if highlight.pb_ratio else None,
                        "ps_ratio": float(highlight.ps_ratio) if highlight.ps_ratio else None,
                        "ev_ebitda": float(highlight.ev_ebitda) if highlight.ev_ebitda else None,
                        "net_margin": float(highlight.net_margin) if highlight.net_margin else None,
                        "beta": float(highlight.beta) if highlight.beta else None,
                        "52_week_high": float(highlight.week_52_high)
                        if highlight.week_52_high
                        else None,
                        "52_week_low": float(highlight.week_52_low)
                        if highlight.week_52_low
                        else None,
                        "enterprise_value": float(highlight.enterprise_value)
                        if highlight.enterprise_value
                        else None,
                        "eps_trailing": float(highlight.eps_trailing)
                        if highlight.eps_trailing
                        else None,
                        "eps_forward": float(highlight.eps_forward)
                        if highlight.eps_forward
                        else None,
                        "peg_ratio": float(highlight.peg_ratio) if highlight.peg_ratio else None,
                        "source": highlight.source,
                    },
                    "source": "highlights",
                }

            # 2. Fallback sur la table fundamentals legacy
            fundamental = (
                session.query(Fundamental)
                .filter_by(asset_id=asset_id)
                .order_by(Fundamental.timestamp.desc())
                .first()
            )

            if fundamental:
                return {
                    "asset_id": asset_id,
                    "timestamp": fundamental.timestamp,
                    "market_cap": fundamental.market_cap,
                    "pe_ratio": fundamental.pe_ratio,
                    "dividend_yield": fundamental.dividend_yield,
                    "extra_metrics": fundamental.extra_metrics or {},
                    "source": "legacy",
                }

            return None

        except SQLAlchemyError as e:
            logger.error(f"Erreur get_fundamental_data asset_id={asset_id}: {e}")
            return None
        finally:
            session.close()

    def get_deep_fundamentals(self, asset_id: int) -> dict | None:
        """
        Retourne l'intégralité des données fondamentales structurées d'un actif.

        Sections retournées :
            - highlights          : métriques clés (FundamentalsHighlights)
            - etf_holdings        : top holdings ETF (si quote_type == 'ETF')
            - esg_scores          : scores ESG et controverses
            - earnings_trend      : estimations futures
            - outstanding_shares  : historique du nombre d'actions

        Args:
            asset_id: ID de l'actif dans la table assets

        Returns:
            dict structuré ou None si l'actif est introuvable.
        """
        session = self.get_session()
        try:
            # Vérifier que l'actif existe
            asset = session.query(Asset).filter_by(id=asset_id).first()
            if not asset:
                return None

            result = {
                "asset_id": asset_id,
                "ticker": asset.ticker,
                "quote_type": asset.quote_type,
            }

            # ── Highlights ────────────────────────────────────────────────────
            highlight = (
                session.query(FundamentalsHighlights)
                .filter_by(asset_id=asset_id)
                .order_by(FundamentalsHighlights.fetched_at.desc())
                .first()
            )
            if highlight:
                result["highlights"] = {
                    col.name: (
                        float(getattr(highlight, col.name))
                        if getattr(highlight, col.name) is not None
                        and hasattr(getattr(highlight, col.name), "__float__")
                        else getattr(highlight, col.name)
                    )
                    for col in FundamentalsHighlights.__table__.columns
                    if col.name not in ("id", "asset_id")
                }
            else:
                result["highlights"] = None

            # ── Financial Statements ──────────────────────────────────────────
            all_stmts = (
                session.query(FinancialStatement)
                .filter_by(asset_id=asset_id)
                .order_by(FinancialStatement.period_end.desc())
                .all()
            )
            # Note: on aplatit en liste plate pour le formateur flexible
            statements_list = []
            for stmt in all_stmts:
                row = {
                    "statement_type": stmt.statement_type,
                    "period_end": stmt.period_end.isoformat() if stmt.period_end else None,
                    "period_type": stmt.period_type,
                    "currency": stmt.currency,
                }
                for col in FinancialStatement.__table__.columns:
                    if col.name in (
                        "id",
                        "asset_id",
                        "statement_type",
                        "period_type",
                        "period_end",
                        "currency",
                        "fetched_at",
                    ):
                        continue
                    val = getattr(stmt, col.name)
                    if val is not None:
                        try:
                            row[col.name] = float(val)
                        except (TypeError, ValueError):
                            row[col.name] = val
                statements_list.append(row)
            result["statements"] = statements_list

            # ── Earnings History (10 derniers) ────────────────────────────────
            earnings = (
                session.query(EarningsHistory)
                .filter_by(asset_id=asset_id)
                .order_by(EarningsHistory.period.desc())
                .limit(10)
                .all()
            )
            result["earnings_history"] = [
                {
                    "period": e.period,
                    "period_end": e.period_end.isoformat() if e.period_end else None,
                    "eps_actual": float(e.eps_actual) if e.eps_actual else None,
                    "eps_estimate": float(e.eps_estimate) if e.eps_estimate else None,
                    "surprise": float(e.surprise) if e.surprise else None,
                    "surprise_pct": float(e.surprise_pct) if e.surprise_pct else None,
                }
                for e in earnings
            ]

            # ── Analyst Ratings ───────────────────────────────────────────────
            rating = session.query(AnalystRatings).filter_by(asset_id=asset_id).first()
            if rating:
                result["analyst_ratings"] = {
                    col.name: (
                        float(getattr(rating, col.name))
                        if getattr(rating, col.name) is not None
                        and hasattr(getattr(rating, col.name), "__float__")
                        and not isinstance(getattr(rating, col.name), int)
                        else getattr(rating, col.name)
                    )
                    for col in AnalystRatings.__table__.columns
                    if col.name not in ("id", "asset_id")
                }
            else:
                result["analyst_ratings"] = None

            # ── ETF-specific data ─────────────────────────────────────────────
            is_etf = (asset.quote_type or "").upper() == "ETF"
            if is_etf:
                etf = session.query(ETFDetails).filter_by(asset_id=asset_id).first()
                if etf:
                    result["etf_details"] = {
                        col.name: (
                            float(getattr(etf, col.name))
                            if getattr(etf, col.name) is not None
                            and hasattr(getattr(etf, col.name), "__float__")
                            and not isinstance(getattr(etf, col.name), (bool, int))
                            else getattr(etf, col.name)
                        )
                        for col in ETFDetails.__table__.columns
                        if col.name not in ("id", "asset_id")
                    }
                else:
                    result["etf_details"] = None

                holdings = (
                    session.query(ETFHolding)
                    .filter_by(etf_asset_id=asset_id)
                    .order_by(ETFHolding.weight.desc())
                    .all()
                )
                result["etf_holdings"] = [
                    {
                        "holding_ticker": h.holding_ticker,
                        "holding_isin": h.holding_isin,
                        "holding_name": h.holding_name,
                        "weight": float(h.weight) if h.weight else None,
                        "sector": h.sector,
                        "country": h.country,
                    }
                    for h in holdings
                ]
            else:
                result["etf_details"] = None
                result["etf_holdings"] = []

            def _val(v):
                if (
                    v is not None
                    and hasattr(v, "__float__")
                    and not isinstance(v, (bool, int, str))
                ):
                    return float(v)
                return v

            # ── ESG Scores (Phase 4) ──────────────────────────────────────────
            esg = session.query(ESGScores).filter_by(asset_id=asset_id).first()
            if esg:
                result["esg_scores"] = {
                    col.name: _val(getattr(esg, col.name))
                    for col in ESGScores.__table__.columns
                    if col.name not in ("id", "asset_id")
                }
            else:
                result["esg_scores"] = None

            # ── Earnings Trend (Phase 4) ──────────────────────────────────────
            trends = session.query(EarningsTrend).filter_by(asset_id=asset_id).all()
            result["earnings_trend"] = [
                {
                    col.name: _val(getattr(t, col.name))
                    for col in EarningsTrend.__table__.columns
                    if col.name not in ("id", "asset_id")
                }
                for t in trends
            ]

            # ── Outstanding Shares (Phase 4) ──────────────────────────────────
            shares = (
                session.query(OutstandingSharesHistory)
                .filter_by(asset_id=asset_id)
                .order_by(OutstandingSharesHistory.date.desc())
                .limit(100)
                .all()
            )
            result["outstanding_shares"] = [
                {
                    col.name: _val(getattr(s, col.name))
                    for col in OutstandingSharesHistory.__table__.columns
                    if col.name not in ("id", "asset_id")
                }
                for s in shares
            ]

            return result

        except SQLAlchemyError as e:
            logger.error(f"Erreur get_deep_fundamentals asset_id={asset_id}: {e}")
            return None
        finally:
            session.close()

    def get_deep_sections(
        self,
        asset_id: int,
        requested_sections: set[str],
        want_all: bool,
    ) -> dict:
        """Return the section-oriented read model used by the deep endpoint."""
        response = {}
        session = self.get_session()
        try:
            if want_all or "highlights" in requested_sections:
                highlight = (
                    session.query(FundamentalsHighlights)
                    .filter_by(asset_id=asset_id)
                    .order_by(FundamentalsHighlights.fetched_at.desc())
                    .first()
                )
                if highlight:
                    response["highlights"] = {
                        column.name: _decimal_to_float(getattr(highlight, column.name))
                        for column in FundamentalsHighlights.__table__.columns
                        if column.name not in ("id", "asset_id", "fetched_at")
                    }

            if want_all or "statements" in requested_sections:
                statements_data = {
                    "income": {"annual": [], "quarterly": []},
                    "balance": {"annual": [], "quarterly": []},
                    "cashflow": {"annual": [], "quarterly": []},
                }
                statements = (
                    session.query(FinancialStatement)
                    .filter_by(asset_id=asset_id)
                    .order_by(FinancialStatement.period_end.desc())
                    .all()
                )
                for statement in statements:
                    statement_type = statement.statement_type
                    period_type = statement.period_type
                    if statement_type not in statements_data or period_type not in (
                        "annual",
                        "quarterly",
                    ):
                        continue
                    row = {
                        "period_end": (
                            statement.period_end.isoformat() if statement.period_end else None
                        )
                    }
                    for column in FinancialStatement.__table__.columns:
                        if column.name in (
                            "id",
                            "asset_id",
                            "period_type",
                            "statement_type",
                            "period_end",
                            "fetched_at",
                        ):
                            continue
                        value = getattr(statement, column.name)
                        if value is not None:
                            row[column.name] = _decimal_to_float(value)
                    statements_data[statement_type][period_type].append(row)
                response["statements"] = statements_data

            if want_all or "earnings" in requested_sections:
                earnings = (
                    session.query(EarningsHistory)
                    .filter_by(asset_id=asset_id)
                    .order_by(EarningsHistory.period.desc())
                    .all()
                )
                response["earnings_history"] = [
                    {
                        "period": earning.period,
                        "eps_actual": _decimal_to_float(earning.eps_actual),
                        "eps_estimate": _decimal_to_float(earning.eps_estimate),
                        "surprise_pct": _decimal_to_float(earning.surprise_pct),
                    }
                    for earning in earnings
                ]

            if want_all or "ratings" in requested_sections:
                rating = (
                    session.query(AnalystRatings)
                    .filter_by(asset_id=asset_id)
                    .order_by(AnalystRatings.fetched_at.desc())
                    .first()
                )
                if rating:
                    response["analyst_ratings"] = {
                        column.name: _decimal_to_float(getattr(rating, column.name))
                        for column in AnalystRatings.__table__.columns
                        if column.name not in ("id", "asset_id", "fetched_at")
                    }
        finally:
            session.close()
        return response
