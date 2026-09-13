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


def test_docker_includes_and_smokes_git_checkpoint_support():
    dockerfile = Path("Dockerfile").read_text()
    assert re.search(r"apt-get install[^\n]*\bgit\b", dockerfile)
    commands = "\n".join(_job_run_commands(_workflow(".github/workflows/ci.yml"), "docker"))
    # The checkpoint smoke goes through the public CLI boundary with a
    # synthetic checkpoint repo - not a private collector helper (audit CI-22).
    assert "_git_checkpoint_summary" not in commands
    assert "hermesd.collect" not in commands
    assert '--snapshot-panel", "4"' in commands or '"--snapshot-panel", "4"' in commands
    assert '"Checkpoints (1)" in snapshot.stdout' in commands
    assert '"checkpoint 1" in snapshot.stdout' in commands


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


def _assert_all_actions_are_sha_pinned(workflow: dict) -> None:
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                # Third-party actions must be commit-SHA pinned. Local
                # composite actions (./.github/actions/*) are in-repo by
                # definition and have their own pinning test.
                assert re.fullmatch(
                    r"[^@]+@[0-9a-f]{40}|\./\.github/actions/[a-z0-9/-]+", step["uses"]
                )


def test_flake_version_matches_project_version() -> None:
    flake_text = Path("flake.nix").read_text()

    # Single source of truth: the flake derives its package version from
    # pyproject.toml instead of duplicating a literal that can drift.
    assert "importTOML ./pyproject.toml" in flake_text
    assert "version = hermesdVersion pkgs;" in flake_text

    # The flake must realize the package and smoke the installed CLI, so
    # `nix flake check` proves buildability rather than mere evaluation.
    assert "checks = forAllSystems" in flake_text
    assert "inherit hermesd;" in flake_text
    assert "hermesd-cli-smoke" in flake_text

    assert len(re.findall(r'github:[^/]+/[^/]+/[0-9a-f]{40}"', flake_text)) == 1


def test_readme_images_use_package_metadata_safe_urls() -> None:
    readme = Path("README.md").read_text()
    relative_image_links = re.findall(r"!\[[^\]]*]\((images/[^)]+)\)", readme)

    assert relative_image_links == []


def test_locked_env_composite_action_is_pinned() -> None:
    action = _workflow(".github/actions/locked-env/action.yml")

    steps = action["runs"]["steps"]
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
    setup_step = next(step for step in steps if step["uses"].startswith(f"{SETUP_UV_ACTION}@"))
    assert setup_step["with"]["version"] == UV_VERSION
    commands = "\n".join(step["run"] for step in steps if "run" in step)
    assert "uv lock --check" in commands
    assert "uv sync --locked --all-extras --dev" in commands


def _assert_ci_uses_locked_env(ci: dict, job_name: str, python_version: str) -> None:
    step = next(
        step
        for step in ci["jobs"][job_name]["steps"]
        if step.get("uses", "").startswith("./.github/actions/locked-env")
    )
    assert step["with"]["python-version"] == python_version


def test_dependency_review_gate_policy() -> None:
    review = _workflow(".github/workflows/dependency-review.yml")

    assert review["on"] == {"pull_request": None}
    # Read-only: posting PR comments would need pull-requests: write for no
    # control benefit (audit CI-13).
    assert review["permissions"] == {"contents": "read"}

    job = review["jobs"]["dependency-review"]
    step = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/dependency-review-action@")
    )
    assert re.fullmatch(r"actions/dependency-review-action@[0-9a-f]{40}", step["uses"])
    assert step["with"]["fail-on-severity"] == "moderate"
    assert step["with"]["retry-on-snapshot-warnings"] is True
    assert "comment-summary-in-pr" not in step["with"]


def test_scheduled_security_workflow_blocks_and_reports() -> None:
    security = _workflow(".github/workflows/security.yml")

    # Scheduled execution plus manual dispatch (audit CI-12).
    assert "schedule" in security["on"]
    assert security["on"]["schedule"] == [{"cron": "23 6 * * 1"}]
    assert "workflow_dispatch" in security["on"]
    assert security["permissions"] == {"contents": "read"}

    installed_commands = "\n".join(_job_run_commands(security, "installed-audit"))
    scan_commands = "\n".join(_job_run_commands(security, "lockfile-scan"))
    gate_commands = "\n".join(_job_run_commands(security, "gate"))

    # Installed-environment audit on every supported interpreter.
    assert security["jobs"]["installed-audit"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.12",
        "3.13",
        "3.14",
    ]
    assert "uv run python scripts/pip_audit_gate.py" in installed_commands

    # Direct lockfile scan complements the installed audit.
    assert "uv export" in scan_commands
    assert "--no-deps --disable-pip" in scan_commands

    # Aggregate gate: any non-success outcome fails the security check.
    assert security["jobs"]["gate"]["if"] == "always()"
    assert set(security["jobs"]["gate"]["needs"]) == {"installed-audit", "lockfile-scan"}
    assert '!= "success"' in gate_commands


def test_ci_change_classification_and_gate() -> None:
    ci = _workflow(".github/workflows/ci.yml")

    # Classification: Docker/Nix rules must include application source and
    # package metadata, so relevant changes cannot silently skip them.
    changes_steps = ci["jobs"]["changes"]["steps"]
    filter_step = next(
        step for step in changes_steps if step.get("uses", "").startswith("dorny/paths-filter@")
    )
    assert re.fullmatch(r"dorny/paths-filter@[0-9a-f]{40}", filter_step["uses"])
    filters = yaml.safe_load(filter_step["with"]["filters"])
    packaged_inputs = {
        ".github/workflows/ci.yml",
        "LICENSE",
        "README.md",
        "hermesd/**",
        "pyproject.toml",
        "uv.lock",
    }
    assert packaged_inputs | {"Dockerfile", ".dockerignore"} == set(filters["docker"])
    assert packaged_inputs | {"flake.nix", "flake.lock"} == set(filters["nix"])

    # Only pull requests may skip Docker/Nix, and only on an empty diff;
    # trusted events always run the full validation.
    for job_name in ("docker", "nix"):
        job = ci["jobs"][job_name]
        assert job["needs"] == "changes"
        assert f"needs.changes.outputs.{job_name} == 'true'" in job["if"]
        assert "github.event_name != 'pull_request'" in job["if"]

    # The aggregate gate always reports and treats every non-success outcome
    # of expected work (including cancelled or misclassified skips) as a
    # failed required check.
    gate = ci["jobs"]["gate"]
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {
        "changes",
        "static",
        "security",
        "test",
        "macos",
        "package",
        "docker",
        "nix",
    }
    gate_step = gate["steps"][0]
    results_env = gate_step["env"]["RESULTS"]
    for job_name in gate["needs"]:
        assert f"{job_name}=${{{{ needs.{job_name}.result }}}}" in results_env
    assert gate_step["env"]["DOCKER_EXPECTED"] == (
        "${{ needs.changes.outputs.docker == 'true' || github.event_name != 'pull_request' }}"
    )
    assert gate_step["env"]["NIX_EXPECTED"] == (
        "${{ needs.changes.outputs.nix == 'true' || github.event_name != 'pull_request' }}"
    )
    gate_commands = "\n".join(_job_run_commands(ci, "gate"))
    assert 'result" ] != "success"' in gate_commands or '!= "success"' in gate_commands


def test_ci_splits_static_security_and_interpreter_gates() -> None:
    ci = _workflow(".github/workflows/ci.yml")

    # Separated, independently visible gates (audit CI-11).
    for job_name in ("static", "security", "test", "macos", "package", "docker", "nix"):
        assert job_name in ci["jobs"]

    static_commands = set(_job_run_commands(ci, "static"))
    assert {
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy hermesd",
        "uv run python -m compileall hermesd",
    } <= static_commands

    expected_matrix = ["3.11", "3.12", "3.13", "3.14"]
    assert ci["jobs"]["security"]["strategy"]["matrix"]["python-version"] == expected_matrix
    assert "uv run python scripts/pip_audit_gate.py" in _job_run_commands(ci, "security")

    assert ci["jobs"]["test"]["strategy"]["matrix"]["python-version"] == expected_matrix
    test_commands = set(_job_run_commands(ci, "test"))
    assert (
        "uv run pytest tests/ -q -ra --tb=short -W error::ResourceWarning --cov=hermesd "
        '--cov-report=term-missing --junitxml="$RUNNER_TEMP/pytest-results.xml"' in test_commands
    )

    # Failure diagnostics: JUnit and coverage data are uploaded whether the
    # job succeeded or not (audit CI-20).
    diagnostics_steps = [
        step
        for step in ci["jobs"]["test"]["steps"]
        if step.get("name") == "Upload test diagnostics"
    ]
    assert len(diagnostics_steps) == 1
    assert diagnostics_steps[0]["if"] == "always()"
    assert diagnostics_steps[0]["uses"].startswith(f"{UPLOAD_ARTIFACT_ACTION}@")
    assert diagnostics_steps[0]["with"]["if-no-files-found"] == "ignore"

    for job_name, python_version in (
        ("static", "3.11"),
        ("security", "${{ matrix.python-version }}"),
        ("test", "${{ matrix.python-version }}"),
        ("macos", "3.14"),
        ("package", "3.11"),
    ):
        _assert_ci_uses_locked_env(ci, job_name, python_version)


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
    assert publish["jobs"]["test"]["strategy"]["matrix"]["python-version"] == expected_matrix
    assert ci["jobs"]["test"]["runs-on"] == "ubuntu-24.04"
    assert ci["jobs"]["macos"]["runs-on"] == "macos-15"
    assert publish["jobs"]["test"]["runs-on"] == "ubuntu-24.04"
    assert publish["jobs"]["release-build"]["runs-on"] == "ubuntu-24.04"
    assert publish["jobs"]["pypi-publish"]["runs-on"] == "ubuntu-24.04"
    assert publish["jobs"]["release-build"]["if"] == "${{ !github.event.release.prerelease }}"
    assert publish["jobs"]["pypi-publish"]["if"] == "${{ !github.event.release.prerelease }}"

    # Release eligibility is mechanically enforced (audit CI-04): the exact
    # release commit must carry a successful, completed aggregate CI gate
    # run; missing, failed, cancelled, stale, or in-progress work refuses to
    # publish.
    assert ci["jobs"]["gate"]["name"] == "CI gate"
    eligibility = publish["jobs"]["release-eligibility"]
    assert eligibility["permissions"] == {"contents": "read", "checks": "read"}
    verify_step = next(
        step
        for step in eligibility["steps"]
        if step.get("name", "").startswith("Verify the release commit")
    )
    assert verify_step["env"]["REQUIRED_CHECK"] == "CI gate"
    verify_commands = verify_step["run"]
    assert "commits/${RELEASE_SHA}/check-runs" in verify_commands
    assert verify_commands.count("refusing to publish") == 3
    assert publish["jobs"]["release-build"]["needs"] == ["test", "release-eligibility"]
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
    for job_name in ("test", "package", "macos"):
        _action_step(ci, job_name, CHECKOUT_ACTION)
    _action_step(publish, "test", CHECKOUT_ACTION)
    _action_step(publish, "release-build", CHECKOUT_ACTION)

    # The release test matrix explicitly opts into uv caching; the release
    # build keeps caching disabled (audit CI-16 trust boundary).
    publish_locked_env = {
        step["with"]["python-version"]: step["with"]["enable-cache"]
        for job in ("test", "release-build")
        for step in publish["jobs"][job]["steps"]
        if step.get("uses", "").startswith("./.github/actions/locked-env")
    }
    assert publish_locked_env == {
        "${{ matrix.python-version }}": "true",
        "3.11": "false",
    }

    # The publication workflow re-runs the full gate on the release commit.
    required_release_gate_commands = {
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy hermesd",
        "uv run python -m compileall hermesd",
        'uv run pytest tests/ -q -ra --tb=short -W error::ResourceWarning --cov=hermesd --cov-report=term-missing --junitxml="$RUNNER_TEMP/pytest-results.xml"',
        "uv run python scripts/pip_audit_gate.py",
    }
    assert required_release_gate_commands <= set(_job_run_commands(publish, "test"))

    ci_package_commands = "\n".join(_job_run_commands(ci, "package"))
    assert "uv build" in ci_package_commands
    # Smoke venvs must be created outside the checkout ($RUNNER_TEMP).
    assert 'uv run python -m venv "$RUNNER_TEMP/wheel-smoke"' in ci_package_commands
    assert '"$RUNNER_TEMP/wheel-smoke/bin/hermesd" --version' in ci_package_commands
    assert '"$RUNNER_TEMP/wheel-smoke/bin/python" -I -m hermesd --version' in ci_package_commands
    assert 'uv run python -m venv "$RUNNER_TEMP/sdist-smoke"' in ci_package_commands
    assert '"$RUNNER_TEMP/sdist-smoke/bin/hermesd" --version' in ci_package_commands
    assert '"$RUNNER_TEMP/sdist-smoke/bin/python" -I -m hermesd --version' in ci_package_commands
    # Installed-artifact behavior smoke must run for wheel and sdist (audit
    # CI-19): snapshots, missing data, read-only, distribution metadata.
    assert ci_package_commands.count("scripts/installed_smoke.py") == 2
    # No checkout-relative smoke paths may remain.
    assert ".wheel-smoke" not in ci_package_commands.replace("$RUNNER_TEMP/wheel-smoke", "")
    assert ".sdist-smoke" not in ci_package_commands.replace("$RUNNER_TEMP/sdist-smoke", "")
    assert "uv run twine check dist/*" in ci_package_commands
    macos_commands = set(_job_run_commands(ci, "macos"))
    assert (
        'uv run pytest tests/ -q -ra --tb=short -W error::ResourceWarning --cov=hermesd --cov-report=term-missing --junitxml="$RUNNER_TEMP/pytest-results.xml"'
        in macos_commands
    )
    assert "docker run --rm hermesd-ci --version" in "\n".join(_job_run_commands(ci, "docker"))
    assert "nix flake check --no-write-lock-file" in "\n".join(_job_run_commands(ci, "nix"))

    release_steps = publish["jobs"]["release-build"]["steps"]
    metadata_step = next(
        step for step in release_steps if step.get("name") == "Verify release metadata"
    )
    assert metadata_step["env"] == {"RELEASE_TAG": "${{ github.event.release.tag_name }}"}

    release_commands = "\n".join(_job_run_commands(publish, "release-build"))
    assert "CHANGELOG.md" in release_commands
    # uv lock --check and uv sync run inside the composite locked-env action
    # (asserted by test_locked_env_composite_action_is_pinned).
    assert "uv build" in release_commands
    assert "dist/hermesd-{version}.tar.gz" in release_commands
    assert "dist/hermesd-{version}-py3-none-any.whl" in release_commands
    assert 'uv run python -m venv "$RUNNER_TEMP/wheel-smoke"' in release_commands
    assert '"$RUNNER_TEMP/wheel-smoke/bin/hermesd" --version' in release_commands
    assert '"$RUNNER_TEMP/wheel-smoke/bin/python" -I -m hermesd --version' in release_commands
    assert 'uv run python -m venv "$RUNNER_TEMP/sdist-smoke"' in release_commands
    assert '"$RUNNER_TEMP/sdist-smoke/bin/python" -m pip install dist/*.tar.gz' in release_commands
    assert '"$RUNNER_TEMP/sdist-smoke/bin/hermesd" --version' in release_commands
    assert '"$RUNNER_TEMP/sdist-smoke/bin/python" -I -m hermesd --version' in release_commands
    assert release_commands.count("scripts/installed_smoke.py") == 2
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
    by_ecosystem = {update["package-ecosystem"]: update for update in updates}
    for ecosystem in ("github-actions", "uv", "docker"):
        update = by_ecosystem[ecosystem]
        assert update["directory"] == "/"
        assert update["schedule"] == {"interval": "weekly"}
        assert update["open-pull-requests-limit"] == 5
    # Routine minor/patch action updates are grouped; majors stay separate.
    groups = by_ecosystem["github-actions"]["groups"]
    assert groups["actions-minor-and-patch"]["update-types"] == [
        "version-update:semver-minor",
        "version-update:semver-patch",
    ]


def test_typed_marker_and_sdist_support_files_are_packaged() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert Path("hermesd/py.typed").is_file()

    includes = set(project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])
    assert {".codex/rules/source-ownership.md", ".github/", "flake.nix"} <= includes
