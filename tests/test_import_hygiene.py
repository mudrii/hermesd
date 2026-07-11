"""Critical-rule guard: hermesd must never import hermes-agent code.

Parses every hermesd/**/*.py with ast, collects all imported module names, and
asserts none reference the forbidden hermes-agent package.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "hermesd"
_PROJECT_ROOT = _PACKAGE_ROOT.parent
_FORBIDDEN_TOKENS = ("hermes_agent", "hermes-agent")


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_no_module_imports_hermes_agent():
    offenders: dict[str, set[str]] = {}
    source_files = sorted(_PACKAGE_ROOT.rglob("*.py"))
    assert source_files, "expected to find hermesd source files to scan"

    for source_file in source_files:
        tree = ast.parse(source_file.read_text(), filename=str(source_file))
        forbidden = {
            module
            for module in _imported_module_names(tree)
            if any(token in module for token in _FORBIDDEN_TOKENS)
        }
        if forbidden:
            offenders[str(source_file.relative_to(_PACKAGE_ROOT.parent))] = forbidden

    assert offenders == {}, f"hermes-agent imports are forbidden: {offenders}"


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
