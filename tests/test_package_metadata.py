from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml


def _workflow(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    assert isinstance(data, dict)
    return data


def _job_run_commands(workflow: dict, job_name: str) -> list[str]:
    jobs = workflow["jobs"]
    steps = jobs[job_name]["steps"]
    return [step["run"] for step in steps if "run" in step]


def _job_uses(workflow: dict, job_name: str) -> list[str]:
    jobs = workflow["jobs"]
    steps = jobs[job_name]["steps"]
    return [step["uses"] for step in steps if "uses" in step]


def test_flake_version_matches_project_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    expected_version = project["project"]["version"]
    flake_text = Path("flake.nix").read_text()

    match = re.search(r'\bversion = "([^"]+)";', flake_text)

    assert match is not None
    assert match.group(1) == expected_version


def test_readme_images_use_package_metadata_safe_urls() -> None:
    readme = Path("README.md").read_text()
    relative_image_links = re.findall(r"!\[[^\]]*]\((images/[^)]+)\)", readme)

    assert relative_image_links == []


def test_ci_and_publish_workflows_match_documented_release_gate() -> None:
    ci = _workflow(".github/workflows/ci.yml")
    publish = _workflow(".github/workflows/python-publish.yml")

    expected_matrix = ["3.11", "3.12", "3.13"]
    assert ci["jobs"]["test"]["strategy"]["matrix"]["python-version"] == expected_matrix
    assert publish["jobs"]["test"]["strategy"]["matrix"]["python-version"] == expected_matrix

    required_test_commands = {
        'uv pip install -e ".[dev]"',
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy hermesd",
        "uv run pytest tests/ -v -W error::ResourceWarning",
        "uv run pip-audit",
        "uv lock --check",
    }
    for workflow in (ci, publish):
        assert required_test_commands <= set(_job_run_commands(workflow, "test"))

    ci_commands = "\n".join(_job_run_commands(ci, "test"))
    assert "uv build" in ci_commands
    assert "uv run python -m venv .wheel-smoke" in ci_commands
    assert ".wheel-smoke/bin/python -m pip install dist/*.whl" in ci_commands
    assert ".wheel-smoke/bin/hermesd --version" in ci_commands
    assert ".wheel-smoke/bin/python -m hermesd --version" in ci_commands
    assert "uvx twine check dist/*" in ci_commands

    release_commands = "\n".join(_job_run_commands(publish, "release-build"))
    assert "uv lock --check" in release_commands
    assert "uv build" in release_commands
    assert ".wheel-smoke/bin/hermesd --version" in release_commands
    assert ".wheel-smoke/bin/python -m hermesd --version" in release_commands
    assert "uvx twine check dist/*" in release_commands
    assert "actions/upload-artifact@v7" in _job_uses(publish, "release-build")
    assert "actions/download-artifact@v8" in _job_uses(publish, "pypi-publish")
    assert publish["jobs"]["pypi-publish"]["permissions"]["id-token"] == "write"
    assert publish["jobs"]["pypi-publish"]["environment"]["name"] == "pypi"
