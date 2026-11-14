"""Architecture tests enforcing Alembic as the only schema authority."""

import ast
from pathlib import Path

from project_sources import relative, scope
from sqlalchemy import create_engine, inspect

from database.migrations import MigrationInspector
from database.service import DatabaseService

ROOT = Path(__file__).parents[1]


def test_runtime_contains_no_schema_creation_fallback():
    offenders = []
    for path in scope("code_without_migrations"):
        source = path.read_text(encoding="utf-8")
        if "metadata.create_all" in source or "ensure_schema_updates" in source:
            offenders.append(relative(path))
    assert offenders == []


def _migrations() -> dict[str, tuple[str, str | None]]:
    """Each migration file, with the revision and the down revision it declares."""
    migrations = {}
    for path in sorted((ROOT / "alembic" / "versions").glob("*.py")):
        if path.name.startswith("_"):
            continue
        declared = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    declared[target.id] = ast.literal_eval(node.value)
        assert "revision" in declared, f"{path.name} declares no revision"
        migrations[path.name] = (declared["revision"], declared.get("down_revision"))
    assert migrations
    return migrations


def _latest_migration_revision() -> str:
    """The head of the graph the migrations declare: the revision no other one follows."""
    migrations = _migrations().values()
    followed = {down for _, down in migrations}
    heads = sorted(revision for revision, _ in migrations if revision not in followed)
    assert len(heads) == 1, f"the migrations have {len(heads)} heads: {heads}"
    return heads[0]


def test_each_migration_file_is_named_after_its_revision():
    """A file named 030_* must declare revision 030: the files sort like the graph."""
    misnamed = {
        name: revision
        for name, (revision, _) in _migrations().items()
        if name.split("_", 1)[0] != revision
    }
    assert misnamed == {}


def test_the_migrations_form_one_chain():
    """Every migration but the first follows one that exists."""
    migrations = _migrations().values()
    revisions = {revision for revision, _ in migrations}
    roots = [revision for revision, down in migrations if down is None]
    dangling = sorted(
        f"{revision} -> {down}" for revision, down in migrations if down and down not in revisions
    )
    assert len(roots) == 1 and not dangling, (roots, dangling)


def test_migration_inspection_does_not_create_version_table():
    engine = create_engine("sqlite:///:memory:")
    inspector = MigrationInspector(engine, None)
    status = inspector.get_status()
    assert status.current_heads == ()
    assert status.expected_heads == (_latest_migration_revision(),)
    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_database_service_accepts_only_current_alembic_revision():
    head = _latest_migration_revision()
    service = DatabaseService("sqlite:///:memory:")
    try:
        with service.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('001')")
        assert service.check_migrations() is False
        with service.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = :head", {"head": head}
            )
        assert service.check_migrations() is True
    finally:
        service.close()
