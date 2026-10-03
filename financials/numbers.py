"""One way to read a number displayed on a web page.

Every scraped provider used to carry its own conversion, each with its own
gaps: a negative figure read as positive (``-2.35`` or ``(2.35)`` gave
``2.35``), ``1,234.56`` read as ``1.0``, a currency code taken for a scale
(``MXN 12.5`` gave twelve and a half million), a non-breaking space making the
whole figure unreadable. The rules are written once, here.

``parse_number`` reads a text that is a number and nothing else (a table cell);
``find_number`` reads the first, or the last, number of a sentence.

A percentage is returned as displayed (``"1,81 %"`` gives ``1.81``): the
providers that publish percentages declare them in ``monitoring/units.py``.
"""

from __future__ import annotations

import math
import re
from typing import Any

# Scales written after a figure, in French and in English, compared in lower case.
SCALES = {
    "k": 1e3,
    "m": 1e6,
    "mio": 1e6,
    "mn": 1e6,
    "mln": 1e6,
    "million": 1e6,
    "millions": 1e6,
    "md": 1e9,
    "mds": 1e9,
    "mrd": 1e9,
    "milliard": 1e9,
    "milliards": 1e9,
    "b": 1e9,
    "bn": 1e9,
    "bln": 1e9,
    "billion": 1e9,
    "billions": 1e9,
    "t": 1e12,
    "tn": 1e12,
    "trillion": 1e12,
    "trillions": 1e12,
}

# Texts displayed in place of a figure that is not available.
PLACEHOLDERS = frozenset(
    {"", "-", "--", "---", "n/a", "na", "n.a.", "n.a", "nc", "n.c.", "nd", "n.d.", "null", "none"}
)

# Currencies as pages write them, compared in lower case. Three capital letters
# are not enough to be a currency: ``PER 12,5`` and ``12 PTS`` are not amounts.
CURRENCIES = frozenset(
    """
    eur usd gbp gbx chf jpy cad aud nzd sek nok dkk pln czk huf ron bgn isk rub try zar
    brl mxn ars clp cop pen cny cnh hkd twd krw sgd inr idr myr thb php vnd ils aed sar
    qar kwd egp ngn mad
    euro euros dollar dollars kr
    """.split()
) | frozenset("€$£¥₹₩₽₺₪฿")

_CURRENCY_SIGNS = "€$£¥₹₩₽₺₪฿"
# Before the figure: a sign, a code, or a dollar with its country (``HK$``, ``A$``).
_CURRENCY_BEFORE = re.compile(rf"[{_CURRENCY_SIGNS}]|[A-Za-z]{{1,3}}\$|[A-Za-z]{{3}}")

# A number as a program writes it: ``336.18``, ``.01``, ``-5``, ``1e5``.
_MACHINE_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

_DIGITS = re.compile(
    r"""
      \d{1,3}(?:[ ']\d{3})+(?:[.,]\d+)?   # 1 234 567,89   1'234.5
    | \d+(?:[.,]\d+)*[.,]?                # 1234,56   1.234,56   1,234.56   12.
    | [.,]\d+                             # .5
    """,
    re.VERBOSE,
)
_FIRST_DIGIT = re.compile(r"\d|[.,](?=\d)")

# A number inside a sentence, with what may be its sign or its parenthesis.
_NUMBER_IN_TEXT = re.compile(
    rf"(?<![\d.,])\(?[+-]?[{_CURRENCY_SIGNS}]?(?:\d+(?:[.,]\d+)*|[.,]\d+)%?\)?"
)

_CHARACTERS = str.maketrans(
    {
        " ": " ",  # non-breaking space
        " ": " ",  # narrow non-breaking space
        " ": " ",  # thin space
        " ": " ",  # figure space
        "−": "-",  # minus sign
        "‒": "-",  # figure dash
        "–": "-",  # en dash, displayed as a minus by some sites
        "—": "-",  # em dash
    }
)


def parse_number(value: Any, *, decimal: str = ",") -> float | None:
    """Read a displayed number, or return ``None`` when the text is not one.

    ``decimal`` is the decimal separator of the page: ``","`` for a French page
    (``1 234,56``), ``"."`` for an English one (``1,234.56``). It only decides
    the cases that are ambiguous; ``1.234,56`` and ``1,234.56`` are read
    correctly whatever its value.

    Understood: a sign (``-``, ``+``, the typographic minus, accounting
    parentheses), thousands separated by spaces of any kind, a scale
    (``k``, ``M``, ``Md``, ``B``, ``T`` ...), a currency before or after, ``%``
    and ``x``. Anything else next to the figure — another figure, a date, a
    word — makes the text unreadable rather than half read.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None

    # One space between words: nothing below depends on how the page was indented.
    text = " ".join(str(value).translate(_CHARACTERS).split())
    if text.lower() in PLACEHOLDERS:
        return None
    if _MACHINE_NUMBER.fullmatch(text):
        number = float(text)
        return number if math.isfinite(number) else None

    start = _FIRST_DIGIT.search(text)
    if not start:
        return None
    digits = _DIGITS.match(text, start.start())
    if not digits:
        return None

    before = _read_before(text[: digits.start()])
    after = _read_after(text[digits.end() :])
    if before is None or after is None:
        return None
    opened, sign = before
    closed, scale, percent = after
    if opened != closed:
        return None

    number = _digits_to_float(digits.group(), decimal)
    if number is None:
        return None
    number *= scale
    # Parentheses are the accounting notation of a negative amount; around a
    # signed figure or a percentage they are only parentheses: "1.04 (+0.41%)".
    if sign == "-" or (opened and not sign and not percent):
        number = -number
    return number if math.isfinite(number) else None


def find_number(text: Any, *, decimal: str = ".", last: bool = False) -> float | None:
    """Read the first number of a sentence (the last one with ``last=True``).

    For a value displayed among words: ``"P/E Ratio: 12.5"``, ``"$1,234.50"``,
    ``"0.32%"``. The sign and the thousands separators written with a comma or
    a dot are kept, which a plain search for digits loses. Scales and thousands
    separated by spaces are not read here: a cell holding only the figure goes
    to ``parse_number``.
    """
    if text is None:
        return None
    sentence = str(text).translate(_CHARACTERS)
    matches = list(_NUMBER_IN_TEXT.finditer(sentence))
    for match in reversed(matches) if last else matches:
        candidate = match.group()
        # A dash stuck to what precedes is a separator, not a minus:
        # ``148.00-172.40`` is a range and ``Q3-2026`` a period.
        glued = match.start() > 0 and sentence[match.start() - 1].isalnum()
        if glued and candidate[0] in "(+-":
            candidate = candidate.lstrip("(+-")
        # A parenthesis that is not closed, or a comma ending a clause, belongs
        # to the sentence and not to the number.
        if candidate.startswith("(") != candidate.endswith(")"):
            candidate = candidate.strip("()")
        if candidate.endswith((".", ",")):
            candidate = candidate[:-1]
        number = parse_number(candidate, decimal=decimal)
        if number is not None:
            return number
    return None


def _read_before(text: str) -> tuple[bool, str | None] | None:
    """What precedes the figure: ``(``, a currency, a sign — or ``None`` if anything else."""
    text = text.replace(" ", "")
    opened = text.startswith("(")
    if opened:
        text = text[1:]
    sign = None
    for symbol in "+-":
        if symbol in text:
            if sign or text.count(symbol) > 1:
                return None
            sign = symbol
    for part in text.replace("+", " ").replace("-", " ").split():
        if not _CURRENCY_BEFORE.fullmatch(part):
            return None
        if part[-1] not in _CURRENCY_SIGNS and part.lower() not in CURRENCIES:
            return None
    return opened, sign


def _read_after(text: str) -> tuple[bool, float, bool] | None:
    """What follows the figure: a scale, a currency, ``%`` or ``x``, ``)``.

    Returns (closing parenthesis, scale, percentage), or ``None`` if anything else.
    """
    text = text.replace(" ", "").lower()
    closed = text.endswith(")")
    if closed:
        text = text[:-1]
    text = text.rstrip("*")  # footnote mark of an estimate
    percent = text.endswith("%")
    if percent or text.endswith("x"):
        text = text[:-1]

    if not text or text in CURRENCIES:
        return closed, 1.0, percent
    if text in SCALES:
        return closed, SCALES[text], percent
    # A scale stuck to its currency: "M€", "kEUR", "Mds EUR".
    for cut in range(1, len(text)):
        if text[:cut] in SCALES and text[cut:] in CURRENCIES:
            return closed, SCALES[text[:cut]], percent
    return None


def _digits_to_float(digits: str, decimal: str) -> float | None:
    """Convert digits and separators (``1 234,56``, ``1,234.56``, ``1.234.567``).

    A separator that is repeated, or that comes before the decimal one, groups
    thousands: the groups must then have three digits. ``31.12.2025`` and
    ``1.5.2`` are not numbers.
    """
    digits = re.sub(r"[ ']", "", digits)
    if digits[-1] in ".,":  # "12." ends a sentence, it has no decimals
        digits = digits[:-1]
    if digits[0] in ".,":
        digits = "0" + digits
    commas, dots = digits.count(","), digits.count(".")

    if commas and dots:
        # Both are present: the last one is the decimal separator.
        decimal_separator = "," if digits.rfind(",") > digits.rfind(".") else "."
    elif commas > 1 or dots > 1:
        decimal_separator = None  # repeated: thousands
    elif commas:
        # "1,234" on an English page is one thousand two hundred and thirty-four.
        thousands = decimal == "." and re.fullmatch(r"\d{1,3},\d{3}", digits)
        decimal_separator = None if thousands else ","
    else:
        decimal_separator = "." if dots else None

    if decimal_separator:
        whole, _, fraction = digits.rpartition(decimal_separator)
    else:
        whole, fraction = digits, ""
    if not whole.isdigit():
        # What remains holds separators: they group thousands, all with the same one.
        if not re.fullmatch(r"\d{1,3}([.,])\d{3}(?:\1\d{3})*", whole):
            return None
        whole = re.sub(r"[.,]", "", whole)
    return float(f"{whole}.{fraction}" if fraction else whole)
