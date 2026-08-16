"""Architecture tests enforcing Alembic as the only schema authority."""

from pathlib import Path

from sqlalchemy import create_engine, inspect

from database.migrations import MigrationInspector
from database.service import DatabaseService

ROOT = Path(__file__).parents[1]


def test_runtime_contains_no_schema_creation_fallback():
    excluded = {ROOT / "alembic", ROOT / "tests"}
    offenders = []
    for path in ROOT.rglob("*.py"):
        if "venv" in path.parts:
            continue
        if any(parent == path or parent in path.parents for parent in excluded):
            continue
        source = path.read_text(encoding="utf-8")
        if "metadata.create_all" in source or "ensure_schema_updates" in source:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_migration_inspection_does_not_create_version_table():
    engine = create_engine("sqlite:///:memory:")
    inspector = MigrationInspector(engine, None)
    status = inspector.get_status()
    assert status.current_heads == ()
    assert status.expected_heads == ("012",)
    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_database_service_accepts_only_current_alembic_revision():
    service = DatabaseService("sqlite:///:memory:")
    try:
        with service.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('012')")
        assert service.check_migrations() is True
    finally:
        service.close()
