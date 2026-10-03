"""The Docker image must run without the project folder mounted over it."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Top-level folders holding Python code that the image legitimately leaves out.
DEVELOPMENT_ONLY = {"tests", "scratch", "venv"}


def _ignored_patterns() -> set[str]:
    lines = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return {
        line.strip().rstrip("/")
        for line in lines
        if line.strip() and not line.strip().startswith(("#", "!"))
    }


def _compose_lines(name: str) -> list[str]:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8").splitlines()


class TestImageContent:
    def test_no_application_package_is_left_out_of_the_image(self):
        packages = {
            path.name
            for path in PROJECT_ROOT.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and path.name not in DEVELOPMENT_ONLY
            and any(path.glob("*.py"))
        }
        assert packages, "no application package found"
        left_out = packages & _ignored_patterns()
        assert not left_out, (
            f".dockerignore excludes application code: {sorted(left_out)}. "
            "The image would not start without the project folder mounted."
        )

    def test_migrations_are_in_the_image(self):
        patterns = _ignored_patterns()
        assert "alembic" not in patterns
        assert "alembic/versions" not in patterns

    def test_default_logo_stays_in_the_image(self):
        lines = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        assert "static/logos" not in _ignored_patterns()
        assert "!static/logos/default.svg" in [line.strip() for line in lines]

    def test_local_secrets_are_kept_out_of_the_image(self):
        assert ".env" in _ignored_patterns()


class TestComposeMounts:
    def test_default_stack_does_not_mount_the_project_over_the_image(self):
        mounts = [line.strip() for line in _compose_lines("docker-compose.yml")]
        assert "- .:/app" not in mounts

    def test_development_override_mounts_the_project(self):
        mounts = [line.strip() for line in _compose_lines("docker-compose.dev.yml")]
        assert "- .:/app" in mounts


class TestDatabasePersistence:
    """The database files must be on a volume, or they are lost with the container."""

    # Data directory of the timescale/timescaledb-ha image. It differs from the
    # official postgres image (/var/lib/postgresql/data): a volume mounted there
    # stays empty while the database is written inside the container.
    TIMESCALE_HA_DATA_DIRECTORY = "/home/postgres/pgdata/data"

    def _lines(self) -> list[str]:
        return [line.strip() for line in _compose_lines("docker-compose.yml")]

    def test_database_image_is_the_one_this_check_knows(self):
        images = [line for line in self._lines() if line.startswith("image: timescale/")]
        assert images == ["image: timescale/timescaledb-ha:pg16"], (
            "The database image changed: check where the new image stores its data "
            "(PGDATA) and update the volume mount and this test together."
        )

    def test_data_directory_is_stated_and_is_the_one_of_the_image(self):
        declared = [line for line in self._lines() if line.startswith("- PGDATA=")]
        assert declared == [f"- PGDATA={self.TIMESCALE_HA_DATA_DIRECTORY}"]

    def test_a_named_volume_is_mounted_on_the_data_directory(self):
        mount = re.compile(rf"- (\w+):{re.escape(self.TIMESCALE_HA_DATA_DIRECTORY)}")
        volumes = [match[1] for line in self._lines() if (match := mount.fullmatch(line))]
        assert len(volumes) == 1, (
            "No named volume is mounted on the data directory of the database: "
            "`docker compose down` would delete the database."
        )
        assert f"{volumes[0]}:" in self._lines(), f"volume {volumes[0]} is not declared"


class TestServiceAddresses:
    """Inside Compose, .env must not send the API to the addresses of local development.

    The API service loads .env, written for a run outside Docker (``localhost``).
    Each address of a service it reads from .env is overridden in
    docker-compose.yml; otherwise the API looks for the database on its own
    container. ``ASYNC_DATABASE_URL`` is emptied so that it is derived from the
    overridden ``DATABASE_URL`` (``database/lifecycle.py``).
    """

    @staticmethod
    def _api_environment() -> dict[str, str]:
        lines = _compose_lines("docker-compose.yml")
        start = lines.index("  fonrex-api:")
        section = []
        for line in lines[start + 1 :]:
            if line.startswith("  ") and not line.startswith("    "):
                break  # next service
            section.append(line.strip())
        assert "- .env" in section, "the API service no longer loads .env: update this test"
        entries = [line[2:] for line in section if re.fullmatch(r"- [A-Z_]+=.*", line)]
        return dict(entry.split("=", 1) for entry in entries)

    @staticmethod
    def _service_addresses_of_env_example() -> set[str]:
        lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        names = {line.split("=", 1)[0] for line in lines if re.match(r"[A-Z_]+=", line)}
        return {name for name in names if name.endswith("DATABASE_URL") or name == "REDIS_URL"}

    def test_every_service_address_of_env_is_overridden(self):
        addresses = self._service_addresses_of_env_example()
        assert {"DATABASE_URL", "ASYNC_DATABASE_URL", "REDIS_URL"} <= addresses

        missing = addresses - set(self._api_environment())
        assert not missing, f"docker-compose.yml does not override {sorted(missing)} from .env"

    def test_async_database_url_is_derived_from_the_compose_database_url(self):
        environment = self._api_environment()
        assert environment["ASYNC_DATABASE_URL"] == ""
        assert "@db:5432/" in environment["DATABASE_URL"]
