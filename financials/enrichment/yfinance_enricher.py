"""
YFinanceEnricher — récupère et structure toutes les données fondamentales
profondes disponibles via yfinance pour un actif donné.

Utilisation :
    enricher = YFinanceEnricher(db_service)
    await enricher.enrich(asset_id=42, ticker="AIR.PA")
"""

import asyncio
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import pandas as pd
import yfinance as yf

from concurrency import run_sync

logger = logging.getLogger(__name__)

# Semaphore global pour le rate limiting yfinance (max 2 requêtes/seconde)
_yfinance_semaphore = asyncio.Semaphore(2)


def _to_decimal(value) -> Decimal | None:
    """Convertit une valeur en Decimal, retourne None si impossible."""
    if value is None:
        return None
    if isinstance(value, float) and (pd.isna(value) or math.isinf(value)):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_int(value) -> int | None:
    """Convertit une valeur en int, retourne None si impossible."""
    if value is None:
        return None
    if isinstance(value, float) and (pd.isna(value) or math.isinf(value)):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _safe_get(source, key, converter=_to_decimal):
    """Extraction défensive avec conversion."""
    if isinstance(source, dict) or isinstance(source, pd.Series):
        value = source.get(key)
    else:
        value = None
    return converter(value) if value is not None else None


def _safe_date(value):
    """Convertit un timestamp pandas/datetime en date Python."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError, OverflowError):
        return None


class YFinanceEnricher:
    """
    Enrichit un actif avec les données fondamentales profondes de yfinance.
    Toutes les méthodes privées sont synchrones (exécutées dans un thread pool).
    """

    def __init__(self, db_service):
        """
        Args:
            db_service: DatabaseService instance (pour obtenir des sessions).
        """
        self.db_service = db_service

    async def enrich(self, asset_id: int, ticker: str) -> dict:
        """Point d'entrée principal — orchestre tous les enrichissements."""
        async with _yfinance_semaphore:
            yf_ticker = await run_sync(yf.Ticker, ticker)

        operations = (
            self._fetch_highlights,
            self._fetch_statements,
            self._fetch_earnings,
            self._fetch_ratings,
            self._fetch_esg,
            self._fetch_earnings_trend,
            self._fetch_outstanding_shares,
            self._fetch_gics,
        )

        # SQLite's StaticPool shares one connection across threads and cannot
        # safely handle concurrent writes. PostgreSQL keeps the faster parallel
        # path used in production.
        is_sqlite = await run_sync(self._database_is_sqlite)

        if is_sqlite:
            results = []
            for operation in operations:
                try:
                    results.append(await run_sync(operation, asset_id, yf_ticker))
                except Exception as exc:
                    results.append(exc)
        else:
            results = await asyncio.gather(
                *(run_sync(operation, asset_id, yf_ticker) for operation in operations),
                return_exceptions=True,
            )

        summary = {
            "highlights": not isinstance(results[0], Exception),
            "statements": not isinstance(results[1], Exception),
            "earnings": not isinstance(results[2], Exception),
            "ratings": not isinstance(results[3], Exception),
            "esg": not isinstance(results[4], Exception),
            "earnings_trend": not isinstance(results[5], Exception),
            "shares_history": not isinstance(results[6], Exception),
            "gics": not isinstance(results[7], Exception),
            "errors": [str(r) for r in results if isinstance(r, Exception)],
        }

        if summary["errors"]:
            logger.warning(
                "Enrichissement yfinance %s (asset_id=%s) — erreurs: %s",
                ticker,
                asset_id,
                summary["errors"],
            )
        else:
            logger.info(
                "✅ Enrichissement yfinance complet pour %s (asset_id=%s)",
                ticker,
                asset_id,
            )

        return summary

    def _database_is_sqlite(self):
        session = self.db_service.get_session()
        try:
            return session.get_bind().dialect.name == "sqlite"
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Highlights (t.info)
    # ------------------------------------------------------------------

    def _fetch_highlights(self, asset_id: int, t: yf.Ticker) -> None:
        """Extrait les métriques clés depuis t.info et upsert en base."""
        try:
            info = t.info or {}
            if not info or len(info) <= 1:
                logger.info("Pas de données info pour asset_id=%s", asset_id)
                return

            values = {
                "asset_id": asset_id,
                "fetched_at": datetime.now(timezone.utc),
                # Valorisation
                "market_cap": _to_decimal(info.get("marketCap")),
                "enterprise_value": _to_decimal(info.get("enterpriseValue")),
                "pe_ratio": _to_decimal(info.get("trailingPE")),
                "pe_forward": _to_decimal(info.get("forwardPE")),
                "pb_ratio": _to_decimal(info.get("priceToBook")),
                "ps_ratio": _to_decimal(info.get("priceToSalesTrailing12Months")),
                "peg_ratio": _to_decimal(info.get("pegRatio")),
                "ev_ebitda": _to_decimal(info.get("enterpriseToEbitda")),
                "ev_revenue": _to_decimal(info.get("enterpriseToRevenue")),
                # Rentabilité
                "roe": _to_decimal(info.get("returnOnEquity")),
                "roa": _to_decimal(info.get("returnOnAssets")),
                "roic": _to_decimal(info.get("returnOnCapital")),
                "net_margin": _to_decimal(info.get("profitMargins")),
                "operating_margin": _to_decimal(info.get("operatingMargins")),
                "gross_margin": _to_decimal(info.get("grossMargins")),
                # Par action
                "eps_trailing": _to_decimal(info.get("trailingEps")),
                "eps_forward": _to_decimal(info.get("forwardEps")),
                "book_value_per_share": _to_decimal(info.get("bookValue")),
                "revenue_per_share": _to_decimal(info.get("revenuePerShare")),
                # Dividende
                "dividend_yield": _to_decimal(info.get("dividendYield")),
                "dividend_rate": _to_decimal(info.get("dividendRate")),
                "dividend_ex_date": _safe_date(info.get("exDividendDate")),
                "payout_ratio": _to_decimal(info.get("payoutRatio")),
                # Technique
                "beta": _to_decimal(info.get("beta")),
                "week_52_high": _to_decimal(info.get("fiftyTwoWeekHigh")),
                "week_52_low": _to_decimal(info.get("fiftyTwoWeekLow")),
                "ma_50": _to_decimal(info.get("fiftyDayAverage")),
                "ma_200": _to_decimal(info.get("twoHundredDayAverage")),
                # Ownership
                "shares_outstanding": _to_int(info.get("sharesOutstanding")),
                "float_shares": _to_int(info.get("floatShares")),
                "pct_insiders": _to_decimal(info.get("heldPercentInsiders")),
                "pct_institutions": _to_decimal(info.get("heldPercentInstitutions")),
                # Short selling
                "shares_short": _to_int(info.get("sharesShort")),
                "shares_short_prior": _to_int(info.get("sharesShortPriorMonth")),
                "short_ratio": _to_decimal(info.get("shortRatio")),
                "short_percent_float": _to_decimal(info.get("shortPercentOfFloat")),
                "short_percent_outstanding": _to_decimal(info.get("sharesPercentSharesOut")),
                "shares_short_date": _safe_date(info.get("dateShortInterest")),
                # TTM et croissance
                "gross_profit_ttm": _to_decimal(info.get("grossProfits")),
                "diluted_eps_ttm": _to_decimal(info.get("trailingEps")),
                "return_on_assets_ttm": _to_decimal(info.get("returnOnAssets")),
                "return_on_equity_ttm": _to_decimal(info.get("returnOnEquity")),
                "quarterly_revenue_growth_yoy": _to_decimal(info.get("revenueGrowth")),
                "quarterly_earnings_growth_yoy": _to_decimal(info.get("earningsGrowth")),
                "revenue_ttm": _to_decimal(info.get("totalRevenue")),
                "ebitda_ttm": _to_decimal(info.get("ebitda")),
            }

            self._upsert_highlights(values)
            logger.debug("Highlights upserted pour asset_id=%s", asset_id)

        except Exception as e:
            logger.error("Erreur _fetch_highlights asset_id=%s: %s", asset_id, e)
            raise

    def _upsert_highlights(self, values: dict) -> None:
        """Upsert via INSERT ... ON CONFLICT pour les highlights."""
        from models import FundamentalsHighlights

        session = self.db_service.get_session()
        try:
            # Chercher un enregistrement existant pour cet asset
            existing = (
                session.query(FundamentalsHighlights).filter_by(asset_id=values["asset_id"]).first()
            )

            if existing:
                for key, value in values.items():
                    if key != "asset_id":
                        setattr(existing, key, value)
            else:
                obj = FundamentalsHighlights(**values)
                session.add(obj)

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Financial Statements (income_stmt, balance_sheet, cashflow)
    # ------------------------------------------------------------------

    # Mapping des noms de lignes yfinance vers les colonnes du modèle
    INCOME_MAPPING = {
        "Total Revenue": "revenue",
        "Gross Profit": "gross_profit",
        "EBITDA": "ebitda",
        "Operating Income": "operating_income",
        "Net Income": "net_income",
        "Basic EPS": "eps_basic",
        "Diluted EPS": "eps_diluted",
    }

    BALANCE_MAPPING = {
        "Total Assets": "total_assets",
        "Total Liabilities Net Minority Interest": "total_liabilities",
        "Total Equity Gross Minority Interest": "total_equity",
        "Total Debt": "total_debt",
        "Cash And Cash Equivalents": "cash_and_equivalents",
        "Cash Cash Equivalents And Short Term Investments": "cash_and_equivalents",
    }

    CASHFLOW_MAPPING = {
        "Operating Cash Flow": "operating_cashflow",
        "Investing Cash Flow": "investing_cashflow",
        "Financing Cash Flow": "financing_cashflow",
        "Free Cash Flow": "free_cashflow",
        "Capital Expenditure": "capex",
    }

    def _fetch_statements(self, asset_id: int, t: yf.Ticker) -> None:
        """Extrait les 3 états financiers × 2 périodicités."""
        try:
            statement_configs = [
                ("income", "annual", t.income_stmt, self.INCOME_MAPPING),
                ("income", "quarterly", t.quarterly_income_stmt, self.INCOME_MAPPING),
                ("balance", "annual", t.balance_sheet, self.BALANCE_MAPPING),
                ("balance", "quarterly", t.quarterly_balance_sheet, self.BALANCE_MAPPING),
                ("cashflow", "annual", t.cashflow, self.CASHFLOW_MAPPING),
                ("cashflow", "quarterly", t.quarterly_cashflow, self.CASHFLOW_MAPPING),
            ]

            count = 0
            for statement_type, period_type, df, mapping in statement_configs:
                if df is None or df.empty:
                    continue
                count += self._process_statement_df(
                    asset_id, statement_type, period_type, df, mapping
                )

            logger.debug("Statements upserted pour asset_id=%s: %d périodes", asset_id, count)

        except Exception as e:
            logger.error("Erreur _fetch_statements asset_id=%s: %s", asset_id, e)
            raise

    def _process_statement_df(
        self,
        asset_id: int,
        statement_type: str,
        period_type: str,
        df: pd.DataFrame,
        mapping: dict,
    ) -> int:
        """Traite un DataFrame de financial statement et upsert les données."""
        from models import FinancialStatement

        session = self.db_service.get_session()
        count = 0
        try:
            # Les colonnes du DataFrame sont les dates de fin de période
            for period_date in df.columns:
                period_end = _safe_date(period_date)
                if not period_end:
                    continue

                values = {
                    "asset_id": asset_id,
                    "statement_type": statement_type,
                    "period_type": period_type,
                    "period_end": period_end,
                    "fetched_at": datetime.now(timezone.utc),
                }

                # Extraire les métriques depuis les index du DataFrame
                column_data = df[period_date]
                for yf_key, model_col in mapping.items():
                    if model_col in values:
                        # Ne pas écraser si déjà renseigné (ex: cash_and_equivalents avec fallback)
                        if values[model_col] is not None:
                            continue
                    val = _to_decimal(column_data.get(yf_key))
                    values[model_col] = val

                # Upsert
                existing = (
                    session.query(FinancialStatement)
                    .filter_by(
                        asset_id=asset_id,
                        statement_type=statement_type,
                        period_type=period_type,
                        period_end=period_end,
                    )
                    .first()
                )

                if existing:
                    for key, value in values.items():
                        if key not in ("asset_id", "statement_type", "period_type", "period_end"):
                            setattr(existing, key, value)
                else:
                    obj = FinancialStatement(**values)
                    session.add(obj)

                count += 1

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        return count

    # ------------------------------------------------------------------
    # Earnings History
    # ------------------------------------------------------------------

    def _fetch_earnings(self, asset_id: int, t: yf.Ticker) -> None:
        """Extrait l'historique EPS actual vs estimate."""
        try:
            from models import EarningsHistory

            # yfinance expose earnings_history comme un DataFrame
            try:
                eh = t.earnings_history
            except Exception:
                eh = None

            if eh is None or (isinstance(eh, pd.DataFrame) and eh.empty):
                logger.debug("Pas d'earnings history pour asset_id=%s", asset_id)
                return

            session = self.db_service.get_session()
            try:
                count = 0

                if isinstance(eh, pd.DataFrame):
                    for _, row in eh.iterrows():
                        # Construire le label de période
                        quarter = row.get("quarter")
                        period_end = row.get("period") or row.get("reportedDate")

                        if quarter:
                            period_label = str(quarter)
                        elif period_end:
                            dt = _safe_date(period_end)
                            if dt:
                                q = (dt.month - 1) // 3 + 1
                                period_label = f"{dt.year}Q{q}"
                            else:
                                continue
                        else:
                            continue

                        values = {
                            "asset_id": asset_id,
                            "period": period_label,
                            "eps_actual": _to_decimal(row.get("epsActual")),
                            "eps_estimate": _to_decimal(row.get("epsEstimate")),
                            "surprise_pct": _to_decimal(row.get("surprisePercent")),
                            "fetched_at": datetime.now(timezone.utc),
                        }

                        existing = (
                            session.query(EarningsHistory)
                            .filter_by(asset_id=asset_id, period=period_label)
                            .first()
                        )

                        if existing:
                            for key, value in values.items():
                                if key not in ("asset_id", "period"):
                                    setattr(existing, key, value)
                        else:
                            obj = EarningsHistory(**values)
                            session.add(obj)

                        count += 1

                session.commit()
                logger.debug(
                    "Earnings upserted pour asset_id=%s: %d périodes",
                    asset_id,
                    count,
                )
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        except Exception as e:
            logger.error("Erreur _fetch_earnings asset_id=%s: %s", asset_id, e)
            raise

    # ------------------------------------------------------------------
    # Analyst Ratings
    # ------------------------------------------------------------------

    def _fetch_ratings(self, asset_id: int, t: yf.Ticker) -> None:
        """Extrait le consensus analystes et l'objectif de cours."""
        try:
            from models import AnalystRatings

            # Objectifs de prix
            try:
                targets = t.analyst_price_targets
            except Exception:
                targets = None

            # Recommandations
            try:
                recs = t.recommendations
            except Exception:
                recs = None

            # Extraire les données de prix cibles
            target_values = {}
            if targets is not None:
                if isinstance(targets, dict):
                    target_values = {
                        "target_mean": _to_decimal(targets.get("current")),
                        "target_low": _to_decimal(targets.get("low")),
                        "target_high": _to_decimal(targets.get("high")),
                        "target_median": _to_decimal(targets.get("mean") or targets.get("median")),
                        "nb_analysts": _to_int(targets.get("numberOfAnalystOpinions")),
                    }
                elif isinstance(targets, pd.DataFrame) and not targets.empty:
                    # Certaines versions de yfinance retournent un DataFrame
                    row = targets.iloc[-1] if len(targets) > 0 else None
                    if row is not None:
                        target_values = {
                            "target_mean": _to_decimal(row.get("current")),
                            "target_low": _to_decimal(row.get("low")),
                            "target_high": _to_decimal(row.get("high")),
                            "target_median": _to_decimal(row.get("mean") or row.get("median")),
                        }

            # Extraire les recommandations (Strong Buy, Buy, Hold, Sell, Strong Sell)
            rec_values = {}
            consensus = None
            if recs is not None and isinstance(recs, pd.DataFrame) and not recs.empty:
                # Prendre la ligne la plus récente
                latest = recs.iloc[-1] if len(recs) > 0 else None
                if latest is not None:
                    rec_values = {
                        "strong_buy": _to_int(latest.get("strongBuy")),
                        "buy": _to_int(latest.get("buy")),
                        "hold": _to_int(latest.get("hold")),
                        "sell": _to_int(latest.get("sell")),
                        "strong_sell": _to_int(latest.get("strongSell")),
                    }

                    # Déterminer le consensus à partir des votes
                    votes = {
                        "Strong Buy": rec_values.get("strong_buy") or 0,
                        "Buy": rec_values.get("buy") or 0,
                        "Hold": rec_values.get("hold") or 0,
                        "Sell": rec_values.get("sell") or 0,
                        "Strong Sell": rec_values.get("strong_sell") or 0,
                    }
                    if any(v > 0 for v in votes.values()):
                        consensus = max(votes, key=votes.get)

            if not target_values and not rec_values:
                logger.debug("Pas de ratings pour asset_id=%s", asset_id)
                return

            values = {
                "asset_id": asset_id,
                "fetched_at": datetime.now(timezone.utc),
                "consensus": consensus,
                **target_values,
                **rec_values,
            }

            session = self.db_service.get_session()
            try:
                existing = session.query(AnalystRatings).filter_by(asset_id=asset_id).first()

                if existing:
                    for key, value in values.items():
                        if key != "asset_id":
                            setattr(existing, key, value)
                else:
                    obj = AnalystRatings(**values)
                    session.add(obj)

                session.commit()
                logger.debug("Ratings upserted pour asset_id=%s", asset_id)
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        except Exception as e:
            logger.error("Erreur _fetch_ratings asset_id=%s: %s", asset_id, e)
            raise

    # ------------------------------------------------------------------
    # ESG Scores
    # ------------------------------------------------------------------

    def _fetch_esg(self, asset_id: int, t: yf.Ticker) -> None:
        """Extrait les données ESG (sustainability)."""
        try:
            from models import ESGScores

            esg = t.sustainability
            if esg is None or (isinstance(esg, pd.DataFrame) and esg.empty):
                return

            # yfinance retourne un DataFrame avec les labels en index
            data = esg.to_dict()[0] if isinstance(esg, pd.DataFrame) else {}

            values = {
                "asset_id": asset_id,
                "total_esg": _to_decimal(data.get("totalEsg")),
                "environment_score": _to_decimal(data.get("environmentScore")),
                "social_score": _to_decimal(data.get("socialScore")),
                "governance_score": _to_decimal(data.get("governanceScore")),
                "controversy_level": _to_int(data.get("highestControversy")),
                "peer_group": data.get("peerGroup"),
                "esg_risk_rating": data.get("ratingCategory"),
                "adult": data.get("adult"),
                "alcoholic": data.get("alcoholic"),
                "animal_testing": data.get("animalTesting"),
                "catholic": data.get("catholic"),
                "controversial_weapons": data.get("controversialWeapons"),
                "small_arms": data.get("smallArms"),
                "fur_leather": data.get("furLeather"),
                "gambling": data.get("gambling"),
                "gmo": data.get("gmo"),
                "military_contract": data.get("militaryContract"),
                "nuclear": data.get("nuclear"),
                "pesticides": data.get("pesticides"),
                "palm_oil": data.get("palmOil"),
                "coal": data.get("coal"),
                "tobacco": data.get("tobacco"),
                "fetched_at": datetime.now(timezone.utc),
            }

            session = self.db_service.get_session()
            try:
                existing = session.query(ESGScores).filter_by(asset_id=asset_id).first()
                if existing:
                    for k, v in values.items():
                        if k != "asset_id":
                            setattr(existing, k, v)
                else:
                    session.add(ESGScores(**values))
                session.commit()
            finally:
                session.close()

        except Exception as e:
            logger.error("Erreur _fetch_esg asset_id=%s: %s", asset_id, e)

    # ------------------------------------------------------------------
    # Earnings Trend
    # ------------------------------------------------------------------

    def _fetch_earnings_trend(self, asset_id: int, t: yf.Ticker) -> None:
        """Extrait les estimations futures (Earnings & Revenue Trend)."""
        try:
            from models import EarningsTrend

            # yfinance expose earnings_estimate et revenue_estimate
            try:
                ee = t.earnings_estimate
                re = t.revenue_estimate
            except Exception:
                logger.debug("Estimates non disponibles pour asset_id=%s", asset_id)
                return

            if ee is None or re is None or ee.empty or re.empty:
                return

            session = self.db_service.get_session()
            try:
                # On itère sur les périodes communes (0q, +1q, 0y, +1y)
                periods = ee.index.intersection(re.index)
                for period in periods:
                    e_row = ee.loc[period]
                    r_row = re.loc[period]

                    values = {
                        "asset_id": asset_id,
                        "period": str(period),
                        "fetched_at": datetime.now(timezone.utc),
                        # Revenue
                        "revenue_avg": _to_decimal(r_row.get("avg")),
                        "revenue_low": _to_decimal(r_row.get("low")),
                        "revenue_high": _to_decimal(r_row.get("high")),
                        "revenue_nb_analysts": _to_int(r_row.get("numberOfAnalysts")),
                        "revenue_growth": _to_decimal(r_row.get("growth")),
                        # EPS
                        "eps_avg": _to_decimal(e_row.get("avg")),
                        "eps_low": _to_decimal(e_row.get("low")),
                        "eps_high": _to_decimal(e_row.get("high")),
                        "eps_nb_analysts": _to_int(e_row.get("numberOfAnalysts")),
                        "eps_growth": _to_decimal(e_row.get("growth")),
                    }

                    # Upsert
                    existing = (
                        session.query(EarningsTrend)
                        .filter_by(asset_id=asset_id, period=str(period))
                        .first()
                    )

                    if existing:
                        for k, v in values.items():
                            if k not in ("asset_id", "period"):
                                setattr(existing, k, v)
                    else:
                        session.add(EarningsTrend(**values))

                session.commit()
            finally:
                session.close()
        except Exception as e:
            logger.error("Erreur _fetch_earnings_trend asset_id=%s: %s", asset_id, e)

    # ------------------------------------------------------------------
    # Outstanding Shares History
    # ------------------------------------------------------------------

    def _fetch_outstanding_shares(self, asset_id: int, t: yf.Ticker) -> None:
        """Extrait l'historique des actions en circulation."""
        try:
            from models import OutstandingSharesHistory

            # Logiciel yfinance: t.get_shares_full() retourne une série temporelle
            shares = t.get_shares_full()
            if shares is None or shares.empty:
                return

            session = self.db_service.get_session()
            try:
                for dt, count in shares.items():
                    date_val = _safe_date(dt)
                    if not date_val:
                        continue

                    values = {
                        "asset_id": asset_id,
                        "date": date_val,
                        "shares": int(count),
                        "fetched_at": datetime.now(timezone.utc),
                    }

                    existing = (
                        session.query(OutstandingSharesHistory)
                        .filter_by(asset_id=asset_id, date=date_val)
                        .first()
                    )

                    if not existing:
                        session.add(OutstandingSharesHistory(**values))
                session.commit()
            finally:
                session.close()
        except Exception as e:
            logger.error("Erreur _fetch_outstanding_shares asset_id=%s: %s", asset_id, e)

    # ------------------------------------------------------------------
    # GICS classification
    # ------------------------------------------------------------------

    def _fetch_gics(self, asset_id: int, t: yf.Ticker) -> None:
        """Met à jour la classification GICS de l'actif."""
        try:
            from models import Asset

            info = t.info
            if not info:
                return

            session = self.db_service.get_session()
            try:
                asset = session.get(Asset, asset_id)
                if asset:
                    # yfinance n'est pas top pour GICS direct, mais on mappe sector/industry
                    asset.gic_sector = info.get("sector")
                    asset.gic_industry = info.get("industry")
                    session.commit()
            finally:
                session.close()
        except Exception as e:
            logger.error("Erreur _fetch_gics asset_id=%s: %s", asset_id, e)
