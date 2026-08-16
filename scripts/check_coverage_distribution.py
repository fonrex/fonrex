"""Fail CI when critical modules hide below the global coverage average."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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
}


def check_distribution(report_path: Path) -> list[str]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = report.get("files", {})
    failures = []
    for module, minimum in MODULE_THRESHOLDS.items():
        module_report = files.get(module)
        if module_report is None:
            failures.append(f"{module}: absent from coverage report")
            continue
        actual = float(module_report["summary"]["percent_covered"])
        if actual < minimum:
            failures.append(f"{module}: {actual:.2f}% < {minimum:.2f}%")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path, nargs="?", default=Path("coverage.json"))
    args = parser.parse_args()

    failures = check_distribution(args.report)
    if failures:
        print("Per-module coverage thresholds failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Per-module coverage thresholds passed ({len(MODULE_THRESHOLDS)} modules).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
