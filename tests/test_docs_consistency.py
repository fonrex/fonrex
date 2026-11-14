"""The documents must describe the project as the code really is.

A figure quoted in the README is a product claim: it is checked here against the
code. Figures that change with every commit (number of test files, of
migrations, of tables) are kept out of the documents instead.

The tables of ARCHITECTURE.md that enumerate what exists (routes, migrations,
modules, cache lifetimes, canary assets) are compared with the code: a list
that nothing checks stops being true at the next change.
"""

import importlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute, APIWebSocketRoute

import main
from cache.service import CacheService
from monitoring.canary_catalog import CANARY_ASSETS
from technical.catalog import INDICATOR_REGISTRY

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DOC_DIRS = {
    ".git",
    ".venv",
    "venv",
    "scratch",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
}
REQUIRED_DOCUMENTS = (
    "README.md",
    "ARCHITECTURE.md",
    "AGENTS.md",
    "GEMINI.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    "fonrex-sheets-connector/README.md",
)


def _ignored_by_git(paths: list[str]) -> set[str]:
    """The paths that the .gitignore rules exclude, tracked or not (empty without Git)."""
    if not paths or shutil.which("git") is None or not (PROJECT_ROOT / ".git").exists():
        return set()
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=PROJECT_ROOT,
        input="\n".join(paths),
        capture_output=True,
        text=True,
        check=False,
    )
    return set(result.stdout.split())


def _discover_documents() -> dict[str, str]:
    """Load all tracked/non-ignored Markdown files without requiring manual registration."""
    candidates: list[str] = []
    for directory, subdirectories, files in os.walk(PROJECT_ROOT):
        subdirectories[:] = sorted(d for d in subdirectories if d not in EXCLUDED_DOC_DIRS)
        for filename in sorted(files):
            if filename.endswith(".md"):
                candidates.append((Path(directory) / filename).relative_to(PROJECT_ROOT).as_posix())
    ignored = _ignored_by_git(candidates)
    return {
        rel: (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        for rel in candidates
        if rel not in ignored
    }


DOCUMENTS = _discover_documents()
README = DOCUMENTS["README.md"]
ARCHITECTURE = DOCUMENTS["ARCHITECTURE.md"]
AGENTS = DOCUMENTS["AGENTS.md"]
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
SHEETS_SCRIPT = (PROJECT_ROOT / "fonrex-sheets-connector/Code.gs").read_text(encoding="utf-8")
SHEETS_MANIFEST = json.loads(
    (PROJECT_ROOT / "fonrex-sheets-connector/appsscript.json").read_text(encoding="utf-8")
)

ALL_PROVIDER_SPECS = [(name, module, cls) for name, module, cls in main.PROVIDER_SPECS] + [
    (label, module, cls) for _state, label, module, cls in main.SPECIALIZED_PROVIDER_SPECS
]


def _quoted_counts(text: str, pattern: str) -> list[int]:
    return [int(value) for value in re.findall(pattern, text)]


def _section(title: str) -> str:
    """Text of a ``## title`` section of ARCHITECTURE.md, up to the next one."""
    marker = f"\n## {title}\n"
    assert marker in ARCHITECTURE, f"ARCHITECTURE.md has no section named {title!r}"
    return ARCHITECTURE.split(marker, 1)[1].split("\n## ", 1)[0]


def _application_operations() -> set[tuple[str, str]]:
    """Every (method, path) the application answers, WebSocket routes included.

    The OpenAPI schema is the reference for what is mounted. It leaves out the
    WebSocket routes and the routes hidden from the schema: those are read from
    the routers imported by ``main`` and from the routes declared on the app.
    """
    operations = {
        (method.upper(), path)
        for path, item in main.app.openapi()["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    routes = list(main.app.routes)
    for value in vars(main).values():
        if isinstance(value, APIRouter):
            routes.extend(value.routes)
    for route in routes:
        if isinstance(route, APIWebSocketRoute):
            operations.add(("WS", route.path))
        elif isinstance(route, APIRoute):
            operations.update((method, route.path) for method in route.methods - {"HEAD", "OPTIONS"})
    return operations


class TestProvidersLoad:
    """A provider that cannot be imported disappears silently at start-up."""

    @pytest.mark.parametrize(("name", "module", "cls"), ALL_PROVIDER_SPECS)
    def test_declared_provider_can_be_imported(self, name, module, cls):
        assert hasattr(importlib.import_module(module), cls), f"{name}: {module}.{cls} is missing"

    def test_application_loaded_every_declared_provider(self):
        assert main.app.state.providers_unavailable == {}
        assert set(main.app.state.providers_available) == {
            name for name, _, _ in main.PROVIDER_SPECS
        }


class TestQuotedFigures:
    def test_fundamentals_provider_count(self):
        counts = _quoted_counts(README, r"Fundamentals \((\d+) providers\)")
        counts += _quoted_counts(README, r"│\s+(\d+) providers\s+│")
        assert counts, "the README no longer states the number of providers"
        assert set(counts) == {len(ALL_PROVIDER_SPECS)}

    def test_news_provider_count(self):
        modules = [
            path
            for path in (PROJECT_ROOT / "news" / "providers").glob("*.py")
            if path.name != "__init__.py"
        ]
        for name, text in DOCUMENTS.items():
            counts = _quoted_counts(text, r"(\d+) providers") if "news" in name.lower() else []
            counts += _quoted_counts(text, r"News \((\d+) providers\)")
            counts += _quoted_counts(text, r"[Nn]ews from (\d+) providers")
            counts += _quoted_counts(text, r"NewsService` \+ (\d+) providers")
            assert set(counts) <= {len(modules)}, name

    def test_technical_indicator_count(self):
        counts = _quoted_counts(README, r"Technical Indicators \((\d+) indicators\)")
        counts += _quoted_counts(ARCHITECTURE, r"registry of (\d+) indicators")
        for text in DOCUMENTS.values():
            counts += _quoted_counts(text, r"(?:\*\*)?(\d+) technical indicators(?:\*\*)?")
        assert counts
        assert set(counts) == {len(INDICATOR_REGISTRY)}

    def test_openbb_widget_count(self):
        widgets = json.loads(
            (PROJECT_ROOT / "integrations/openbb/widgets.json").read_text(encoding="utf-8")
        )
        counts = _quoted_counts(README, r"(\d+) available widgets")
        assert counts
        assert set(counts) == {len(widgets)}

    @pytest.mark.parametrize(
        "pattern",
        [r"\d+ test files", r"\(\d+ migrations\)", r"\d+ tables", r"Test coverage \(\d+ files\)"],
    )
    def test_volatile_counts_stay_out_of_the_documents(self, pattern):
        for name, text in DOCUMENTS.items():
            assert not re.search(pattern, text), (
                f"{name} quotes a count that goes stale with every commit ({pattern}): remove it"
            )

    def test_referenced_test_files_exist(self):
        for name, text in DOCUMENTS.items():
            for path in set(re.findall(r"`(tests/test_\w+\.py)`", text)):
                assert (PROJECT_ROOT / path).is_file(), (
                    f"{name} documents {path}, which does not exist"
                )


class TestArchitectureTables:
    """ARCHITECTURE.md enumerates what exists: the enumerations must be complete."""

    def test_routes_table_lists_every_route_of_the_application(self):
        documented = set(
            re.findall(
                r"^\| (GET|POST|PUT|PATCH|DELETE|WS) \| `([^`]+)` \|",
                _section("Main Endpoints"),
                flags=re.MULTILINE,
            )
        )
        real = _application_operations()
        assert real - documented == set(), "routes missing from the routes table"
        assert documented - real == set(), "the routes table describes routes that do not exist"

    def test_migrations_table_lists_every_migration(self):
        documented = set(
            re.findall(
                r"^\| \d+ \| `(\w+\.py)` \|", _section("Alembic Migrations"), flags=re.MULTILINE
            )
        )
        real = {path.name for path in (PROJECT_ROOT / "alembic" / "versions").glob("*.py")}
        assert real - documented == set(), "migrations missing from the migrations table"
        assert documented - real == set(), "the migrations table names files that do not exist"

    def test_module_map_names_existing_modules_and_every_router(self):
        described = re.findall(r"^\| `([^`]+)` \|", _section("Module Map"), flags=re.MULTILINE)
        assert described
        for entry in described:
            assert (PROJECT_ROOT / entry).exists(), f"the module map describes {entry}: not found"
        # A file present on one machine only passes the check above there and fails
        # in the CI: an entry ignored by Git is never in the repository.
        ignored = _ignored_by_git(described)
        assert not ignored, f"the module map describes files Git ignores: {sorted(ignored)}"

        routers = {
            f"routers/{path.name}"
            for path in (PROJECT_ROOT / "routers").glob("*.py")
            if path.name != "__init__.py"
        }
        assert routers - set(described) == set(), "routers missing from the module map"

        packages = {
            path.parent.name
            for path in PROJECT_ROOT.glob("*/__init__.py")
            if path.parent.name not in {"tests", "alembic"}
        }
        covered = {entry.split("/", 1)[0] for entry in described}
        assert packages - covered == set(), "packages missing from the module map"

    def test_cache_lifetimes_match_the_cache_service(self):
        rows = re.findall(
            r"^\|[^|]+\|[^|]+\| ([\d ]+) s[^|]*\| `CacheService`, category `(\w+)` \|",
            _section("Cache And Logging"),
            flags=re.MULTILINE,
        )
        assert rows, "the cache table no longer names the categories of CacheService"
        for seconds, category in rows:
            assert int(seconds.replace(" ", "")) == CacheService.DEFAULT_TTLS[category], category

    def test_canary_table_lists_the_canary_assets_and_their_fields(self):
        table = _section("Provider Health Monitoring").split("**Canary assets:**", 1)[1]
        rows = dict(re.findall(r"^\| `([A-Z.]+)` \| ([a-z_, ]+) \|", table, flags=re.MULTILINE))
        assert set(rows) == set(CANARY_ASSETS)
        for ticker, fields in rows.items():
            assert {field.strip() for field in fields.split(",")} == set(CANARY_ASSETS[ticker])


class TestContributorRules:
    """AGENTS.md is the contract given to coding agents: it must stay accurate."""

    def test_every_guard_named_in_the_rules_exists(self):
        paths = set(re.findall(r"`((?:tests|scripts)/[\w/]+\.py)`", AGENTS))
        assert paths, "AGENTS.md no longer names the tests that enforce its rules"
        for path in paths:
            assert (PROJECT_ROOT / path).is_file(), f"AGENTS.md names {path}, which does not exist"

    @staticmethod
    def _strip_code(text: str) -> str:
        without_fences = re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.DOTALL)
        return re.sub(r"`[^`\n]+`", "", without_fences)

    @staticmethod
    def _heading_slugs(text: str) -> set[str]:
        stripped = re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.DOTALL)
        slugs: set[str] = set()
        counts: dict[str, int] = {}
        for heading in re.findall(r"^#{1,6}\s+(.+)$", stripped, flags=re.MULTILINE):
            cleaned = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading.strip().lower())
            cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
            cleaned = re.sub(r"[^\w\s-]", "", cleaned).replace(" ", "-")
            count = counts.get(cleaned, 0)
            counts[cleaned] = count + 1
            slugs.add(cleaned if count == 0 else f"{cleaned}-{count}")
        return slugs

    @classmethod
    def _local_links(cls, text: str) -> list[tuple[str, str]]:
        return re.findall(
            r"\]\((?!https?://|mailto:)([^)#\s]*)(?:#([^)\s]+))?\)", cls._strip_code(text)
        )

    def test_local_document_links_across_all_documents_resolve(self):
        heading_cache = {
            (PROJECT_ROOT / doc_name).resolve(): self._heading_slugs(doc_text)
            for doc_name, doc_text in DOCUMENTS.items()
        }
        readme_doc_links = 0
        for name, text in DOCUMENTS.items():
            base_dir = (PROJECT_ROOT / name).parent
            targets = self._local_links(text)
            for target, fragment in targets:
                if not target and not fragment:
                    continue
                if target.lower().endswith(IMAGE_SUFFIXES):
                    continue
                resolved = (
                    (PROJECT_ROOT / name).resolve()
                    if not target
                    else (base_dir / target).resolve()
                )
                assert resolved.exists(), f"{name} links to a missing file: {target}"
                if name == "README.md" and target:
                    readme_doc_links += 1
                if fragment and resolved in heading_cache:
                    assert fragment in heading_cache[resolved], (
                        f"{name} links to missing section #{fragment} in {target or name}"
                    )
        assert readme_doc_links > 0, "README.md no longer links to any local document"

    def test_link_extraction_ignores_code_blocks_and_inline_code(self):
        sample = (
            "Real link to [README](README.md#overview).\n"
            "Inline example `[fake](missing_inline.md#nope)` is ignored.\n"
            "```markdown\n[fenced](missing_fenced.md#nope)\n```\n"
        )
        assert self._local_links(sample) == [("README.md", "overview")]

    def test_core_documents_exist(self):
        missing = [doc for doc in REQUIRED_DOCUMENTS if doc not in DOCUMENTS]
        assert not missing, f"Core documents missing: {missing}"

    def test_agent_instruction_entrypoints_delegate_to_agents_md(self):
        for entrypoint in ("GEMINI.md", "CLAUDE.md", ".github/copilot-instructions.md"):
            text = DOCUMENTS[entrypoint]
            assert "AGENTS.md" in text, f"{entrypoint} must delegate to AGENTS.md"

    def test_pull_request_types_match_the_title_check(self):
        workflow = (PROJECT_ROOT / ".github/workflows/pr-title-check.yml").read_text(
            encoding="utf-8"
        )
        allowed = re.findall(r"^\s{12}(\w+)$", workflow.split("types: |", 1)[1], flags=re.MULTILINE)
        assert allowed
        for kind in allowed:
            assert f"`{kind}`" in AGENTS, f"AGENTS.md does not list the pull request type {kind}"


class TestSelfHostedScope:
    """Fonrex has no hosted data API: every client talks to the user's own instance."""

    HOSTED_API = "api.fonrex.io"

    def test_documents_do_not_point_to_a_hosted_data_api(self):
        for name, text in DOCUMENTS.items():
            assert self.HOSTED_API not in text, name

    def test_sheets_connector_has_no_built_in_api_host(self):
        assert self.HOSTED_API not in SHEETS_SCRIPT
        assert "urlFetchWhitelist" not in SHEETS_MANIFEST

    def test_sheets_connector_does_not_require_a_paid_account(self):
        text = DOCUMENTS["fonrex-sheets-connector/README.md"].lower()
        assert "paid plan required" not in text
        assert "relay" not in text

    def test_relay_is_described_as_a_page_relay_in_development(self):
        section = README.split("what Fonrex Relay will be", 1)[1].split("\n## ", 1)[0]
        assert "in development" in section
        assert "does not store or serve financial data" in section
