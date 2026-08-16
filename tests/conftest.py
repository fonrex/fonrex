"""
pytest configuration — isolation des suites de tests.

Les deux suites test_migrations.py et test_yfinance_enricher.py
utilisent SQLAlchemy avec StaticPool et peuvent interférer si
exécutées dans le même processus sans isolation explicite.
"""


def pytest_collection_modifyitems(items):
    """Garantit l'ordre d'exécution : migrations avant enricher pour éviter
    les conflits de SQLAlchemy mapper registry."""
    migration_tests = [i for i in items if "test_migrations" in str(i.fspath)]
    enricher_tests = [i for i in items if "test_yfinance_enricher" in str(i.fspath)]
    other_tests = [
        i
        for i in items
        if "test_migrations" not in str(i.fspath) and "test_yfinance_enricher" not in str(i.fspath)
    ]
    items[:] = migration_tests + other_tests + enricher_tests
