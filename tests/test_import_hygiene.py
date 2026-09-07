"""Critical-rule guard for hermesd production imports.

Parses every hermesd/**/*.py with ast, collects all imported module names, and
allows only the standard library, local modules, and declared runtime imports.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "hermesd"
_PROJECT_ROOT = _PACKAGE_ROOT.parent
_DECLARED_RUNTIME_IMPORT_ROOTS = {"pydantic", "rich", "yaml"}
_ALLOWED_IMPORT_ROOTS = {*sys.stdlib_module_names, "hermesd", *_DECLARED_RUNTIME_IMPORT_ROOTS}


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def _undeclared_import_roots(tree: ast.AST) -> set[str]:
    return {
        module.partition(".")[0]
        for module in _imported_module_names(tree)
        if module.partition(".")[0] not in _ALLOWED_IMPORT_ROOTS
    }


def test_production_imports_are_stdlib_local_or_declared_runtime_dependencies():
    offenders: dict[str, set[str]] = {}
    source_files = sorted(_PACKAGE_ROOT.rglob("*.py"))
    assert source_files, "expected to find hermesd source files to scan"

    for source_file in source_files:
        tree = ast.parse(source_file.read_text(), filename=str(source_file))
        undeclared = _undeclared_import_roots(tree)
        if undeclared:
            offenders[str(source_file.relative_to(_PACKAGE_ROOT.parent))] = undeclared

    assert offenders == {}, f"production imports must be stdlib, local, or declared: {offenders}"


@pytest.mark.parametrize(
    ("statement", "expected_root"),
    [
        ("from agent import Agent", "agent"),
        ("from hermes_cli import main", "hermes_cli"),
        ("import gateway.run", "gateway"),
        ("import run_agent", "run_agent"),
    ],
)
def test_import_guard_rejects_actual_hermes_agent_import_roots(
    statement: str,
    expected_root: str,
):
    assert _undeclared_import_roots(ast.parse(statement)) == {expected_root}


def _normalized_dependency_name(requirement: str) -> str:
    name = requirement.split(";", 1)[0].split("[", 1)[0]
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
        name = name.split(separator, 1)[0]
    return name.strip().lower().replace("_", "-")


def test_package_metadata_has_no_hermes_agent_dependency():
    project = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text())
    dependencies = list(project["project"].get("dependencies", []))
    for optional_dependencies in project["project"].get("optional-dependencies", {}).values():
        dependencies.extend(optional_dependencies)

    forbidden = {
        dependency
        for dependency in dependencies
        if _normalized_dependency_name(dependency) == "hermes-agent"
    }

    assert forbidden == set(), f"hermes-agent package dependency is forbidden: {forbidden}"
