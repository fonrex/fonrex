"""The lock files freeze the versions installed by Docker, the CI and ``make install``.

``requirements*.txt`` say what the project needs, as ranges. ``requirements*.lock``
record the exact versions that were tested, with their hashes. Without them two
builds of the same commit install different packages. Refresh them with
``make lock`` after editing a requirements file.
"""

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# requirements file -> the lock generated from it
LOCKS = {
    "requirements.txt": "requirements.lock",
    "requirements-dev.txt": "requirements-dev.lock",
}

PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)")
HOW_TO_FIX = "Run `make lock` and commit the lock files."


def _read(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _direct_requirements(name: str) -> list[Requirement]:
    """Requirements declared in a file, following its ``-r`` includes."""
    requirements = []
    for raw in _read(name).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r"):
            requirements += _direct_requirements(line[2:].strip())
        else:
            requirements.append(Requirement(line))
    return requirements


def _lock_entries(name: str) -> list[tuple[str, Version, list[str]]]:
    """Each pinned package of a lock file with the lines that belong to it."""
    entries: list[tuple[str, Version, list[str]]] = []
    for line in _read(name).splitlines():
        match = PIN.match(line)
        if match:
            entries.append(
                (canonicalize_name(match["name"]), Version(match["version"]), [line])
            )
        elif entries and line.startswith(" "):
            entries[-1][2].append(line)
    return entries


def _locked_versions(name: str) -> dict[str, Version]:
    return {package: version for package, version, _lines in _lock_entries(name)}


@pytest.mark.parametrize(("requirements", "lock"), LOCKS.items())
class TestLockMatchesRequirements:
    def test_every_declared_package_is_locked_within_its_range(self, requirements, lock):
        locked = _locked_versions(lock)
        for requirement in _direct_requirements(requirements):
            package = canonicalize_name(requirement.name)
            assert package in locked, f"{requirement.name} is not in {lock}. {HOW_TO_FIX}"
            assert requirement.specifier.contains(locked[package], prereleases=True), (
                f"{lock} pins {requirement.name}=={locked[package]}, outside "
                f"'{requirement.specifier}' declared in {requirements}. {HOW_TO_FIX}"
            )

    def test_every_locked_package_has_one_version_and_hashes(self, requirements, lock):
        entries = _lock_entries(lock)
        assert entries, f"{lock} pins nothing"
        packages = [package for package, _version, _lines in entries]
        assert len(packages) == len(set(packages)), f"{lock} pins a package twice"
        for package, _version, lines in entries:
            assert any("--hash=sha256:" in line for line in lines), (
                f"{package} has no hash in {lock}. {HOW_TO_FIX}"
            )


def test_development_lock_uses_the_runtime_versions():
    runtime = _locked_versions("requirements.lock")
    development = _locked_versions("requirements-dev.lock")
    different = {
        package: (str(version), str(development.get(package)))
        for package, version in runtime.items()
        if development.get(package) != version
    }
    assert not different, (
        f"The tests would not run on the versions of the image: {different}. {HOW_TO_FIX}"
    )


class TestLockIsWhatGetsInstalled:
    def test_docker_image_installs_the_runtime_lock(self):
        dockerfile = _read("Dockerfile")
        assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in dockerfile
        assert "-r requirements.txt" not in dockerfile

    def test_ci_installs_the_development_lock(self):
        workflow = _read(".github/workflows/quality.yml")
        assert "pip install --require-hashes -r requirements-dev.lock" in workflow, (
            "The CI must install requirements-dev.lock (apply ci-lock.patch if it is "
            "still next to this repository)."
        )
        assert "-r requirements-dev.txt" not in workflow

    def test_make_targets_install_the_locks(self):
        makefile = _read("Makefile")
        assert "pip install --require-hashes -r requirements.lock" in makefile
        assert "pip install --require-hashes -r requirements-dev.lock" in makefile
        assert "\nlock:" in makefile, "The `make lock` target is missing"
