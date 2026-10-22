"""Migration 027 — room for the long prefixes of ESEF extensions

Revision ID: 027
Revises: 026
Create Date: 2026-10-21

An ESEF filing names the concepts of its extension with a prefix of its own, and a
unit may hold one: the extension of Ecclesiastical Insurance Office plc is
``ecclesiasticalinsuranceofficepubliclimitedcompany``, 50 characters, where 026 gave
40. ``esef_facts.taxonomy`` and ``esef_facts.unit`` take 255 characters, as
``concept``. The downgrade gives them back 40 characters, and fails when a value
longer than that is stored.
"""

import sqlalchemy as sa

from alembic import op

revision = "027"
down_revision = "026"


def upgrade() -> None:
    for column in ("taxonomy", "unit"):
        op.alter_column("esef_facts", column, type_=sa.String(255), existing_type=sa.String(40))


def downgrade() -> None:
    for column in ("taxonomy", "unit"):
        op.alter_column("esef_facts", column, type_=sa.String(40), existing_type=sa.String(255))
