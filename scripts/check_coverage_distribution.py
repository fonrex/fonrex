"""Fail CI when critical modules hide below the global coverage average.

The global threshold (``fail_under`` in ``pyproject.toml``) only guards the
average, so a well-tested module can hide an untested one. This script adds a
floor per module:

* every module of a guarded package (the data providers) must declare a floor,
  so a new provider cannot be merged without tests;
* a floor is the measured coverage rounded down: raise it when the coverage of
  the module rises, never lower it.

Run ``make test-cov`` to produce ``coverage.json`` and check it. With
``--summary FILE`` the script also appends a Markdown report to ``FILE``; the CI
uses it to show the coverage on the pull request checks.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MODULE_THRESHOLDS = {
    "cache/adapters.py": 95.0,
    "financials/enrichment/adapters.py": 90.0,
    "historical/normalization.py": 85.0,
    "historical/providers.py": 65.0,
    "monitoring/canary_catalog.py": 75.0,
    "monitoring/canary_monitor.py": 44.0,
    "monitoring/price_ranges.py": 85.0,
    "realtime/connection_manager.py": 80.0,
    "technical/calculation_engine.py": 70.0,
    "database/technical.py": 90.0,
    "technical/indicator_service.py": 70.0,
    "use_cases/fundamentals.py": 75.0,
    "use_cases/specialized.py": 90.0,
    # Data providers: parsers checked against real pages, search and download
    # checked against a simulated network.
    "financials/providers/base.py": 98.0,
    "financials/providers/Barrons_provider.py": 92.0,
    "financials/providers/BourseDirect_provider.py": 94.0,
    "financials/providers/boursorama_provider.py": 82.0,
    "financials/providers/Fortuneo_provider.py": 87.0,
    "financials/providers/GoogleFinance_provider.py": 85.0,
    "financials/providers/Gurufocus_provider.py": 83.0,
    "financials/providers/index_constituents.py": 69.0,
    "financials/providers/Investing_provider.py": 93.0,
    "financials/providers/InvestirLesEchos_provider.py": 95.0,
    "financials/providers/justetf.py": 87.0,
    "financials/providers/JustETF_provider.py": 83.0,
    "financials/providers/Marketwatch_provider.py": 88.0,
    "financials/providers/MorningStar_provider.py": 78.0,
    "financials/providers/Msn_provider.py": 100.0,
    "financials/providers/openfigi.py": 90.0,
    "financials/providers/sec_edgar.py": 78.0,
    "financials/providers/wallStreetJournal_provider.py": 85.0,
    "financials/providers/yfinance_provider.py": 81.0,
    "financials/providers/ZoneBourse_provider.py": 86.0,
}

# Every module of these packages must appear in MODULE_THRESHOLDS.
GUARDED_PACKAGES = ("financials/providers/",)

# A floor this far below the measured coverage is reported as ready to be raised.
RAISE_HINT_MARGIN = 5.0


def is_guarded(module: str) -> bool:
    """Tell whether a module must declare a coverage floor."""
    return module.startswith(GUARDED_PACKAGES) and not module.endswith("/__init__.py")


def load_report(report_path: Path) -> dict:
    return json.loads(report_path.read_text(encoding="utf-8"))


def _coverage(module_report: dict) -> float:
    return float(module_report["summary"]["percent_covered"])


def check_report(report: dict) -> list[str]:
    files = report.get("files", {})
    failures = []
    for module, minimum in MODULE_THRESHOLDS.items():
        module_report = files.get(module)
        if module_report is None:
            failures.append(f"{module}: absent from coverage report")
            continue
        actual = _coverage(module_report)
        if actual < minimum:
            failures.append(f"{module}: {actual:.2f}% < {minimum:.2f}%")
    for module in sorted(files):
        if is_guarded(module) and module not in MODULE_THRESHOLDS:
            failures.append(
                f"{module}: no coverage floor declared "
                f"(measured {_coverage(files[module]):.2f}%, add it to MODULE_THRESHOLDS)"
            )
    return failures


def check_distribution(report_path: Path) -> list[str]:
    return check_report(load_report(report_path))


def global_floor(pyproject_path: Path | None = None) -> float | None:
    """Read the global ``fail_under`` threshold, if the project declares one."""
    path = pyproject_path or PROJECT_ROOT / "pyproject.toml"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = config.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under")
    return float(value) if value is not None else None


def render_summary(report: dict, floor: float | None = None) -> str:
    """Render the coverage of the guarded modules as a Markdown report."""
    files = report.get("files", {})
    failures = check_report(report)
    lines = ["## Test coverage", ""]

    total = report.get("totals", {}).get("percent_covered")
    if total is not None:
        global_line = f"**Global: {float(total):.2f}%**"
        if floor is not None:
            status = "✅" if float(total) >= floor else "❌"
            global_line = f"{status} {global_line} (floor {floor:.0f}%)"
        lines += [global_line, ""]

    lines += [
        "| | Module | Coverage | Floor | Margin |",
        "|---|---|---:|---:|---:|",
    ]
    can_be_raised = []
    for module in sorted(set(MODULE_THRESHOLDS) | {name for name in files if is_guarded(name)}):
        minimum = MODULE_THRESHOLDS.get(module)
        module_report = files.get(module)
        if module_report is None:
            lines.append(f"| ❌ | `{module}` | absent | {minimum:.0f}% | |")
            continue
        actual = _coverage(module_report)
        if minimum is None:
            lines.append(f"| ❌ | `{module}` | {actual:.2f}% | none | |")
            continue
        status = "✅" if actual >= minimum else "❌"
        lines.append(
            f"| {status} | `{module}` | {actual:.2f}% | {minimum:.0f}% | {actual - minimum:+.2f} |"
        )
        if actual - minimum >= RAISE_HINT_MARGIN:
            can_be_raised.append(f"`{module}` ({minimum:.0f}% → {int(actual)}%)")

    lines.append("")
    if failures:
        lines.append(f"**{len(failures)} coverage floor(s) not met.**")
    else:
        lines.append(f"All {len(MODULE_THRESHOLDS)} coverage floors are met.")
    if can_be_raised:
        lines += ["", "Floors that can be raised: " + ", ".join(can_be_raised) + "."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path, nargs="?", default=Path("coverage.json"))
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="append a Markdown coverage report to this file (e.g. $GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args()

    report = load_report(args.report)
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as summary_file:
            summary_file.write(render_summary(report, global_floor()))

    failures = check_report(report)
    if failures:
        print("Per-module coverage thresholds failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Per-module coverage thresholds passed ({len(MODULE_THRESHOLDS)} modules).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
