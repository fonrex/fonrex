"""Sample ``~/.zipline/extension.py`` that plugs FonRex into Zipline.

Copy the body of this file into ``~/.zipline/extension.py`` (or symlink it)
so the ``fonrex`` bundle becomes discoverable from the ``zipline`` CLI:

.. code-block:: bash

    mkdir -p ~/.zipline
    cp zipline_bundle/extension.py ~/.zipline/extension.py

    export DATABASE_URL="postgresql://fonrex:fonrex@localhost:5432/fonrex"
    zipline ingest -b fonrex
    zipline bundles

Environment variables honoured
------------------------------

``DATABASE_URL``
    SQLAlchemy URL of the FonRex Postgres database. Falls back to the
    docker-compose default.

``FONREX_BUNDLE_TICKERS``
    Comma-separated tickers to restrict the ingest. When unset every asset
    with EOD rows in the target window is ingested.

``FONREX_BUNDLE_CALENDAR``
    Zipline trading calendar name. Defaults to ``NYSE`` because most
    ingested tickers are US-listed; use ``XPAR`` for Euronext Paris, etc.
"""

from __future__ import annotations

import os

from zipline_bundle import register_fonrex_bundle


def _env_tickers() -> list[str] | None:
    raw = os.environ.get("FONREX_BUNDLE_TICKERS")
    if not raw:
        return None
    return [chunk.strip().upper() for chunk in raw.split(",") if chunk.strip()]


register_fonrex_bundle(
    bundle_name=os.environ.get("FONREX_BUNDLE_NAME", "fonrex"),
    tickers=_env_tickers(),
    database_url=os.environ.get("DATABASE_URL"),
    calendar_name=os.environ.get("FONREX_BUNDLE_CALENDAR", "NYSE"),
)
