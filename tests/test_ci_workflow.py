"""The CI runs the database tests, on the database an installation runs.

``tests/test_timescale_integration.py`` needs a TimescaleDB server and is skipped
without one. Skipped everywhere, it protects nothing: the migrations of existing
data, the storage of prices on a compressed hypertable and the cleanup are SQL
that SQLite cannot run.
"""

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
COMPOSE = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
MAKEFILE = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")


def _database_image_of_the_installation() -> str:
    """Image of the ``db`` service of docker-compose.yml."""
    service = re.search(r"^  db:\n(?P<body>(?:    .*\n|\n)+)", COMPOSE, re.MULTILINE)
    assert service, "docker-compose.yml has no db service"
    image = re.search(r"^    image:\s*(\S+)", service["body"], re.MULTILINE)
    assert image, "the db service of docker-compose.yml names no image"
    return image[1]


def _workflow_services() -> str:
    block = re.search(r"^    services:\n(?P<body>(?:      .*\n|\n)+)", WORKFLOW, re.MULTILINE)
    assert block, "the quality job of the CI starts no service: the database tests are skipped"
    return block["body"]


def test_ci_starts_the_database_of_an_installation():
    image = _database_image_of_the_installation()

    assert "timescaledb" in image
    assert re.search(rf"^\s+image:\s*{re.escape(image)}\s*$", _workflow_services(), re.MULTILINE), (
        f"the CI must run the database tests on {image}, the image of docker-compose.yml"
    )


def test_ci_gives_the_tests_the_address_of_that_database():
    address = re.search(r"^\s+FONREX_TEST_DATABASE_URL:\s*(\S+)", WORKFLOW, re.MULTILINE)
    assert address, "without FONREX_TEST_DATABASE_URL the database tests are skipped"

    services = _workflow_services()
    user = re.search(r"POSTGRES_USER:\s*(\S+)", services)[1]
    password = re.search(r"POSTGRES_PASSWORD:\s*(\S+)", services)[1]
    port = re.search(r"-\s*(\d+):5432", services)[1]
    assert address[1].startswith(f"postgresql://{user}:{password}@127.0.0.1:{port}/")


def test_ci_waits_for_the_database_over_tcp():
    """On its socket the server answers during its initialisation, before it restarts."""
    assert re.search(r'--health-cmd "pg_isready -h 127\.0\.0\.1 ', _workflow_services())


def test_local_target_uses_the_same_image():
    image = re.search(r"^TEST_DB_IMAGE\s*:=\s*(\S+)", MAKEFILE, re.MULTILINE)
    assert image, "the Makefile has no test-db target"
    assert image[1] == _database_image_of_the_installation()
    assert re.search(r"^test-db:.*## ", MAKEFILE, re.MULTILINE)


def test_database_tests_are_not_skipped_on_the_ci():
    """On GitHub Actions the variable must be set, whatever the workflow that runs pytest."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        assert os.environ.get("FONREX_TEST_DATABASE_URL"), (
            "FONREX_TEST_DATABASE_URL is not set: tests/test_timescale_integration.py "
            "would be skipped and the run would still be green"
        )
