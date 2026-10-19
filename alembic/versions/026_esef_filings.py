"""Migration 026 — ESEF filings of European listed companies

Revision ID: 026
Revises: 025
Create Date: 2026-10-19

A company listed on a regulated market of the European Union files its annual
report in the European Single Electronic Format (ESEF), its figures tagged in
XBRL. filings.xbrl.org collects the filings, by Legal Entity Identifier (LEI),
and converts each one to xBRL-JSON.

- ``esef_filings``: a filing of the index (its ``fxo_id``, its LEI, the period the
  index gives it, its country, its links, its counts of errors), and what its last
  reading found: ``read_status`` (``read``, ``refused``, ``missing``), the last day
  of the latest period of its facts (``report_end``, the true period of the
  filing), and its counts of facts, breakdowns left out and conflicts dropped.
- ``esef_facts``: one value per company (LEI), concept (the prefix of the
  filing, ``ifrs-full`` or an extension of the company, and its name), unit and
  period. A period is its last day and its length in days (0 for a balance). The
  value of the latest filing is kept: a restatement replaces the earlier one;
  ``fxo_id`` names the filing it was read from, ``report_end`` orders the filings.
  The values go with their filing (``ON DELETE CASCADE``).
- ``esef_entity_loads``: the last reading of the filings of each company.

The index ``ix_esef_filings_lei`` serves the filings of a company by period, and
``ix_esef_facts_fxo_id`` the values of a filing. The downgrade drops the three tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "026"
down_revision = "025"


def upgrade() -> None:
    op.create_table(
        "esef_filings",
        sa.Column("fxo_id", sa.String(100), primary_key=True),
        sa.Column("lei", sa.String(20), nullable=False),
        sa.Column("listed_period_end", sa.Date()),
        sa.Column("country", sa.String(2)),
        sa.Column("json_url", sa.String(500)),
        sa.Column("viewer_url", sa.String(500)),
        sa.Column("report_url", sa.String(500)),
        sa.Column("package_url", sa.String(500)),
        sa.Column("error_count", sa.Integer()),
        sa.Column("warning_count", sa.Integer()),
        sa.Column("inconsistency_count", sa.Integer()),
        sa.Column("added", sa.DateTime(timezone=True)),
        sa.Column("processed", sa.DateTime(timezone=True)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("listed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("read_status", sa.String(12)),
        sa.Column("read_reason", sa.String(500)),
        sa.Column("report_end", sa.Date()),
        sa.Column("facts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("breakdowns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conflicts", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "read_status IN ('read', 'refused', 'missing')", name="ck_esef_filings_read_status"
        ),
    )
    op.create_index("ix_esef_filings_lei", "esef_filings", ["lei", "report_end"])
    op.create_table(
        "esef_facts",
        sa.Column("lei", sa.String(20), primary_key=True),
        sa.Column("taxonomy", sa.String(40), primary_key=True),
        sa.Column("concept", sa.String(255), primary_key=True),
        sa.Column("unit", sa.String(40), primary_key=True),
        sa.Column("period_end", sa.Date(), primary_key=True),
        sa.Column("duration_days", sa.Integer(), primary_key=True),
        sa.Column("period_start", sa.Date()),
        sa.Column("value", sa.Numeric(28, 6), nullable=False),
        sa.Column("decimals", sa.Integer()),
        sa.Column("fxo_id", sa.String(100), nullable=False),
        sa.Column("report_end", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["fxo_id"],
            ["esef_filings.fxo_id"],
            name="fk_esef_facts_filing",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_esef_facts_fxo_id", "esef_facts", ["fxo_id"])
    op.create_table(
        "esef_entity_loads",
        sa.Column("lei", sa.String(20), primary_key=True),
        sa.Column("entity_name", sa.String(255)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_report_end", sa.Date()),
        sa.Column("facts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("esef_entity_loads")
    op.drop_index("ix_esef_facts_fxo_id", table_name="esef_facts")
    op.drop_table("esef_facts")
    op.drop_index("ix_esef_filings_lei", table_name="esef_filings")
    op.drop_table("esef_filings")
