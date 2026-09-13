from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from packaging.version import Version


def _workflow(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    assert isinstance(data, dict)
    if True in data:
        data["on"] = data.pop(True)
    return data


def test_change_classification_has_permissions_and_covers_validation_inputs() -> None:
    ci = _workflow(".github/workflows/ci.yml")
    changes = ci["jobs"]["changes"]
    assert changes["permissions"] == {"contents": "read", "pull-requests": "read"}

    filter_step = next(
        step for step in changes["steps"] if step.get("uses", "").startswith("dorny/paths-filter@")
    )
    filters = yaml.safe_load(filter_step["with"]["filters"])
    shared_inputs = {
        ".codex/rules/**",
        ".github/**",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "docs/**",
        "hermesd/**",
        "pyproject.toml",
        "scripts/**",
        "tests/**",
        "uv.lock",
    }
    assert shared_inputs | {".dockerignore", "Dockerfile"} <= set(filters["docker"])
    assert shared_inputs | {"flake.lock", "flake.nix"} <= set(filters["nix"])

    representative_files = (
        ".codex/rules/source-ownership.md",
        ".github/workflows/security.yml",
        "CHANGELOG.md",
        "docs/ci-release-policy.md",
        "scripts/installed_smoke.py",
        "tests/test_package_metadata.py",
    )
    for patterns in filters.values():
        for path in representative_files:
            assert any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns), path


def test_dependency_review_is_part_of_the_required_ci_gate() -> None:
    ci = _workflow(".github/workflows/ci.yml")
    review = ci["jobs"]["dependency-review"]
    assert review["if"] == "github.event_name == 'pull_request'"
    assert review["permissions"] == {"contents": "read"}

    step = next(
        step
        for step in review["steps"]
        if step.get("uses", "").startswith("actions/dependency-review-action@")
    )
    assert re.fullmatch(r"actions/dependency-review-action@[0-9a-f]{40}", step["uses"])
    assert step["with"] == {
        "fail-on-severity": "moderate",
        "fail-on-scopes": "runtime, development, unknown",
        "retry-on-snapshot-warnings": True,
    }

    gate = ci["jobs"]["gate"]
    assert "dependency-review" in gate["needs"]
    gate_step = gate["steps"][0]
    assert "dependency-review=${{ needs.dependency-review.result }}" in gate_step["env"]["RESULTS"]
    assert gate_step["env"]["DEPENDENCY_REVIEW_EXPECTED"] == (
        "${{ github.event_name == 'pull_request' }}"
    )

    # The dependency check belongs to this aggregate workflow. Keeping a
    # second PR workflow would duplicate API work without strengthening the
    # required CI gate.
    assert not Path(".github/workflows/dependency-review.yml").exists()


def test_static_analysis_types_ci_helper_scripts() -> None:
    ci = _workflow(".github/workflows/ci.yml")
    commands = {step["run"] for step in ci["jobs"]["static"]["steps"] if "run" in step}
    assert "uv run mypy hermesd scripts" in commands


def _run_gate(
    results: str,
    *,
    docker_expected: bool,
    nix_expected: bool,
    dependency_review_expected: bool,
) -> subprocess.CompletedProcess[str]:
    ci = _workflow(".github/workflows/ci.yml")
    command = ci["jobs"]["gate"]["steps"][0]["run"]
    env = os.environ | {
        "RESULTS": results,
        "DOCKER_EXPECTED": str(docker_expected).lower(),
        "NIX_EXPECTED": str(nix_expected).lower(),
        "DEPENDENCY_REVIEW_EXPECTED": str(dependency_review_expected).lower(),
    }
    return subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


@pytest.mark.parametrize(
    ("results", "docker_expected", "nix_expected", "review_expected"),
    [
        (
            "changes=success dependency-review=success static=success security=success "
            "test=success macos=success package=success docker=success nix=success",
            True,
            True,
            True,
        ),
        (
            "changes=success dependency-review=success static=success security=success "
            "test=success macos=success package=success docker=skipped nix=skipped",
            False,
            False,
            True,
        ),
        (
            "changes=success dependency-review=skipped static=success security=success "
            "test=success macos=success package=success docker=success nix=success",
            True,
            True,
            False,
        ),
    ],
)
def test_ci_gate_accepts_only_the_expected_success_or_skip_states(
    results: str,
    docker_expected: bool,
    nix_expected: bool,
    review_expected: bool,
) -> None:
    completed = _run_gate(
        results,
        docker_expected=docker_expected,
        nix_expected=nix_expected,
        dependency_review_expected=review_expected,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("results", "docker_expected", "nix_expected", "review_expected"),
    [
        (
            "changes=failure dependency-review=success static=success security=success "
            "test=success macos=success package=success docker=skipped nix=skipped",
            False,
            False,
            True,
        ),
        (
            "changes=success dependency-review=cancelled static=success security=success "
            "test=success macos=success package=success docker=skipped nix=skipped",
            False,
            False,
            True,
        ),
        (
            "changes=success dependency-review= static=success security=success test=success "
            "macos=success package=success docker=skipped nix=skipped",
            False,
            False,
            True,
        ),
        (
            "changes=success dependency-review=skipped static=success security=success "
            "test=success macos=success package=success docker=success nix=skipped",
            False,
            False,
            False,
        ),
        (
            "changes=success dependency-review=success static=success security=success "
            "test=success macos=success package=success docker=skipped nix=skipped",
            False,
            False,
            False,
        ),
    ],
)
def test_ci_gate_rejects_failed_missing_cancelled_and_unexpected_outcomes(
    results: str,
    docker_expected: bool,
    nix_expected: bool,
    review_expected: bool,
) -> None:
    completed = _run_gate(
        results,
        docker_expected=docker_expected,
        nix_expected=nix_expected,
        dependency_review_expected=review_expected,
    )
    assert completed.returncode != 0
    assert "::error::" in completed.stdout


def test_dependabot_group_uses_group_update_type_values() -> None:
    dependabot = _workflow(".github/dependabot.yml")
    actions = next(
        update
        for update in dependabot["updates"]
        if update["package-ecosystem"] == "github-actions"
    )
    assert actions["groups"]["actions-minor-and-patch"]["update-types"] == [
        "minor",
        "patch",
    ]


def test_nix_build_uses_supported_build_tooling_and_runtime_git() -> None:
    flake = Path("flake.nix").read_text()

    hatchling = re.search(r'mkHatchling.*?version = "([^"]+)";', flake, re.DOTALL)
    assert hatchling is not None
    assert Version(hatchling.group(1)) >= Version("1.32")
    assert "build-system = [ (mkHatchling pkgs) ];" in flake
    assert "dontCheckRuntimeDeps = true;" in flake
    assert "python.pkgs.pytestCheckHook" in flake
    assert "python.pkgs.packaging" in flake
    assert 'pytestFlags = [ "tests" ];' in flake
    assert "pkgs.git" in flake
    assert "makeWrapperArgs" in flake
    assert "pkgs.lib.makeBinPath [ pkgs.git ]" in flake
    assert "hermesd-checkpoint-smoke" in flake
    assert "--snapshot-panel 4 --no-color" in flake


def test_nix_ci_exercises_every_advertised_flake_system() -> None:
    ci = _workflow(".github/workflows/ci.yml")
    assert ci["jobs"]["nix"]["strategy"]["matrix"]["include"] == [
        {"runner": "ubuntu-24.04", "system": "x86_64-linux"},
        {"runner": "ubuntu-24.04-arm", "system": "aarch64-linux"},
        {"runner": "macos-15-intel", "system": "x86_64-darwin"},
        {"runner": "macos-15", "system": "aarch64-darwin"},
    ]
    assert ci["jobs"]["nix"]["runs-on"] == "${{ matrix.runner }}"


def test_policy_documents_verified_repository_controls() -> None:
    policy = " ".join(Path("docs/ci-release-policy.md").read_text().split())

    assert "classic branch protection rule" in policy
    assert "bound to the GitHub Actions app" in policy
    assert "Administrator enforcement is enabled" in policy
    assert "direct runtime requirements" in policy
    assert "transitive dependencies" in policy
    assert "dependency-review" in policy and "CI gate" in policy
    assert "accepts only `v*` tag refs" in policy
    assert "pattern `v*`, type `tag`" in policy
    assert "no required reviewers" in policy
    assert "administrator bypass is disabled" in policy
    assert "Release tag creation" in policy
    assert "Immutable release tags" in policy
    assert "`update`, `deletion`, and `non_fast_forward` rules" in policy
    assert "do not prove protected-`main` ancestry" in policy
    assert "`release-eligibility` job enforces that separately" in policy
