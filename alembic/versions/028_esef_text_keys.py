"""Migration 028 — no length limit on the names of the ESEF values

Revision ID: 028
Revises: 027
Create Date: 2026-10-22

A company names the concepts and units of its own extension as it wishes: the
first reading of a catalogue of 3 000 European companies found names longer than
the 255 characters of 027 (Eurofins Scientific among them). ``esef_facts.taxonomy``,
``concept`` and ``unit`` become ``text``: PostgreSQL stores and indexes them alike.
The downgrade gives them back 255 characters, and fails when a longer value is stored.
"""

import sqlalchemy as sa

from alembic import op

revision = "028"
down_revision = "027"

COLUMNS = ("taxonomy", "concept", "unit")


def upgrade() -> None:
    for column in COLUMNS:
        op.alter_column("esef_facts", column, type_=sa.Text(), existing_type=sa.String(255))


def downgrade() -> None:
    for column in COLUMNS:
        op.alter_column("esef_facts", column, type_=sa.String(255), existing_type=sa.Text())
