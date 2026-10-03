"""The test suite does not depend on what runs on the developer's machine."""

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

DEVELOPER_SERVICES = {
    "REDIS_URL": "redis://localhost:6379/0",
    "DATABASE_URL": "postgresql://fonrex:fonrex_password@localhost:5432/fonrex",
    "ASYNC_DATABASE_URL": "postgresql+asyncpg://fonrex:fonrex_password@localhost:5432/fonrex",
}


def test_suite_never_points_to_the_services_of_the_developer():
    """Addresses exported in the shell, or the defaults, are replaced before any import.

    With a `make docker-run` stack up, the default addresses are the real Redis and
    the real database: the suite cached its fake answers there and its result
    changed with what was running (a failure seen only with Redis reachable).
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, runpy; runpy.run_path('tests/conftest.py'); "
            "print(os.environ['REDIS_URL'], os.environ['DATABASE_URL'], "
            "os.environ.get('ASYNC_DATABASE_URL'))",
        ],
        cwd=PROJECT,
        env={**os.environ, **DEVELOPER_SERVICES},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    redis_url, database_url, async_database_url = completed.stdout.split()

    assert redis_url == "redis://127.0.0.1:1/0"
    assert database_url.endswith("@127.0.0.1:1/fonrex_tests")
    assert async_database_url == "None"


def test_application_of_the_suite_reads_those_addresses():
    import main

    assert main.REDIS_URL == os.environ["REDIS_URL"] == "redis://127.0.0.1:1/0"
