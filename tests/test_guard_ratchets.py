"""Ratchets: the guards and the quality settings never loosen compared with main.

A guard test can be made green by editing its own exceptions instead of fixing
the code: a module added to an allowlist, a line budget raised, a coverage floor
lowered, a directory added to an exclusion. Each ratchet below reads a list or a
number as it is on main and as it is now, and fails when it moved the loose way.

The baseline is ``origin/main`` (fetched by .github/workflows/quality.yml), else
the local ``main``. On the CI a missing baseline fails the test; on a machine
without one the test is skipped. A list that does not exist on main yet has no
baseline: its ratchet starts once it is merged.

Loosening a rule on purpose is a decision for a human, stated in the pull request
(AGENTS.md, "When a guard test fails"); it never happens to get a green run.
"""

import ast
import configparser
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_REFS = ("origin/main", "main")


# ── Baseline ─────────────────────────────────────────────────────────────────


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    )


def baseline_ref() -> str | None:
    """The first reference of main found in the repository, or None."""
    if shutil.which("git") is None or not (PROJECT_ROOT / ".git").exists():
        return None
    for ref in BASELINE_REFS:
        if _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0:
            return ref
    return None


def require_baseline_ref(ref: str | None) -> str:
    """Fail on the CI, skip elsewhere, when there is no main to compare with."""
    if ref is None:
        message = "no main branch to compare the guards with (run: git fetch origin main)"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(
                f"{message}: the workflow must fetch origin/main before the tests",
                pytrace=False,
            )
        pytest.skip(message)
    return ref


def baseline_text(ref: str, relative_path: str) -> str | None:
    """The file as it is on ``ref``, or None when it does not exist there."""
    result = _git("show", f"{ref}:{relative_path}")
    return result.stdout if result.returncode == 0 else None


# ── Readers: a value out of a file's text, None when it is absent ────────────


def python_constant(name: str) -> Callable[[str], object | None]:
    """A literal assigned to ``name``, at module or class level."""

    def read(source: str) -> object | None:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            else:
                continue
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return ast.literal_eval(node.value)
        return None

    return read


def toml_value(*keys: str) -> Callable[[str], object | None]:
    def read(source: str) -> object | None:
        value: object = tomllib.loads(source)
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value

    return read


def toml_ignore_pairs(source: str) -> set[str] | None:
    """Each ``pattern: rule`` pair of Ruff's per-file-ignores."""
    ignores = toml_value("tool", "ruff", "lint", "per-file-ignores")(source)
    if ignores is None:
        return None
    return {f"{pattern}: {rule}" for pattern, rules in ignores.items() for rule in rules}


# mypy settings that let more code through when they are set.
LOOSENING_MYPY_PREFIXES = ("ignore_", "allow_", "implicit_")
LOOSENING_MYPY_KEYS = {"disable_error_code", "follow_imports", "exclude"}


def mypy_relaxations(source: str) -> set[str] | None:
    """Each loosening setting of the mypy configuration, per module it applies to."""
    mypy = toml_value("tool", "mypy")(source)
    if not isinstance(mypy, dict):
        return None
    scopes = [("*", mypy)]
    for override in mypy.get("overrides", []):
        modules = override.get("module", [])
        for module in [modules] if isinstance(modules, str) else modules:
            scopes.append((module, override))
    relaxed = set()
    for scope, settings in scopes:
        for key, value in settings.items():
            if key in {"module", "overrides"}:
                continue
            if (
                key.startswith(LOOSENING_MYPY_PREFIXES)
                or key in LOOSENING_MYPY_KEYS
                or value is False
            ):
                relaxed.add(f"{scope}: {key} = {value!r}")
    return relaxed


def ini_values(section: str, key: str) -> Callable[[str], list[str] | None]:
    """A list option of an ini file, one item per line or per word."""

    def read(source: str) -> list[str] | None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(source)
        if not parser.has_option(section, key):
            return None
        raw = parser.get(section, key)
        if "\n" in raw.strip():
            return [line.strip() for line in raw.splitlines() if line.strip()]
        return raw.split()

    return read


def make_variable(name: str) -> Callable[[str], list[str] | None]:
    def read(source: str) -> list[str] | None:
        match = re.search(rf"^{re.escape(name)}\s*:=\s*(.*)$", source, re.MULTILINE)
        return match[1].split() if match else None

    return read


# ── Checks: what moved the loose way, as messages ────────────────────────────


def _exists(module: str) -> bool:
    return (PROJECT_ROOT / module).exists()


def may_shrink_never_grow(baseline, current) -> list[str]:
    """An allowlist or an exclusion: no new entry."""
    return [f"added {entry!r}" for entry in sorted(set(current) - set(baseline))]


def exclusion_never_grows(*harmless: str) -> Callable[[object, object], list[str]]:
    """An exclusion: no new entry, except those that name no code of the project.

    ``harmless`` lists the entries that only point to installed packages (the
    virtual environments) or restore a default of the tool. Keep it to those:
    an entry naming a directory of the repository does not belong here.
    """

    def check(baseline, current) -> list[str]:
        return may_shrink_never_grow(set(baseline) | set(harmless), current)

    return check


def may_grow_never_shrink(baseline, current) -> list[str]:
    """A list of rules or of guarded modules: no entry removed."""
    return [f"removed {entry!r}" for entry in sorted(set(baseline) - set(current))]


def guarded_paths_never_dropped(baseline, current) -> list[str]:
    """A list of guarded modules: an entry leaves it only with its module."""
    return [
        f"removed {entry!r}, which still exists"
        for entry in sorted(set(baseline) - set(current))
        if _exists(entry)
    ]


def floors_never_drop(baseline, current) -> list[str]:
    """Coverage floors per module: never lowered, never removed while the module exists."""
    loosened = []
    for module, floor in sorted(baseline.items()):
        if module not in current:
            if _exists(module):
                loosened.append(f"removed the floor of {module!r} ({floor})")
        elif current[module] < floor:
            loosened.append(f"lowered {module!r}: {floor} -> {current[module]}")
    return loosened


def budgets_never_rise(baseline, current) -> list[str]:
    """Maximum sizes per module: never raised, never removed while the module exists."""
    loosened = []
    for module, budget in sorted(baseline.items()):
        if module not in current:
            if _exists(module):
                loosened.append(f"removed the budget of {module!r} ({budget})")
        elif current[module] > budget:
            loosened.append(f"raised {module!r}: {budget} -> {current[module]}")
    return loosened


def number_never_drops(baseline, current) -> list[str]:
    return [f"lowered: {baseline} -> {current}"] if current < baseline else []


def scopes_never_widen(baseline, current) -> list[str]:
    """What each guard scope leaves out never grows; a new scope leaves out no more than
    an existing one."""
    loosened = []
    for name, left_out in sorted(current.items()):
        if name in baseline:
            added = sorted(set(left_out) - set(baseline[name]))
            loosened.extend(f"{name!r} also leaves out {directory!r}" for directory in added)
        elif not any(set(left_out) <= set(known) for known in baseline.values()):
            loosened.append(f"new scope {name!r} leaves out {sorted(left_out)}")
    return loosened


def stays_on(baseline, current) -> list[str]:
    return (
        [f"turned off: {baseline!r} -> {current!r}"]
        if baseline is True and current is not True
        else []
    )


# ── The ratchets ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Ratchet:
    name: str
    path: str
    read: Callable[[str], object | None]
    loosened: Callable[[object, object], list[str]]


RATCHETS = [
    # Coverage (AGENTS.md rule 6)
    Ratchet(
        "module coverage floors",
        "scripts/check_coverage_distribution.py",
        python_constant("MODULE_THRESHOLDS"),
        floors_never_drop,
    ),
    Ratchet(
        "global coverage floor",
        "pyproject.toml",
        toml_value("tool", "coverage", "report", "fail_under"),
        number_never_drops,
    ),
    Ratchet(
        "files left out of the coverage",
        "pyproject.toml",
        toml_value("tool", "coverage", "run", "omit"),
        exclusion_never_grows(".venv/*", "venv/*"),
    ),
    Ratchet(
        "lines left out of the coverage",
        "pyproject.toml",
        toml_value("tool", "coverage", "report", "exclude_also"),
        may_shrink_never_grow,
    ),
    # Lint and typing
    Ratchet(
        "Ruff rules",
        "pyproject.toml",
        toml_value("tool", "ruff", "lint", "select"),
        may_grow_never_shrink,
    ),
    Ratchet(
        "paths left out of Ruff",
        "pyproject.toml",
        toml_value("tool", "ruff", "extend-exclude"),
        exclusion_never_grows(".venv", "venv"),
    ),
    Ratchet("Ruff per-file ignores", "pyproject.toml", toml_ignore_pairs, may_shrink_never_grow),
    Ratchet(
        "typed boundaries of make typecheck",
        "Makefile",
        make_variable("TYPED_BOUNDARIES"),
        guarded_paths_never_dropped,
    ),
    Ratchet("mypy strict mode", "pyproject.toml", toml_value("tool", "mypy", "strict"), stays_on),
    Ratchet("mypy relaxations", "pyproject.toml", mypy_relaxations, may_shrink_never_grow),
    # Test collection
    Ratchet(
        "directories pytest does not collect",
        "pytest.ini",
        ini_values("pytest", "norecursedirs"),
        # ".*" and "venv" are defaults of pytest that setting the option replaces.
        exclusion_never_grows(".*", ".venv", "venv"),
    ),
    Ratchet(
        "warnings pytest ignores",
        "pytest.ini",
        ini_values("pytest", "filterwarnings"),
        may_shrink_never_grow,
    ),
    # Files the guards read (tests/project_sources.py)
    Ratchet(
        "directories no guard reads",
        "tests/project_sources.py",
        python_constant("SKIPPED_DIRS"),
        may_shrink_never_grow,
    ),
    Ratchet(
        "directories left out of the guard scopes",
        "tests/project_sources.py",
        python_constant("GUARD_SCOPES"),
        scopes_never_widen,
    ),
    # Architecture guards
    Ratchet(
        "modules with their own HTTP client",
        "tests/test_provider_http_policy.py",
        python_constant("LEGACY_DIRECT_HTTP_CLIENTS"),
        may_shrink_never_grow,
    ),
    Ratchet(
        "modules calling threads directly",
        "tests/test_async_boundary.py",
        python_constant("ALLOWED_THREAD_MODULES"),
        may_shrink_never_grow,
    ),
    Ratchet(
        "modules catching typed exceptions only",
        "tests/test_exception_boundaries.py",
        python_constant("TYPED_EXCEPTION_MODULES"),
        guarded_paths_never_dropped,
    ),
    Ratchet(
        "packages catching typed exceptions only",
        "tests/test_exception_boundaries.py",
        python_constant("TYPED_EXCEPTION_PACKAGES"),
        guarded_paths_never_dropped,
    ),
    Ratchet(
        "boundaries that never catch every exception",
        "tests/test_exception_boundaries.py",
        python_constant("REFACTORED_BOUNDARY_MODULES"),
        guarded_paths_never_dropped,
    ),
    Ratchet(
        "line budgets of the orchestrators",
        "tests/test_exception_boundaries.py",
        python_constant("LINE_BUDGETS"),
        budgets_never_rise,
    ),
    Ratchet(
        "recovery functions of the realtime worker",
        "tests/test_exception_boundaries.py",
        python_constant("WORKER_RECOVERY_FUNCTIONS"),
        may_shrink_never_grow,
    ),
]


@pytest.fixture(scope="module")
def ref() -> str:
    return require_baseline_ref(baseline_ref())


@pytest.mark.parametrize("ratchet", RATCHETS, ids=lambda ratchet: ratchet.name)
def test_guard_never_loosens_compared_with_main(ratchet: Ratchet, ref: str):
    source = baseline_text(ref, ratchet.path)
    baseline = ratchet.read(source) if source is not None else None
    if baseline is None:
        pytest.skip(f"{ratchet.name}: not on {ref} yet, the ratchet starts once merged")

    path = PROJECT_ROOT / ratchet.path
    current = ratchet.read(path.read_text(encoding="utf-8")) if path.is_file() else None
    assert current is not None, (
        f"{ratchet.name}: {ratchet.path} no longer defines it; the ratchet reads it there"
    )

    loosened = ratchet.loosened(baseline, current)
    assert not loosened, (
        f"{ratchet.name} in {ratchet.path} loosened compared with {ref}: "
        + "; ".join(loosened)
        + ". Fix the code instead (AGENTS.md, 'When a guard test fails')."
    )


def test_guards_read_the_project_through_project_sources():
    """A guard walking the repository itself would choose its own exclusions."""
    walks = []
    for path in sorted((PROJECT_ROOT / "tests").glob("*.py")):
        if path.name == "project_sources.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"rglob", "walk"}
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in {"ROOT", "PROJECT_ROOT", "PROJECT", "root"}
            ) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "rglob"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"ROOT", "PROJECT_ROOT", "PROJECT", "root"}
            ):
                walks.append(f"tests/{path.name}:{node.lineno}")
    assert not walks, (
        f"Guards walking the whole repository: {walks}. Read it with "
        "tests/project_sources.py (python_files, scope), whose exclusions are ratchets."
    )


def test_every_ratchet_reads_a_value_of_the_working_tree():
    """A renamed constant or setting would silently turn its ratchet off."""
    missing = [
        f"{ratchet.name} ({ratchet.path})"
        for ratchet in RATCHETS
        if ratchet.read((PROJECT_ROOT / ratchet.path).read_text(encoding="utf-8")) is None
    ]
    assert not missing, f"Ratchets reading nothing: {missing}"


# ── Behaviour of the readers and checks, without Git ─────────────────────────


class TestReaders:
    def test_python_constant_at_module_and_class_level(self):
        source = 'A = {"x": 1}\nclass T:\n    B = {"a.py"}\n    C: tuple = ("c",)\n'
        assert python_constant("A")(source) == {"x": 1}
        assert python_constant("B")(source) == {"a.py"}
        assert python_constant("C")(source) == ("c",)
        assert python_constant("D")(source) is None

    def test_toml_value_and_ignore_pairs(self):
        source = (
            "[tool.coverage.report]\nfail_under = 70\n"
            '[tool.ruff.lint.per-file-ignores]\n"a.py" = ["E402", "E741"]\n'
        )
        assert toml_value("tool", "coverage", "report", "fail_under")(source) == 70
        assert toml_value("tool", "missing")(source) is None
        assert toml_ignore_pairs(source) == {"a.py: E402", "a.py: E741"}
        assert toml_ignore_pairs("") is None

    def test_ini_values_on_one_line_and_several(self):
        source = (
            "[pytest]\nnorecursedirs = venv .venv\nfilterwarnings =\n    ignore:a\n    ignore:b\n"
        )
        assert ini_values("pytest", "norecursedirs")(source) == ["venv", ".venv"]
        assert ini_values("pytest", "filterwarnings")(source) == ["ignore:a", "ignore:b"]
        assert ini_values("pytest", "missing")(source) is None

    def test_mypy_relaxations_per_module(self):
        source = (
            '[tool.mypy]\nstrict = true\nfollow_imports = "silent"\nwarn_unreachable = true\n'
            '[[tool.mypy.overrides]]\nmodule = ["pandas", "pandas.*"]\n'
            "ignore_missing_imports = true\ncheck_untyped_defs = false\n"
        )
        assert mypy_relaxations(source) == {
            "*: follow_imports = 'silent'",
            "pandas: ignore_missing_imports = True",
            "pandas.*: ignore_missing_imports = True",
            "pandas: check_untyped_defs = False",
            "pandas.*: check_untyped_defs = False",
        }
        assert mypy_relaxations("[tool.ruff]\n") is None

    def test_make_variable(self):
        source = "X := 1\nTYPED_BOUNDARIES := a.py b.py\n"
        assert make_variable("TYPED_BOUNDARIES")(source) == ["a.py", "b.py"]
        assert make_variable("NOPE")(source) is None


class TestChecks:
    def test_allowlist_may_shrink_but_never_grow(self):
        assert may_shrink_never_grow({"a", "b"}, {"a"}) == []
        assert may_shrink_never_grow({"a"}, {"a", "b"}) == ["added 'b'"]

    def test_exclusion_accepts_only_its_harmless_entries(self):
        check = exclusion_never_grows(".venv")
        assert check(["data"], ["data", ".venv"]) == []
        assert check(["data"], ["data", ".venv", "macro"]) == ["added 'macro'"]

    def test_rules_may_grow_but_never_shrink(self):
        assert may_grow_never_shrink(["E4"], ["E4", "F"]) == []
        assert may_grow_never_shrink(["E4", "F"], ["E4"]) == ["removed 'F'"]

    def test_guarded_path_leaves_only_with_its_module(self):
        assert guarded_paths_never_dropped(["Makefile", "gone/nowhere.py"], []) == [
            "removed 'Makefile', which still exists"
        ]

    def test_floors_never_drop(self):
        assert floors_never_drop({"Makefile": 80.0}, {"Makefile": 85.0}) == []
        assert floors_never_drop({"Makefile": 80.0}, {"Makefile": 79.0}) == [
            "lowered 'Makefile': 80.0 -> 79.0"
        ]
        assert floors_never_drop({"Makefile": 80.0}, {}) == [
            "removed the floor of 'Makefile' (80.0)"
        ]
        assert floors_never_drop({"gone/nowhere.py": 80.0}, {}) == []

    def test_budgets_never_rise(self):
        assert budgets_never_rise({"Makefile": 500}, {"Makefile": 450}) == []
        assert budgets_never_rise({"Makefile": 500}, {"Makefile": 501}) == [
            "raised 'Makefile': 500 -> 501"
        ]
        assert budgets_never_rise({"Makefile": 500}, {}) == [
            "removed the budget of 'Makefile' (500)"
        ]

    def test_scopes_never_widen(self):
        baseline = {"code": ("tests",), "service": ("tests", "scripts")}
        assert scopes_never_widen(baseline, {"code": (), "service": ("tests",)}) == []
        assert scopes_never_widen(baseline, {"code": ("tests", "macro")}) == [
            "'code' also leaves out 'macro'"
        ]
        assert scopes_never_widen(baseline, {"other": ("scripts",)}) == []
        assert scopes_never_widen(baseline, {"other": ("macro",)}) == [
            "new scope 'other' leaves out ['macro']"
        ]

    def test_strict_mode_stays_on(self):
        assert stays_on(True, True) == []
        assert stays_on(True, False) == ["turned off: True -> False"]
        assert stays_on(None, False) == []

    def test_number_never_drops(self):
        assert number_never_drops(70, 75) == []
        assert number_never_drops(75, 70) == ["lowered: 75 -> 70"]


class TestMissingBaseline:
    def test_fails_on_the_ci(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with pytest.raises(pytest.fail.Exception, match="must fetch origin/main"):
            require_baseline_ref(None)

    def test_is_skipped_elsewhere(self, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with pytest.raises(pytest.skip.Exception):
            require_baseline_ref(None)

    def test_a_found_reference_is_returned(self):
        assert require_baseline_ref("origin/main") == "origin/main"
