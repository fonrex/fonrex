"""
pytest configuration — isolation des suites de tests.

Les deux suites test_migrations.py et test_yfinance_enricher.py
utilisent SQLAlchemy avec StaticPool et peuvent interférer si
exécutées dans le même processus sans isolation explicite.
"""

import os
import warnings

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp")

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated\. Use 'HTTP_422_UNPROCESSABLE_CONTENT' instead\.",
        category=DeprecationWarning,
    )
    import fastapi  # noqa: F401


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
