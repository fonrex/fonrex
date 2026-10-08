"""The single reader of numbers displayed on provider pages."""

import ast
from pathlib import Path

import pytest

from financials.numbers import find_number, parse_number


class TestParseNumber:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # French pages
            ("6,89 EUR", 6.89),
            ("1,49%", 1.49),
            ("1,81 %", 1.81),
            ("25,5x", 25.5),
            ("1 234,56", 1234.56),
            ("1 234,56", 1234.56),  # non-breaking space
            ("1 234,56", 1234.56),  # narrow non-breaking space
            ("80 945 151 250", 80_945_151_250),
            ("1.234,56", 1234.56),
            ("1.234.567", 1_234_567),
            ("293,320 $", 293.32),
            ("31,27", 31.27),
            ("1.81", 1.81),
            ("12", 12.0),
            ("1'234,5", 1234.5),
        ],
    )
    def test_french_page(self, text, expected):
        assert parse_number(text, decimal=",") == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("38.25", 38.25),
            ("1,234.56", 1234.56),
            ("1,234", 1234.0),
            ("12,345,678", 12_345_678),
            ("$1,234.50", 1234.5),
            ("5.67%", 5.67),
            ("0.32%", 0.32),
            ("€189.30", 189.3),
            ("USD 12.5", 12.5),
            ("12.5 USD", 12.5),
            # A decimal comma on an English page is still a decimal comma.
            ("12,5", 12.5),
            ("1,23", 1.23),
        ],
    )
    def test_english_page(self, text, expected):
        assert parse_number(text, decimal=".") == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("-0,25", -0.25),
            ("+1,5%", 1.5),
            ("−2,35", -2.35),  # typographic minus
            ("–2,35", -2.35),  # en dash used as a minus
            ("(2,35)", -2.35),  # accounting notation
            ("(1 234,5)", -1234.5),
            ("-12,96 Md", -12_960_000_000),
            ("- 3,5 %", -3.5),
            ("-$1.2B", -1_200_000_000),
            ("$-1.2B", -1_200_000_000),
        ],
    )
    def test_sign_is_kept(self, text, expected):
        assert parse_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("3 k", 3_000),
            ("3K", 3_000),
            ("12 M", 12_000_000),
            ("456,7 Mio", 456_700_000),
            ("80,95 Md", 80_950_000_000),
            ("1,2 Mds", 1_200_000_000),
            ("2 Mrd", 2_000_000_000),
            ("1.23B", 1_230_000_000),
            ("102.47B", 102_470_000_000),
            ("4.87T", 4_870_000_000_000),
            ("17,83 Md€", 17_830_000_000),
            ("3 k€", 3_000),
            ("1,5 milliard", 1_500_000_000),
            ("2.5 billion", 2_500_000_000),
        ],
    )
    def test_scale(self, text, expected):
        assert parse_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "text", ["MXN 12.5", "12.5 MXN", "BRL 12.5", "KRW 12.5", "TRY 12.5", "12.5 THB"]
    )
    def test_currency_code_is_not_a_scale(self, text):
        """``M``, ``B``, ``K`` and ``T`` inside a currency code multiplied the figure."""
        assert parse_number(text, decimal=".") == 12.5

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "-", "--", "—", "N/A", "n.a.", "nc", "EUR", "Md", "%", "abc", None],
    )
    def test_no_figure(self, text):
        assert parse_number(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "80,95 Inconnu",
            "12 pts",
            "12 PTS",
            "3 ans",
            "5 ANS",
            "PER 12,5",
            "12 TTM",
            "JUN 30",
            "Aug 10, 2026",
            "12 / 23",
            "12:30",
            "(2,35",
            "2,35)",
            "1,5 Mxyz",
            "12-month",
        ],
    )
    def test_text_that_is_more_than_a_number_is_not_half_read(self, text):
        assert parse_number(text) is None
        assert parse_number(text, decimal=".") is None

    @pytest.mark.parametrize(
        "text",
        [
            "31.12.2025",  # a date
            "31/12/2025",
            "1.5 2.5",  # two figures of two columns
            "12,5 13,2",
            "25,31 22,10",
            "2024 2025",
            "1 2",
            "1.2.3",
            "1..2",
            "192.168.0.1",
            "1,23,456",
            "1.234,567.8",
            "12 5",
            "1 2345",
        ],
    )
    def test_two_figures_or_a_date_are_not_one_number(self, text):
        """Separators group thousands by three digits, or the text is not a number."""
        assert parse_number(text) is None
        assert parse_number(text, decimal=".") is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12 eur", 12.0),
            ("12 Eur", 12.0),
            ("6,89 euros", 6.89),
            ("73 420 MEUR", 73_420_000_000),
            ("12 kEUR", 12_000),
            ("12 MUSD", 12_000_000),
            ("12 mds eur", 12_000_000_000),
            ("USD12.5", 12.5),
            ("CHF12.50", 12.5),
            ("HK$320.00", 320.0),
            ("A$45.10", 45.1),
            ("\u20b91,234.50", 1234.5),
            ("12 kr", 12.0),
            ("1,81 %*", 1.81),  # footnote mark of an estimate
        ],
    )
    def test_currency_as_pages_write_it(self, text, expected):
        assert parse_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (".5", 0.5),
            (",5", 0.5),
            (".01", 0.01),
            ("-.5", -0.5),
            ("336.18", 336.18),
            ("2399", 2399.0),
            ("1e5", 100000.0),
            ("12.", 12.0),
        ],
    )
    def test_number_as_a_program_writes_it(self, text, expected):
        """Values of a data file (the SEC forms) are read like the others."""
        assert parse_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("(+0.52%)", 0.52), ("(0.41%)", 0.41), ("(+12)", 12.0), ("(-2.35)", -2.35)],
    )
    def test_parentheses_around_a_signed_figure_or_a_percentage_are_not_a_minus(
        self, text, expected
    ):
        assert parse_number(text, decimal=".") == pytest.approx(expected)

    def test_long_runs_of_spaces_cost_nothing(self):
        """Indentation kept inside a cell made the search take seconds."""
        import time

        started = time.perf_counter()
        for text in ("1" + " " * 5000 + "a", "(" + " " * 5000 + "a", "2026" + "\n " * 3000 + "(e)"):
            assert parse_number(text) is None
        assert parse_number("12" + " " * 5000 + "M") == 12_000_000
        assert time.perf_counter() - started < 0.5

    def test_numbers_are_returned_as_they_are(self):
        assert parse_number(12) == 12.0
        assert parse_number(-1.5) == -1.5
        assert parse_number(True) is None
        assert parse_number(float("nan")) is None
        assert parse_number(float("inf")) is None


class TestFindNumber:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("38.25", 38.25),
            ("P/E Ratio: 12.5", 12.5),
            ("$8.72", 8.72),
            ("0.32%", 0.32),
            ("EPS (TTM) $1,234.50", 1234.5),
            ("-2.35", -2.35),
            ("EPS: -2.35", -2.35),
            ("EPS (2.35)", -2.35),
            ("−0.44%", -0.44),
            ("1,234.56 USD", 1234.56),
            ("Yield 1.5%, ex-date soon", 1.5),
            ("Price is 12.", 12.0),
        ],
    )
    def test_first_number_of_a_sentence(self, text, expected):
        assert find_number(text) == pytest.approx(expected)

    def test_last_number(self):
        assert find_number("P/E Ratio (TTM) 2 years: 12.5", last=True) == 12.5
        assert find_number("52 Week Range 148.00 - 172.40", last=True) == 172.4

    def test_range_separator_is_not_a_minus(self):
        assert find_number("148.00-172.40", last=True) == 172.4
        assert find_number("148.00-172.40") == 148.0
        assert find_number("Q3-2026", last=True) == 2026

    def test_value_stuck_to_its_label(self):
        """An HTML parser joins a label and its value without a space."""
        assert find_number("Yield0.32%") == 0.32
        assert find_number("P/E Ratio38.25") == 38.25
        assert find_number("EPS$8.72") == 8.72

    def test_french_sentence(self):
        assert find_number("PER : 25,5", decimal=",") == 25.5

    def test_change_in_parentheses_keeps_its_sign(self):
        assert find_number("Change: +1.25 (+0.52%)", last=True) == 0.52
        assert find_number("1.04 (0.41%)", last=True) == 0.41
        assert find_number("EPS (2.35)") == -2.35

    def test_addresses_and_dates_are_not_numbers(self):
        assert find_number("192.168.0.1") is None
        assert find_number("31.12.2025") is None
        assert find_number("on 31.12.2025 the yield was 1.5%") == 1.5

    def test_number_without_leading_digit(self):
        assert find_number("beta .52") == 0.52

    @pytest.mark.parametrize("text", ["", "N/A", "no figure here", None])
    def test_no_number(self, text):
        assert find_number(text) is None


# Files of ``financials/providers`` that still convert texts by themselves.
OWN_CONVERSION = {
    # Retry-After header of an HTTP answer, not a figure of a page.
    "base.py",
    # Reads the first figure of a whole page: the search itself has to be redone.
    "Gurufocus_provider.py",
    # Amounts with their currency and unit, kept as Decimal (ETF details).
    "JustETF_provider.py",
    "justetf.py",
}


def test_providers_read_displayed_numbers_with_the_common_reader():
    """``float(...)`` in a provider is a private conversion with its own gaps.

    Sign, thousands separators, scales and currencies are handled once, in
    ``financials/numbers.py``.
    """
    providers = Path(__file__).parent.parent / "financials" / "providers"
    offenders = []
    for path in sorted(providers.glob("*.py")):
        if path.name in OWN_CONVERSION:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, f"use parse_number / find_number instead of float(): {offenders}"
