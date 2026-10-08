"""The per-module coverage gate run by ``make test-cov`` and the CI."""

import importlib.util
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "check_coverage_distribution.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_coverage_distribution", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

# Fixed floors for the behaviour tests, so raising a real floor never breaks them.
SAMPLE_FLOORS = {
    "cache/adapters.py": 95.0,
    "historical/providers.py": 65.0,
    "financials/providers/base.py": 98.0,
    "financials/providers/justetf.py": 87.0,
}


@pytest.fixture
def sample_floors(monkeypatch):
    monkeypatch.setattr(gate, "MODULE_THRESHOLDS", dict(SAMPLE_FLOORS))


def _report(overrides: dict[str, float] | None = None, *, total: float = 80.0) -> dict:
    """Build a coverage report where every declared module sits exactly on its floor."""
    coverage = dict(gate.MODULE_THRESHOLDS)
    coverage.update(overrides or {})
    return {
        "totals": {"percent_covered": total},
        "files": {
            module: {"summary": {"percent_covered": percent}}
            for module, percent in coverage.items()
        },
    }


class TestDeclaredFloors:
    def test_every_provider_module_declares_a_floor(self):
        on_disk = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for package in gate.GUARDED_PACKAGES
            for path in (PROJECT_ROOT / package).glob("*.py")
            if path.name != "__init__.py"
        }
        missing = on_disk - set(gate.MODULE_THRESHOLDS)
        assert not missing, (
            f"Add a coverage floor for {sorted(missing)} in scripts/check_coverage_distribution.py"
        )

    def test_floors_point_to_existing_modules(self):
        stale = [
            module for module in gate.MODULE_THRESHOLDS if not (PROJECT_ROOT / module).is_file()
        ]
        assert not stale, f"Floors declared for modules that no longer exist: {stale}"

    def test_floors_are_percentages(self):
        for module, floor in gate.MODULE_THRESHOLDS.items():
            assert 0 < floor <= 100, module

    def test_global_floor_is_read_from_pyproject_and_never_lowered(self):
        assert gate.global_floor() >= 70.0

    def test_global_floor_is_optional(self, tmp_path):
        assert gate.global_floor(tmp_path / "missing.toml") is None
        empty = tmp_path / "pyproject.toml"
        empty.write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
        assert gate.global_floor(empty) is None


@pytest.mark.usefixtures("sample_floors")
class TestCheckReport:
    def test_modules_on_their_floor_pass(self):
        assert gate.check_report(_report()) == []

    def test_module_below_its_floor_fails(self):
        failures = gate.check_report(_report({"financials/providers/justetf.py": 86.5}))

        assert failures == ["financials/providers/justetf.py: 86.50% < 87.00%"]

    def test_module_absent_from_the_report_fails(self):
        report = _report()
        del report["files"]["financials/providers/base.py"]

        assert gate.check_report(report) == [
            "financials/providers/base.py: absent from coverage report"
        ]

    def test_provider_without_a_floor_fails(self):
        failures = gate.check_report(_report({"financials/providers/New_provider.py": 12.0}))

        assert len(failures) == 1
        assert failures[0].startswith(
            "financials/providers/New_provider.py: no coverage floor declared"
        )
        assert "12.00%" in failures[0]

    def test_package_init_and_other_packages_need_no_floor(self):
        report = _report({"financials/providers/__init__.py": 100.0, "routers/quotes.py": 3.0})

        assert gate.check_report(report) == []

    def test_check_distribution_reads_the_report_file(self, tmp_path):
        path = tmp_path / "coverage.json"
        path.write_text(json.dumps(_report({"cache/adapters.py": 10.0})), encoding="utf-8")

        assert gate.check_distribution(path) == ["cache/adapters.py: 10.00% < 95.00%"]


@pytest.mark.usefixtures("sample_floors")
class TestSummary:
    def test_summary_lists_global_and_module_coverage(self):
        summary = gate.render_summary(_report(total=71.64), floor=70.0)

        assert "✅ **Global: 71.64%** (floor 70%)" in summary
        assert "| ✅ | `financials/providers/base.py` | 98.00% | 98% | +0.00 |" in summary
        assert "All 4 coverage floors are met." in summary
        assert "can be raised" not in summary

    def test_summary_flags_failures(self):
        report = _report(
            {"financials/providers/base.py": 50.0, "financials/providers/New_provider.py": 12.0},
            total=60.0,
        )
        del report["files"]["cache/adapters.py"]

        summary = gate.render_summary(report, floor=70.0)

        assert "❌ **Global: 60.00%** (floor 70%)" in summary
        assert "| ❌ | `financials/providers/base.py` | 50.00% | 98% | -48.00 |" in summary
        assert "| ❌ | `financials/providers/New_provider.py` | 12.00% | none | |" in summary
        assert "| ❌ | `cache/adapters.py` | absent | 95% | |" in summary
        assert "**3 coverage floor(s) not met.**" in summary

    def test_summary_suggests_raising_lagging_floors(self):
        summary = gate.render_summary(_report({"historical/providers.py": 76.43}))

        assert "Floors that can be raised: `historical/providers.py` (65% → 76%)." in summary

    def test_summary_without_global_floor(self):
        assert "**Global: 80.00%**\n" in gate.render_summary(_report())


@pytest.mark.usefixtures("sample_floors")
class TestCommandLine:
    def _run(self, monkeypatch, tmp_path, report, *extra):
        path = tmp_path / "coverage.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["check_coverage_distribution.py", str(path), *extra])
        return gate.main()

    def test_passing_report_exits_zero(self, monkeypatch, tmp_path, capsys):
        assert self._run(monkeypatch, tmp_path, _report()) == 0
        assert "thresholds passed" in capsys.readouterr().out

    def test_failing_report_exits_one(self, monkeypatch, tmp_path, capsys):
        report = _report({"financials/providers/justetf.py": 1.0})

        assert self._run(monkeypatch, tmp_path, report) == 1
        assert "- financials/providers/justetf.py: 1.00% < 87.00%" in capsys.readouterr().out

    def test_summary_is_appended_even_when_the_gate_fails(self, monkeypatch, tmp_path):
        summary = tmp_path / "summary.md"
        summary.write_text("previous step\n", encoding="utf-8")
        report = _report({"financials/providers/justetf.py": 1.0})

        assert self._run(monkeypatch, tmp_path, report, "--summary", str(summary)) == 1

        content = summary.read_text(encoding="utf-8")
        assert content.startswith("previous step\n## Test coverage")
        assert "**1 coverage floor(s) not met.**" in content
