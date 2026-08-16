"""Prevent known deprecation warnings from returning to project code."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _application_files():
    for path in ROOT.rglob("*.py"):
        if {"venv", "tests", "scratch"}.intersection(path.parts):
            continue
        yield path


def test_application_does_not_use_datetime_utcnow():
    offenders = []
    for path in _application_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
    for path in _application_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
