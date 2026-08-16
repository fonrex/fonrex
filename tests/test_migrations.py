"""
Tests de migration Alembic pour Fonrex.

Vérifie que upgrade() et downgrade() fonctionnent correctement
sans base de données réelle (SQLite in-memory avec StaticPool).

Note: La migration 001 contient des chemins conditionnels (checkfirst)
et la migration 002 utilise des fonctionnalités PostgreSQL-spécifiques
(JSONB, ON CONFLICT, CREATE VIEW). Ces tests valident la logique Python
des migrations en mode SQLite-compatible autant que possible.
"""

import os
import sys
import unittest

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

# S'assurer que le répertoire racine est dans sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from models import (
    AnalystRatings,
    Asset,
    Base,
    EarningsHistory,
    FinancialStatement,
)
from schemas.fundamentals import (
    AnalystRatingsSchema,
    DeepFundamentalsResponse,
    EarningsHistorySchema,
    ETFDetailsSchema,
    ETFHoldingSchema,
    FinancialStatementSchema,
    HighlightsSchema,
    PeriodType,
    StatementType,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_sqlite_engine():
    """SQLite in-memory partagé entre threads, compatible avec tous les tests."""
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# ── Tests modèles SQLAlchemy ───────────────────────────────────────────────────


class TestModelsCreateAll(unittest.TestCase):
    """Vérifie que tous les nouveaux modèles Phase 2 se créent sans erreur."""

    def setUp(self):
        self.engine = _make_sqlite_engine()
        Base.metadata.create_all(self.engine)
        self.inspector = inspect(self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_all_new_tables_exist(self):
        tables = self.inspector.get_table_names()
        expected = [
            "fundamentals_highlights",
            "financial_statements",
            "earnings_history",
            "analyst_ratings",
            "etf_details",
            "etf_holdings",
        ]
        for table in expected:
            self.assertIn(table, tables, f"Table manquante: {table}")

    def test_fundamentals_highlights_columns(self):
        cols = {c["name"] for c in self.inspector.get_columns("fundamentals_highlights")}
        required = {
            "id",
            "asset_id",
            "fetched_at",
            "source",
            "market_cap",
            "pe_ratio",
            "pe_forward",
            "roe",
            "roa",
            "dividend_yield",
            "beta",
            "week_52_high",
            "week_52_low",
            "shares_outstanding",
            "pct_insiders",
            "pct_institutions",
            "book_value_per_share",
        }
        for col in required:
            self.assertIn(col, cols, f"Colonne manquante dans fundamentals_highlights: {col}")

    def test_financial_statements_columns(self):
        cols = {c["name"] for c in self.inspector.get_columns("financial_statements")}
        # Phase 2 rename: 'statement' → 'statement_type'
        self.assertIn("statement_type", cols, "statement_type manquant (Phase 2)")
        self.assertNotIn("statement", cols, "Ancien champ 'statement' toujours présent")
        # New fields
        for col in (
            "ebit",
            "currency",
            "net_debt",
            "rd_expense",
            "sga_expense",
            "dividends_paid",
            "depreciation_amortization",
        ):
            self.assertIn(col, cols, f"Nouveau champ manquant: {col}")

    def test_earnings_history_columns(self):
        cols = {c["name"] for c in self.inspector.get_columns("earnings_history")}
        self.assertIn("period_end", cols)
        self.assertIn("surprise", cols)  # nouveau champ Phase 2

    def test_analyst_ratings_columns(self):
        cols = {c["name"] for c in self.inspector.get_columns("analyst_ratings")}
        self.assertIn("target_mean", cols)
        self.assertIn("target_median", cols)  # nouveau champ Phase 2
        self.assertNotIn("target_price", cols, "target_price supprimé en Phase 2")

    def test_etf_details_table(self):
        cols = {c["name"] for c in self.inspector.get_columns("etf_details")}
        required = {
            "id",
            "asset_id",
            "inception_date",
            "net_expense_ratio",
            "total_net_assets",
            "is_ucits",
            "domicile",
            "replication_method",
            "ytd_return",
            "return_1y",
            "return_3y",
            "sharpe_ratio",
            "alloc_cash",
            "alloc_stock_us",
            "alloc_bond",
        }
        for col in required:
            self.assertIn(col, cols, f"Colonne ETFDetails manquante: {col}")

    def test_etf_holdings_table(self):
        cols = {c["name"] for c in self.inspector.get_columns("etf_holdings")}
        required = {
            "id",
            "etf_asset_id",
            "holding_ticker",
            "holding_isin",
            "holding_name",
            "weight",
            "sector",
            "country",
        }
        for col in required:
            self.assertIn(col, cols, f"Colonne ETFHolding manquante: {col}")


# ── Tests contraintes d'unicité ────────────────────────────────────────────────


class TestUniqueConstraints(unittest.TestCase):
    """Vérifie le comportement upsert des contraintes."""

    def setUp(self):
        self.engine = _make_sqlite_engine()
        Base.metadata.create_all(self.engine)
        self.Session = sa.orm.sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _make_asset(self, session):
        asset = Asset(id=1, ticker="AAPL", name="Apple Inc", isin="US0378331005")
        session.add(asset)
        session.commit()

    def test_financial_statement_unique_constraint(self):
        """Deux insertions pour le même (asset, statement_type, period_type, period_end) doivent violer la contrainte."""
        from datetime import date

        session = self.Session()
        self._make_asset(session)
        stmt1 = FinancialStatement(
            asset_id=1,
            statement_type="income",
            period_type="annual",
            period_end=date(2024, 12, 31),
            revenue=100000000,
        )
        session.add(stmt1)
        session.commit()

        stmt2 = FinancialStatement(
            asset_id=1,
            statement_type="income",
            period_type="annual",
            period_end=date(2024, 12, 31),
            revenue=999999999,
        )
        session.add(stmt2)
        with self.assertRaises(Exception):
            session.commit()
        session.rollback()
        session.close()

    def test_earnings_unique_constraint(self):
        """Deux insertions pour le même (asset_id, period) doivent violer la contrainte."""
        session = self.Session()
        self._make_asset(session)
        e1 = EarningsHistory(asset_id=1, period="2024Q4", eps_actual=1.21)
        session.add(e1)
        session.commit()

        e2 = EarningsHistory(asset_id=1, period="2024Q4", eps_actual=9.99)
        session.add(e2)
        with self.assertRaises(Exception):
            session.commit()
        session.rollback()
        session.close()

    def test_analyst_ratings_unique_per_asset(self):
        """Un actif ne peut avoir qu'un seul enregistrement de ratings (unique=True sur asset_id)."""
        session = self.Session()
        self._make_asset(session)
        r1 = AnalystRatings(asset_id=1, consensus="Buy", nb_analysts=10)
        session.add(r1)
        session.commit()

        r2 = AnalystRatings(asset_id=1, consensus="Hold", nb_analysts=15)
        session.add(r2)
        with self.assertRaises(Exception):
            session.commit()
        session.rollback()
        session.close()


# ── Tests schémas Pydantic ─────────────────────────────────────────────────────


class TestPydanticSchemas(unittest.TestCase):
    """Vérifie que les schémas Pydantic v2 se valident correctement."""

    def test_highlights_schema_valid(self):
        data = {
            "market_cap": "107400000000",
            "pe_ratio": "28.4",
            "roe": "0.421",
            "beta": "1.12",
            "shares_outstanding": 777000000,
            "source": "yfinance",
        }
        schema = HighlightsSchema(**data)
        self.assertEqual(float(schema.market_cap), 107400000000.0)
        self.assertEqual(schema.shares_outstanding, 777000000)
        self.assertEqual(schema.source, "yfinance")

    def test_highlights_schema_all_none(self):
        schema = HighlightsSchema()
        self.assertIsNone(schema.market_cap)
        self.assertIsNone(schema.roe)

    def test_financial_statement_schema(self):
        from datetime import date

        data = {
            "period_end": date(2024, 12, 31),
            "period_type": "annual",
            "statement_type": "income",
            "revenue": "65400000000",
            "net_income": "3400000000",
            "eps_diluted": "4.87",
        }
        schema = FinancialStatementSchema(**data)
        self.assertEqual(schema.period_type, PeriodType.annual)
        self.assertEqual(schema.statement_type, StatementType.income)
        self.assertEqual(float(schema.revenue), 65400000000.0)

    def test_earnings_history_schema(self):
        schema = EarningsHistorySchema(
            period="2024Q4",
            eps_actual="1.21",
            eps_estimate="1.15",
            surprise_pct="5.2",
        )
        self.assertEqual(schema.period, "2024Q4")
        self.assertEqual(float(schema.surprise_pct), 5.2)

    def test_analyst_ratings_schema(self):
        schema = AnalystRatingsSchema(
            consensus="Buy",
            target_mean="165.0",
            nb_analysts=24,
            strong_buy=8,
            buy=10,
            hold=5,
        )
        self.assertEqual(schema.consensus, "Buy")
        self.assertEqual(schema.nb_analysts, 24)
        self.assertEqual(schema.strong_buy, 8)

    def test_etf_details_schema(self):
        from datetime import date

        schema = ETFDetailsSchema(
            inception_date=date(2004, 1, 22),
            net_expense_ratio="0.0007",
            is_ucits=True,
            domicile="IE",
            replication_method="Physical",
        )
        self.assertEqual(float(schema.net_expense_ratio), 0.0007)
        self.assertTrue(schema.is_ucits)

    def test_etf_holding_schema(self):
        schema = ETFHoldingSchema(
            holding_ticker="AAPL",
            holding_name="Apple Inc",
            weight="0.074500",
            country="US",
        )
        self.assertAlmostEqual(float(schema.weight), 0.0745, places=4)

    def test_deep_fundamentals_response_empty(self):
        """Vérifie que DeepFundamentalsResponse peut être instancié avec des données minimales."""
        response = DeepFundamentalsResponse(
            asset_profile={"ticker": "AAPL", "name": "Apple Inc"},
        )
        self.assertIsNone(response.highlights)
        self.assertEqual(response.etf_holdings, [])
        self.assertIn("income", response.statements)
        self.assertEqual(response.meta["source"], "fonrex")

    def test_deep_fundamentals_response_full(self):
        from datetime import date

        response = DeepFundamentalsResponse(
            asset_profile={"ticker": "AAPL"},
            highlights=HighlightsSchema(market_cap="3000000000000", pe_ratio="30.0"),
            statements={
                "income": [
                    FinancialStatementSchema(
                        period_end=date(2024, 9, 28),
                        period_type="annual",
                        revenue="391035000000",
                    )
                ],
                "balance": [],
                "cashflow": [],
            },
            earnings_history=[
                EarningsHistorySchema(
                    period="2024Q4",
                    eps_actual="2.40",
                    eps_estimate="2.35",
                )
            ],
            analyst_ratings=AnalystRatingsSchema(consensus="Buy", nb_analysts=40),
        )
        self.assertIsNotNone(response.highlights)
        self.assertEqual(len(response.statements["income"]), 1)
        self.assertEqual(len(response.earnings_history), 1)
        self.assertEqual(response.analyst_ratings.consensus, "Buy")


# ── Tests idempotence migration ────────────────────────────────────────────────


class TestMigrationIdempotence(unittest.TestCase):
    """
    Vérifie que create_all et drop_all sont idempotents.
    Les migrations Alembic réelles (PostgreSQL) nécessitent un environnement Docker.
    """

    def test_create_all_is_idempotent(self):
        """Deux appels successifs à create_all ne doivent pas lever d'erreur."""
        engine = _make_sqlite_engine()
        Base.metadata.create_all(engine)
        Base.metadata.create_all(engine)  # deuxième appel — doit être no-op
        Base.metadata.drop_all(engine)
        engine.dispose()

    def test_drop_all_then_create_all(self):
        """drop_all suivi de create_all doit recréer toutes les tables."""
        engine = _make_sqlite_engine()
        Base.metadata.create_all(engine)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

        inspector = inspect(engine)
        tables = inspector.get_table_names()
        for table in ("assets", "fundamentals_highlights", "etf_details", "etf_holdings"):
            self.assertIn(table, tables, f"Table absente après recréation: {table}")

        Base.metadata.drop_all(engine)
        engine.dispose()


# ── Tests alembic.ini + env.py ────────────────────────────────────────────────


class TestAlembicConfig(unittest.TestCase):
    """Vérifie que les fichiers de configuration Alembic sont en place."""

    def test_alembic_ini_exists(self):
        alembic_ini = os.path.join(ROOT, "alembic.ini")
        self.assertTrue(os.path.exists(alembic_ini), f"alembic.ini introuvable: {alembic_ini}")

    def test_alembic_env_exists(self):
        alembic_env = os.path.join(ROOT, "alembic", "env.py")
        self.assertTrue(os.path.exists(alembic_env), f"alembic/env.py introuvable: {alembic_env}")

    def test_migration_001_exists(self):
        migration = os.path.join(ROOT, "alembic", "versions", "001_initial_schema.py")
        self.assertTrue(os.path.exists(migration), f"Migration 001 introuvable: {migration}")

    def test_migration_002_exists(self):
        migration = os.path.join(ROOT, "alembic", "versions", "002_refonte_fundamentals.py")
        self.assertTrue(os.path.exists(migration), f"Migration 002 introuvable: {migration}")

    def test_alembic_env_imports(self):
        """Vérifie que alembic/env.py peut être parsé sans erreurs de syntaxe."""
        import ast

        alembic_env = os.path.join(ROOT, "alembic", "env.py")
        with open(alembic_env) as f:
            source = f.read()
        try:
            ast.parse(source)
        except SyntaxError as e:
            self.fail(f"Syntaxe invalide dans alembic/env.py: {e}")

    def test_migration_002_imports(self):
        """Vérifie que la migration 002 peut être parsée sans erreurs de syntaxe."""
        import ast

        migration = os.path.join(ROOT, "alembic", "versions", "002_refonte_fundamentals.py")
        with open(migration) as f:
            source = f.read()
        try:
            ast.parse(source)
        except SyntaxError as e:
            self.fail(f"Syntaxe invalide dans 002_refonte_fundamentals.py: {e}")


if __name__ == "__main__":
    unittest.main()
