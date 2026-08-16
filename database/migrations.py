"""Read-only verification of the schema revision managed by Alembic."""

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from database.component import DatabaseComponent


@dataclass(frozen=True)
class MigrationStatus:
    """Current and expected Alembic revisions for one database."""

    current_heads: tuple[str, ...]
    expected_heads: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return self.current_heads == self.expected_heads


class MigrationInspector(DatabaseComponent):
    """Inspect Alembic state without creating or altering database objects."""

    def __init__(self, engine, session_factory, config_path=None):
        super().__init__(engine, session_factory)
        self.config_path = Path(config_path or Path(__file__).parents[1] / "alembic.ini")

    def get_status(self) -> MigrationStatus:
        config = Config(str(self.config_path))
        script_location = Path(config.get_main_option("script_location"))
        if not script_location.is_absolute():
            config.set_main_option(
                "script_location", str(self.config_path.parent / script_location)
            )
        scripts = ScriptDirectory.from_config(config)
        expected = tuple(sorted(scripts.get_heads()))
        with self.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            current = tuple(sorted(context.get_current_heads()))
        return MigrationStatus(current_heads=current, expected_heads=expected)
