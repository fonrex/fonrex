"""Guardrails against broad exception handling in core application layers."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _broad_handlers(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None or (
            isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
        ):
            lines.append(node.lineno)
    return lines


def test_no_bare_except_in_application_code():
    offenders = {}
    for path in ROOT.rglob("*.py"):
        if "venv" in path.parts or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and node.type is None
        ]
        if lines:
            offenders[path.relative_to(ROOT).as_posix()] = lines
    assert offenders == {}


def test_core_persistence_and_http_adapters_use_typed_exceptions():
    paths = [ROOT / "cache" / "service.py"]
    paths.extend((ROOT / "database").glob("*.py"))
    paths.extend(
        ROOT / "routers" / name
        for name in ("admin.py", "assets.py", "historical.py", "monitoring.py", "valuation.py")
    )
    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for path in paths
        if (lines := _broad_handlers(path))
    }
    assert offenders == {}


def test_technical_core_does_not_import_framework_or_persistence_packages():
    """Technical policy depends on ports, never concrete infrastructure."""
    forbidden_roots = {"database", "fastapi", "models", "redis", "sqlalchemy"}
    offenders = {}
    for path in (ROOT / "technical").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        forbidden = sorted(imported_roots & forbidden_roots)
        if forbidden:
            offenders[path.relative_to(ROOT).as_posix()] = forbidden
    assert offenders == {}


def test_large_orchestrators_keep_focused_module_boundaries():
    """Prevent extracted responsibilities from drifting back into orchestrators."""
    technical_service = ROOT / "technical" / "indicator_service.py"
    realtime_worker = ROOT / "realtime" / "worker.py"
    canary_monitor = ROOT / "monitoring" / "canary_monitor.py"
    historical_ingestion = ROOT / "historical" / "ingestion_service.py"

    technical_tree = ast.parse(technical_service.read_text(encoding="utf-8"))
    technical_imports = {
        node.module.split(".")[0]
        for node in ast.walk(technical_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    technical_imports.update(
        alias.name.split(".")[0]
        for node in ast.walk(technical_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert technical_imports.isdisjoint({"models", "pandas_ta", "sqlalchemy"})

    realtime_tree = ast.parse(realtime_worker.read_text(encoding="utf-8"))
    class_names = {node.name for node in ast.walk(realtime_tree) if isinstance(node, ast.ClassDef)}
    assert "ConnectionManager" not in class_names

    # Progressive budgets: these modules were 753 and 606 lines respectively.
    assert len(technical_service.read_text(encoding="utf-8").splitlines()) <= 450
    assert len(realtime_worker.read_text(encoding="utf-8").splitlines()) <= 575

    # Extracted catalog/range and provider/normalization responsibilities must
    # not drift back into their former 653- and 708-line orchestrators.
    assert len(canary_monitor.read_text(encoding="utf-8").splitlines()) <= 500
    assert len(historical_ingestion.read_text(encoding="utf-8").splitlines()) <= 500


def test_application_contracts_do_not_regress_to_any_or_untyped_callables():
    """Keep public boundary contracts explicit and machine-checkable."""
    contract_paths = [
        ROOT / "use_cases" / "ports.py",
        ROOT / "technical" / "contracts.py",
        ROOT / "monitoring" / "ports.py",
    ]
    offenders = {}
    for path in contract_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        issues = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Any":
                issues.append((node.lineno, "Any"))
            if isinstance(node, ast.arg) and node.annotation is None and node.arg != "self":
                issues.append((node.lineno, f"argument:{node.arg}"))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is None:
                issues.append((node.lineno, f"return:{node.name}"))
        if issues:
            offenders[path.relative_to(ROOT).as_posix()] = issues
    assert offenders == {}


def test_monitoring_core_does_not_import_sqlalchemy_or_database_models():
    """Monitoring policy depends on ports; SQLAlchemy stays in its adapter."""
    forbidden_roots = {"database", "models", "schemas", "sqlalchemy"}
    offenders = {}
    for path in (ROOT / "monitoring").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        forbidden = sorted(imported_roots & forbidden_roots)
        if forbidden:
            offenders[path.relative_to(ROOT).as_posix()] = forbidden
    assert offenders == {}


def test_refactored_application_boundaries_do_not_catch_every_exception():
    """Unexpected programming errors must remain observable outside recovery loops."""
    paths = [
        ROOT / "cache" / "adapters.py",
        ROOT / "cache" / "technical.py",
        ROOT / "database" / "technical.py",
        ROOT / "technical" / "calculation_engine.py",
        ROOT / "technical" / "indicator_service.py",
        ROOT / "realtime" / "connection_manager.py",
        ROOT / "routers" / "technical.py",
        ROOT / "use_cases" / "fundamentals.py",
    ]
    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for path in paths
        if (lines := _broad_handlers(path))
    }
    assert offenders == {}


def test_realtime_worker_keeps_generic_recovery_only_at_stream_boundaries():
    path = ROOT / "realtime" / "worker.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    recovery_functions = {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            handler.type is None
            or isinstance(handler.type, ast.Name)
            and handler.type.id in {"Exception", "BaseException"}
            for handler in ast.walk(function)
            if isinstance(handler, ast.ExceptHandler)
        )
    }
    assert recovery_functions == {"_stream_ticker", "_run_streamer"}
