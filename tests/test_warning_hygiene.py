"""Prevent known deprecation warnings from returning to project code."""

import ast
import functools
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


@functools.lru_cache(maxsize=1)
def _application_trees() -> list[tuple[Path, ast.AST]]:
    trees = []
    for path in sorted(ROOT.rglob("*.py")):
        if {"venv", ".venv", "tests", "scratch", "node_modules"}.intersection(path.parts):
            continue
        trees.append((path, ast.parse(path.read_text(encoding="utf-8"))))
    return trees


def test_application_does_not_use_datetime_utcnow():
    offenders = []
    for path, tree in _application_trees():
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "utcnow"
            for node in ast.walk(tree)
        ):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_application_does_not_use_legacy_query_get():
    offenders = []
    for path, tree in _application_trees():
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Attribute)
            and node.func.value.func.attr == "query"
            for node in ast.walk(tree)
        ):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_ruff_per_file_ignores_match_existing_files():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignores = config.get("tool", {}).get("ruff", {}).get("lint", {}).get("per-file-ignores", {})
    stale = [pattern for pattern in ignores if not list(ROOT.glob(pattern))]
    assert stale == [], f"pyproject.toml has per-file-ignores matching no file: {stale}"
