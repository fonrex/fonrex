"""From the XBRL facts of the SEC to annual financial statements.

The SEC publishes, for a company, every fact of its filings
(``companyfacts``): a concept (``us-gaap:Revenues``), a unit (``USD``), a
period and a value, with the filing it comes from. A figure of the statements
is not one concept:

* **several concepts for one figure.** Revenue is ``Revenues`` at JPMorgan,
  ``RevenueFromContractWithCustomerExcludingAssessedTax`` at Apple, ``Revenue``
  (IFRS) at SAP and ``RevenueFromSaleOfGoods`` at Novartis. Each column lists
  its concepts in order; for each year the first one published is taken.
* **one fact, several filings.** A 10-K repeats the two previous years, and may
  restate them. One value per period is kept: the one of the latest filing.
* **the length of the period, not the form.** A 10-Q also gives nine months
  from the start of the year. A fiscal year is a period of 350 to 380 days
  filed in an annual report; a balance is a fact at a date (0 days).
* **one currency.** A foreign filer may give a few figures twice, in its
  currency and in US dollars. The figures of a year are read in the currency
  of the latest annual report, never mixed.

Values are kept as the SEC publishes them: payments (capex, dividends,
repurchases) are positive amounts, flows may be negative. A figure the company
does not publish (a bank has no operating income) stays ``None``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

US_GAAP = "us-gaap"
IFRS = "ifrs-full"
TAXONOMIES = (US_GAAP, IFRS)

# Forms of the annual reports: domestic (10-K), foreign (20-F), Canadian (40-F), amended.
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "20-F", "20-F/A", "40-F", "40-F/A"})
FISCAL_YEAR_DAYS = (350, 380)

MONEY = "money"
PER_SHARE = "per_share"
SHARES = "shares"


@dataclass(frozen=True)
class Column:
    """A column of ``financial_statements`` and the concepts that can fill it, in order."""

    name: str
    statement: str  # "income" | "balance" | "cashflow"
    kind: str  # MONEY | PER_SHARE | SHARES
    concepts: tuple[tuple[str, str], ...]

    @property
    def instant(self) -> bool:
        """A balance sheet figure is a fact at a date; the others cover the year."""
        return self.statement == "balance"


def _column(name: str, statement: str, gaap: tuple, ifrs: tuple, kind: str = MONEY) -> Column:
    return Column(
        name,
        statement,
        kind,
        tuple((US_GAAP, concept) for concept in gaap) + tuple((IFRS, concept) for concept in ifrs),
    )


COLUMNS: tuple[Column, ...] = (
    # Income statement
    _column(
        "revenue",
        "income",
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            "RevenuesNetOfInterestExpense",
        ),
        # IFRS 15 revenue is tagged either way: Airbus and Novo Nordisk use "Revenue",
        # TotalEnergies and Pernod Ricard "RevenueFromContractsWithCustomers".
        ("Revenue", "RevenueFromContractsWithCustomers", "RevenueFromSaleOfGoods"),
    ),
    _column("gross_profit", "income", ("GrossProfit",), ("GrossProfit",)),
    _column(
        "operating_income",
        "income",
        ("OperatingIncomeLoss",),
        ("ProfitLossFromOperatingActivities",),
    ),
    # A company without minority interests may tag only the profit of the group
    # (Novo Nordisk): it is then the profit of the owners of the parent.
    _column(
        "net_income",
        "income",
        ("NetIncomeLoss",),
        ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss"),
    ),
    _column(
        "eps_basic", "income", ("EarningsPerShareBasic",), ("BasicEarningsLossPerShare",), PER_SHARE
    ),
    _column(
        "eps_diluted",
        "income",
        ("EarningsPerShareDiluted",),
        ("DilutedEarningsLossPerShare",),
        PER_SHARE,
    ),
    _column(
        "shares_basic",
        "income",
        ("WeightedAverageNumberOfSharesOutstandingBasic",),
        ("WeightedAverageShares",),
        SHARES,
    ),
    _column(
        "shares_diluted",
        "income",
        ("WeightedAverageNumberOfDilutedSharesOutstanding",),
        ("AdjustedWeightedAverageShares",),
        SHARES,
    ),
    _column(
        "rd_expense",
        "income",
        ("ResearchAndDevelopmentExpense",),
        ("ResearchAndDevelopmentExpense",),
    ),
    _column(
        "sga_expense",
        "income",
        ("SellingGeneralAndAdministrativeExpense",),
        ("SellingGeneralAndAdministrativeExpense",),
    ),
    _column(
        "interest_expense",
        "income",
        ("InterestExpense", "InterestExpenseNonoperating"),
        ("InterestExpense", "FinanceCosts"),
    ),
    _column(
        "tax_provision",
        "income",
        ("IncomeTaxExpenseBenefit",),
        ("IncomeTaxExpenseContinuingOperations",),
    ),
    # Balance sheet
    _column("total_assets", "balance", ("Assets",), ("Assets",)),
    _column("total_liabilities", "balance", ("Liabilities",), ("Liabilities",)),
    _column(
        "total_equity",
        "balance",
        ("StockholdersEquity",),
        ("EquityAttributableToOwnersOfParent", "Equity"),
    ),
    _column(
        "cash_and_equivalents",
        "balance",
        ("CashAndCashEquivalentsAtCarryingValue",),
        ("CashAndCashEquivalents",),
    ),
    _column(
        "short_term_investments",
        "balance",
        ("ShortTermInvestments", "MarketableSecuritiesCurrent"),
        ("CurrentInvestments",),
    ),
    _column(
        "accounts_receivable",
        "balance",
        ("AccountsReceivableNetCurrent",),
        ("TradeAndOtherCurrentReceivables",),
    ),
    _column("inventory", "balance", ("InventoryNet",), ("Inventories",)),
    _column("goodwill", "balance", ("Goodwill",), ("Goodwill",)),
    _column(
        "intangible_assets",
        "balance",
        ("IntangibleAssetsNetExcludingGoodwill",),
        ("IntangibleAssetsOtherThanGoodwill",),
    ),
    _column(
        "long_term_debt",
        "balance",
        ("LongTermDebtNoncurrent", "LongTermDebt"),
        ("LongtermBorrowings",),
    ),
    _column(
        "retained_earnings",
        "balance",
        ("RetainedEarningsAccumulatedDeficit",),
        ("RetainedEarnings",),
    ),
    # Cash flow statement
    _column(
        "operating_cashflow",
        "cashflow",
        ("NetCashProvidedByUsedInOperatingActivities",),
        ("CashFlowsFromUsedInOperatingActivities",),
    ),
    _column(
        "investing_cashflow",
        "cashflow",
        ("NetCashProvidedByUsedInInvestingActivities",),
        ("CashFlowsFromUsedInInvestingActivities",),
    ),
    _column(
        "financing_cashflow",
        "cashflow",
        ("NetCashProvidedByUsedInFinancingActivities",),
        ("CashFlowsFromUsedInFinancingActivities",),
    ),
    _column(
        "capex",
        "cashflow",
        ("PaymentsToAcquirePropertyPlantAndEquipment",),
        (
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwill"
            "InvestmentPropertyAndOtherNoncurrentAssets",
        ),
    ),
    _column(
        "dividends_paid",
        "cashflow",
        ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
        (
            "DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities",
            "DividendsPaidClassifiedAsFinancingActivities",
        ),
    ),
    _column(
        "stock_repurchases",
        "cashflow",
        ("PaymentsForRepurchaseOfCommonStock",),
        ("PaymentsToAcquireOrRedeemEntitysShares",),
    ),
    _column(
        "depreciation_amortization",
        "cashflow",
        (
            "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization",
        ),
        ("DepreciationAndAmortisationExpense",),
    ),
)

COLUMNS_BY_NAME = {column.name: column for column in COLUMNS}
# The concepts Fonrex reads: only their facts are kept.
MAPPED_CONCEPTS = frozenset(pair for column in COLUMNS for pair in column.concepts)
# The columns that date a fiscal year: a year is a period one of them covers.
_YEAR_COLUMNS = ("revenue", "net_income", "operating_cashflow")


class XbrlError(ValueError):
    """An answer of the SEC that cannot be read: nothing is stored from it."""


@dataclass(frozen=True)
class FactRow:
    """A fact as it is stored in ``xbrl_facts``."""

    cik: str
    taxonomy: str
    concept: str
    unit: str
    period_end: date
    duration_days: int
    period_start: date | None
    value: Decimal
    accession: str
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    filed: date
    frame: str | None

    @property
    def key(self) -> tuple:
        return (self.taxonomy, self.concept, self.unit, self.period_end, self.duration_days)

    @property
    def order(self) -> tuple:
        """Which of two filings of the same fact is the latest."""
        return (self.filed, self.accession)


def _date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def _is_fiscal_year(days: int) -> bool:
    return FISCAL_YEAR_DAYS[0] <= days <= FISCAL_YEAR_DAYS[1]


def facts_to_rows(cik: str, companyfacts: Any) -> list[FactRow]:
    """The facts of the annual reports of the mapped concepts, latest filing first.

    Kept: forms of ``ANNUAL_FORMS``, balances (0 day) and fiscal years (350 to
    380 days). One row per concept, unit and period: the one filed last.
    """
    if not isinstance(companyfacts, dict) or not isinstance(companyfacts.get("facts"), dict):
        raise XbrlError("the answer of the SEC has no 'facts'")
    latest: dict[tuple, FactRow] = {}
    for taxonomy, concepts in companyfacts["facts"].items():
        if taxonomy not in TAXONOMIES or not isinstance(concepts, dict):
            continue
        for concept, body in concepts.items():
            if (taxonomy, concept) not in MAPPED_CONCEPTS or not isinstance(body, dict):
                continue
            for unit, facts in (body.get("units") or {}).items():
                for fact in facts or []:
                    row = _row(cik, taxonomy, concept, unit, fact)
                    if row is None:
                        continue
                    kept = latest.get(row.key)
                    if kept is None or row.order > kept.order:
                        latest[row.key] = row
    return sorted(latest.values(), key=lambda row: (row.period_end, row.concept, row.unit))


def _row(cik: str, taxonomy: str, concept: str, unit: str, fact: Any) -> FactRow | None:
    if not isinstance(fact, dict) or fact.get("form") not in ANNUAL_FORMS:
        return None
    end, start, filed = _date(fact.get("end")), _date(fact.get("start")), _date(fact.get("filed"))
    if end is None or filed is None or not fact.get("accn"):
        return None
    days = (end - start).days if start else 0
    if start is not None and not _is_fiscal_year(days):
        return None
    try:
        value = Decimal(str(fact["val"]))
    except (KeyError, InvalidOperation):
        return None
    if not value.is_finite():
        return None
    fiscal_year = fact.get("fy")
    return FactRow(
        cik=cik,
        taxonomy=taxonomy,
        concept=concept,
        unit=unit,
        period_end=end,
        duration_days=days,
        period_start=start,
        value=value,
        accession=str(fact["accn"]),
        form=str(fact["form"]),
        fiscal_year=fiscal_year if isinstance(fiscal_year, int) else None,
        fiscal_period=fact.get("fp"),
        filed=filed,
        frame=fact.get("frame"),
    )


@dataclass(frozen=True)
class Figure:
    """A figure of the statements and the fact it was read from."""

    value: Decimal
    taxonomy: str
    concept: str
    unit: str
    accession: str
    form: str
    filed: date

    @property
    def filing_url(self) -> str:
        """The index of the filing on EDGAR."""
        return (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{int(self.accession.split('-')[0])}/{self.accession.replace('-', '')}/"
        )


@dataclass
class FiscalYear:
    """The figures of one fiscal year, read in one currency."""

    period_end: date
    currency: str
    figures: dict[str, Figure] = field(default_factory=dict)

    def value(self, column: str) -> Decimal | None:
        figure = self.figures.get(column)
        return figure.value if figure else None


def reporting_currency(rows: Iterable[FactRow]) -> str | None:
    """The currency of the latest annual report: the most frequent money unit it uses."""
    money = [row for row in rows if "/" not in row.unit and row.unit != "shares"]
    if not money:
        return None
    last_filed = max(row.order for row in money)
    units = Counter(row.unit for row in money if row.order == last_filed)
    return units.most_common(1)[0][0]


def _unit_for(column: Column, currency: str) -> str:
    if column.kind == PER_SHARE:
        return f"{currency}/shares"
    if column.kind == SHARES:
        return "shares"
    return currency


def annual_statements(rows: Iterable[FactRow]) -> list[FiscalYear]:
    """The fiscal years of a company, most recent first, from its stored facts."""
    rows = list(rows)
    currency = reporting_currency(rows)
    if currency is None:
        return []
    by_key = {
        (row.taxonomy, row.concept, row.unit, row.period_end, row.duration_days): row
        for row in rows
    }
    by_concept: dict[tuple[str, str, str, bool], list[FactRow]] = {}
    for row in rows:
        instant = row.duration_days == 0
        by_concept.setdefault((row.taxonomy, row.concept, row.unit, instant), []).append(row)

    year_ends = sorted(
        {
            row.period_end
            for name in _YEAR_COLUMNS
            for taxonomy, concept in COLUMNS_BY_NAME[name].concepts
            for row in by_concept.get((taxonomy, concept, currency, False), [])
        },
        reverse=True,
    )
    years = []
    for end in year_ends:
        year = FiscalYear(end, currency)
        for column in COLUMNS:
            unit = _unit_for(column, currency)
            for taxonomy, concept in column.concepts:
                row = _fact_at(by_key, by_concept, taxonomy, concept, unit, end, column.instant)
                if row is not None:
                    year.figures[column.name] = Figure(
                        row.value, taxonomy, concept, unit, row.accession, row.form, row.filed
                    )
                    break
        years.append(year)
    return years


def _fact_at(by_key, by_concept, taxonomy, concept, unit, end, instant) -> FactRow | None:
    if instant:
        return by_key.get((taxonomy, concept, unit, end, 0))
    for row in by_concept.get((taxonomy, concept, unit, False), []):
        if row.period_end == end:
            return row
    return None
