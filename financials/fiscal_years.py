"""A fiscal year seen as one row.

``financial_statements`` holds one row per statement type: the income
statement, the balance sheet and the cash-flow statement of a fiscal year are
three rows, each filling only its own columns. A calculation that needs the debt
(balance sheet) next to the interest (income statement) or the free cash flow
(cash-flow statement) must first put the three rows of a year together.

Reading "the latest row" instead gives one statement out of three, and every
figure of the two others as missing.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from models import FinancialStatement

# Columns that say which row it is, not what the company published.
_NOT_A_FIGURE = frozenset({"id", "asset_id", "statement_type", "period_type", "fetched_at"})

#: Columns carried by a fiscal year: the period, the currency and every figure.
FISCAL_YEAR_FIELDS: tuple[str, ...] = tuple(
    column.name
    for column in FinancialStatement.__table__.columns
    if column.name not in _NOT_A_FIGURE
)

# When two statements give the same figure, the one that owns it is listed first.
_STATEMENT_ORDER = {"income": 0, "balance": 1, "cashflow": 2}


class FiscalYear(SimpleNamespace):
    """The statements of one fiscal year: every column, ``None`` when not published."""


def fiscal_years(rows: Iterable[Any], limit: int | None = None) -> list[FiscalYear]:
    """Group statement rows by period end, most recent fiscal year first.

    ``rows`` are rows of one instrument and one periodicity, in any order.
    ``limit`` keeps the most recent fiscal years only — it counts years, not rows.
    """
    by_period: dict[Any, list[Any]] = {}
    for row in rows:
        by_period.setdefault(row.period_end, []).append(row)

    years = []
    for period_end in sorted(by_period, reverse=True):
        statements = sorted(
            by_period[period_end],
            key=lambda row: _STATEMENT_ORDER.get(getattr(row, "statement_type", None), 99),
        )
        figures = {
            field: next(
                (
                    value
                    for value in (getattr(row, field, None) for row in statements)
                    if value is not None
                ),
                None,
            )
            for field in FISCAL_YEAR_FIELDS
        }
        years.append(FiscalYear(**figures))
    return years if limit is None else years[:limit]
