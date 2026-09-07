from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

CHECKOUT_ACTION = "actions/checkout"
SETUP_UV_ACTION = "astral-sh/setup-uv"
UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact"
DOWNLOAD_ARTIFACT_ACTION = "actions/download-artifact"
PYPI_PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
UV_VERSION = "0.12.10"


def _workflow(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    assert isinstance(data, dict)
    if True in data:
        data["on"] = data.pop(True)
    return data


def _job_run_commands(workflow: dict, job_name: str) -> list[str]:
    jobs = workflow["jobs"]
    steps = jobs[job_name]["steps"]
    return [step["run"] for step in steps if "run" in step]


def _job_uses(workflow: dict, job_name: str) -> list[str]:
    jobs = workflow["jobs"]
    steps = jobs[job_name]["steps"]
    return [step["uses"] for step in steps if "uses" in step]


def _uses_steps(workflow: dict, job_name: str) -> list[dict]:
    jobs = workflow["jobs"]
    steps = jobs[job_name]["steps"]
    return [step for step in steps if "uses" in step]


def _action_step(workflow: dict, job_name: str, action: str) -> dict:
    step = next(
        step for step in _uses_steps(workflow, job_name) if step["uses"].startswith(f"{action}@")
    )
    assert re.fullmatch(rf"{re.escape(action)}@[0-9a-f]{{40}}", step["uses"])
    return step


def _assert_setup_uv_is_pinned(workflow: dict, job_name: str) -> None:
    setup_step = _action_step(workflow, job_name, SETUP_UV_ACTION)
    assert setup_step["with"]["version"] == UV_VERSION


def _assert_all_actions_are_sha_pinned(workflow: dict) -> None:
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])


def test_flake_version_matches_project_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    expected_version = project["project"]["version"]
    flake_text = Path("flake.nix").read_text()

    match = re.search(r'\bversion = "([^"]+)";', flake_text)

    assert match is not None
    assert match.group(1) == expected_version
    assert len(re.findall(r'github:[^/]+/[^/]+/[0-9a-f]{40}"', flake_text)) == 1


def test_readme_images_use_package_metadata_safe_urls() -> None:
    readme = Path("README.md").read_text()
    relative_image_links = re.findall(r"!\[[^\]]*]\((images/[^)]+)\)", readme)

    assert relative_image_links == []


def test_ci_and_publish_workflows_match_documented_release_gate() -> None:
    ci = _workflow(".github/workflows/ci.yml")
    publish = _workflow(".github/workflows/python-publish.yml")

    assert ci["on"] == {
        "push": {"branches": ["main"]},
        "pull_request": None,
        "workflow_dispatch": None,
    }
    assert ci["permissions"] == {"contents": "read"}
    assert ci["concurrency"] == {
        "group": "ci-${{ github.ref }}",
        "cancel-in-progress": True,
    }
    assert publish["on"] == {"release": {"types": ["published"]}}
    assert publish["permissions"] == {"contents": "read"}

    expected_matrix = ["3.11", "3.12", "3.13", "3.14"]
    assert ci["jobs"]["test"]["strategy"]["matrix"]["python-version"] == expected_matrix
    assert publish["jobs"]["test"]["strategy"]["matrix"]["python-version"] == expected_matrix
    assert ci["jobs"]["test"]["runs-on"] == "ubuntu-24.04"
    assert ci["jobs"]["macos"]["runs-on"] == "macos-15"
    assert publish["jobs"]["test"]["runs-on"] == "ubuntu-24.04"
    assert publish["jobs"]["release-build"]["runs-on"] == "ubuntu-24.04"
    assert publish["jobs"]["pypi-publish"]["runs-on"] == "ubuntu-24.04"
    assert publish["jobs"]["release-build"]["if"] == "${{ !github.event.release.prerelease }}"
    assert publish["jobs"]["pypi-publish"]["if"] == "${{ !github.event.release.prerelease }}"
    assert ci["jobs"]["test"]["timeout-minutes"] == 20
    assert publish["jobs"]["test"]["timeout-minutes"] == 20
    assert publish["jobs"]["release-build"]["timeout-minutes"] == 15
    assert publish["jobs"]["pypi-publish"]["timeout-minutes"] == 10
    assert publish["concurrency"] == {
        "group": "publish-${{ github.event.release.tag_name }}",
        "cancel-in-progress": False,
    }
    _assert_all_actions_are_sha_pinned(ci)
    _assert_all_actions_are_sha_pinned(publish)
    _action_step(ci, "test", CHECKOUT_ACTION)
    _action_step(ci, "package", CHECKOUT_ACTION)
    _action_step(ci, "macos", CHECKOUT_ACTION)
    _action_step(publish, "test", CHECKOUT_ACTION)
    _action_step(publish, "release-build", CHECKOUT_ACTION)
    _assert_setup_uv_is_pinned(ci, "test")
    _assert_setup_uv_is_pinned(ci, "package")
    _assert_setup_uv_is_pinned(ci, "macos")
    _assert_setup_uv_is_pinned(publish, "test")
    _assert_setup_uv_is_pinned(publish, "release-build")

    required_test_commands = {
        "uv sync --locked --all-extras --dev",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy hermesd",
        "uv run python -m compileall hermesd",
        "uv run pytest tests/ -v -W error::ResourceWarning --cov=hermesd --cov-report=term-missing",
        "uv run pip-audit",
        "uv lock --check",
    }
    for workflow in (ci, publish):
        assert required_test_commands <= set(_job_run_commands(workflow, "test"))

    ci_package_commands = "\n".join(_job_run_commands(ci, "package"))
    assert "uv build" in ci_package_commands
    assert "uv run python -m venv .wheel-smoke" in ci_package_commands
    assert ".wheel-smoke/bin/python -m pip install dist/*.whl" in ci_package_commands
    assert ".wheel-smoke/bin/hermesd --version" in ci_package_commands
    assert ".wheel-smoke/bin/python -I -m hermesd --version" in ci_package_commands
    assert "uv run python -m venv .sdist-smoke" in ci_package_commands
    assert ".sdist-smoke/bin/python -m pip install dist/*.tar.gz" in ci_package_commands
    assert ".sdist-smoke/bin/hermesd --version" in ci_package_commands
    assert ".sdist-smoke/bin/python -I -m hermesd --version" in ci_package_commands
    assert "uv run twine check dist/*" in ci_package_commands
    assert {
        "uv sync --locked --all-extras --dev",
        "uv run pytest tests/ -v -W error::ResourceWarning --cov=hermesd --cov-report=term-missing",
    } <= set(_job_run_commands(ci, "macos"))
    assert "docker run --rm hermesd-ci --version" in "\n".join(_job_run_commands(ci, "docker"))
    assert "nix flake check --no-write-lock-file" in _job_run_commands(ci, "nix")

    release_steps = publish["jobs"]["release-build"]["steps"]
    metadata_step = next(
        step for step in release_steps if step.get("name") == "Verify release metadata"
    )
    assert metadata_step["env"] == {"RELEASE_TAG": "${{ github.event.release.tag_name }}"}

    release_commands = "\n".join(_job_run_commands(publish, "release-build"))
    assert "CHANGELOG.md" in release_commands
    assert "uv lock --check" in release_commands
    assert "uv build" in release_commands
    assert "dist/hermesd-{version}.tar.gz" in release_commands
    assert "dist/hermesd-{version}-py3-none-any.whl" in release_commands
    assert ".wheel-smoke/bin/hermesd --version" in release_commands
    assert ".wheel-smoke/bin/python -I -m hermesd --version" in release_commands
    assert "uv run python -m venv .sdist-smoke" in release_commands
    assert ".sdist-smoke/bin/python -m pip install dist/*.tar.gz" in release_commands
    assert ".sdist-smoke/bin/hermesd --version" in release_commands
    assert ".sdist-smoke/bin/python -I -m hermesd --version" in release_commands
    assert "uv run twine check dist/*" in release_commands
    upload_step = _action_step(publish, "release-build", UPLOAD_ARTIFACT_ACTION)
    assert upload_step["with"] == {
        "name": "release-dists",
        "path": "dist/*",
        "if-no-files-found": "error",
        "retention-days": 7,
    }
    publish_steps = publish["jobs"]["pypi-publish"]["steps"]
    downloaded_step = next(
        step
        for step in publish_steps
        if step.get("name") == "Verify downloaded release distributions"
    )
    assert downloaded_step["env"] == {"RELEASE_TAG": "${{ github.event.release.tag_name }}"}
    publish_commands = "\n".join(_job_run_commands(publish, "pypi-publish"))
    assert "RELEASE_TAG" in publish_commands
    assert 'expected_sdist="dist/hermesd-${version}.tar.gz"' in publish_commands
    assert 'expected_wheel="dist/hermesd-${version}-py3-none-any.whl"' in publish_commands
    _action_step(publish, "pypi-publish", DOWNLOAD_ARTIFACT_ACTION)
    _action_step(publish, "pypi-publish", PYPI_PUBLISH_ACTION)
    assert publish["jobs"]["pypi-publish"]["permissions"]["id-token"] == "write"
    assert publish["jobs"]["pypi-publish"]["environment"]["name"] == "pypi"


def test_dependabot_tracks_github_actions_versions() -> None:
    dependabot = _workflow(".github/dependabot.yml")

    assert dependabot["version"] == 2
    updates = dependabot["updates"]
    assert {
        "package-ecosystem": "github-actions",
        "directory": "/",
        "schedule": {"interval": "weekly"},
        "open-pull-requests-limit": 5,
    } in updates
    assert {
        "package-ecosystem": "docker",
        "directory": "/",
        "schedule": {"interval": "weekly"},
        "open-pull-requests-limit": 5,
    } in updates


def test_typed_marker_and_sdist_support_files_are_packaged() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert Path("hermesd/py.typed").is_file()

    includes = set(project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])
    assert {".github/", "flake.nix"} <= includes
