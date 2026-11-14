"""The files of the project, as the guard tests read them.

A guard reads the files Git would commit: the tracked files and the new files that
.gitignore does not exclude. A virtual environment, downloads or local data are never
read, and a guard sees on a machine what it sees on the CI, which has a fresh checkout.
The list is built once per test session, and each Python file is parsed once.

Each guard reads a scope of GUARD_SCOPES. GUARD_SCOPES and SKIPPED_DIRS are ratchets
(tests/test_guard_ratchets.py): what they leave out may shrink, never grow.
"""

import ast
import fnmatch
import functools
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directories never read, wherever they are: local scratch work, also left out of
# compileall and of the pytest collection.
SKIPPED_DIRS = ("scratch",)

# The directories each scope leaves out, wherever they are. ".*" names every hidden
# directory.
GUARD_SCOPES = {
    # Everything the project runs or ships.
    "code": ("tests",),
    # The same, but the migrations, which create the schema by design.
    "code_without_migrations": ("tests", "alembic"),
    # The code of the API and its services, without the operator scripts.
    "service_code": ("tests", "scripts", "alembic"),
    # Where a setting of .env.example can be read: not where it is only written about.
    "settings_readers": ("tests", "data", "static", "logs", "docs", "img", ".*"),
}

# Without Git, the directories a walk of the repository skips.
_NOT_PROJECT = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
_NOT_PROJECT |= {".ruff_cache", ".mypy_cache"}


def _listed_by_git() -> list[str] | None:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return [name for name in completed.stdout.decode("utf-8").split("\0") if name]


def _walked() -> list[str]:
    """Without Git (a source archive): every file outside environments and caches."""
    names = []
    for directory, subdirectories, files in os.walk(PROJECT_ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in _NOT_PROJECT]
        names.extend(
            (Path(directory) / name).relative_to(PROJECT_ROOT).as_posix() for name in files
        )
    return names


def _directories(path: Path) -> tuple[str, ...]:
    return path.relative_to(PROJECT_ROOT).parts[:-1]


def _leaves_out(path: Path, directories: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(part, pattern) for part in _directories(path) for pattern in directories
    )


@functools.cache
def _all_files() -> tuple[Path, ...]:
    names = _listed_by_git()
    if names is None:
        names = _walked()
    paths = {PROJECT_ROOT / name for name in names}
    # A tracked file deleted in the working tree is still listed by Git.
    return tuple(
        sorted(path for path in paths if path.is_file() and not _leaves_out(path, SKIPPED_DIRS))
    )


@functools.cache
def project_files(*patterns: str) -> tuple[Path, ...]:
    """The files of the project whose name matches one of the patterns, sorted."""
    return tuple(
        path
        for path in _all_files()
        if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in patterns)
    )


def python_files() -> tuple[Path, ...]:
    return project_files("*.py")


def scope(name: str, *patterns: str) -> tuple[Path, ...]:
    """The files of a scope of GUARD_SCOPES (Python files unless patterns are given)."""
    return tuple(
        path
        for path in project_files(*(patterns or ("*.py",)))
        if not _leaves_out(path, GUARD_SCOPES[name])
    )


@functools.cache
def syntax_tree(path: Path) -> ast.Module:
    """The parsed source of a file, shared by every guard of the session."""
    return ast.parse(path.read_text(encoding="utf-8"))


def relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()
