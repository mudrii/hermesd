from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

MAIN_BRANCH = "main"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_WORKFLOW_NAME = "CI"
CI_GATE_NAME = "CI gate"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)

ApiGet = Callable[[str, dict[str, str] | None], object]


class EligibilityError(ValueError):
    """A release cannot be tied to trusted, current validation evidence."""


@dataclass(frozen=True, slots=True)
class EligibilityEvidence:
    """Trusted evidence that makes one release commit publishable."""

    main_sha: str
    workflow_run_id: int
    workflow_run_attempt: int


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise EligibilityError(f"GitHub returned an invalid {label} object")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise EligibilityError(f"GitHub returned an invalid {label} list")
    return list(value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EligibilityError(f"GitHub returned an invalid {label}")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EligibilityError(f"GitHub returned an invalid {label}")
    return value


def _sha(value: object, label: str) -> str:
    sha = _string(value, label)
    if _SHA_PATTERN.fullmatch(sha) is None:
        raise EligibilityError(f"GitHub returned an invalid {label}")
    return sha.lower()


def _nested_string(value: object, key: str, label: str) -> str:
    return _string(_object(value, label).get(key), f"{label}.{key}")


def _validate_repository(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts) or any(part in {".", ".."} for part in parts):
        raise EligibilityError(f"invalid GitHub repository {repository!r}")
    return parts[0], parts[1]


def _validate_main_history(
    branch_value: object,
    comparison_value: object,
    release_sha: str,
) -> str:
    branch = _object(branch_value, "main branch")
    if branch.get("name") != MAIN_BRANCH:
        raise EligibilityError("GitHub did not return the configured main branch")
    if branch.get("protected") is not True:
        raise EligibilityError("main is not protected; refusing to publish")

    main_sha = _sha(_object(branch.get("commit"), "main commit").get("sha"), "main SHA")
    comparison = _object(comparison_value, "main-history comparison")
    status = comparison.get("status")
    behind_by = comparison.get("behind_by")
    base_sha = _sha(
        _object(comparison.get("base_commit"), "comparison base commit").get("sha"),
        "comparison base SHA",
    )
    merge_base_sha = _sha(
        _object(comparison.get("merge_base_commit"), "comparison merge base").get("sha"),
        "comparison merge-base SHA",
    )
    if (
        status not in {"ahead", "identical"}
        or behind_by != 0
        or base_sha != release_sha
        or merge_base_sha != release_sha
    ):
        raise EligibilityError(
            f"release commit {release_sha} is not part of protected main history"
        )
    return main_sha


def _validate_workflow(value: object) -> int:
    workflow = _object(value, "CI workflow")
    workflow_id = _integer(workflow.get("id"), "CI workflow id")
    if workflow.get("path") != CI_WORKFLOW_PATH:
        raise EligibilityError("GitHub did not resolve the required CI workflow path")
    if workflow.get("state") != "active":
        raise EligibilityError("the required CI workflow is not active")
    return workflow_id


def _latest_run_id(value: object) -> int:
    response = _object(value, "CI workflow-runs response")
    runs = _array(response.get("workflow_runs"), "CI workflow-runs")
    total_count = _integer(response.get("total_count"), "CI workflow-runs total_count")
    if total_count != len(runs):
        raise EligibilityError("CI workflow-run evidence is incomplete")
    if not runs:
        raise EligibilityError("no CI workflow run exists for the release commit")
    return max(
        _integer(_object(run, "CI workflow run").get("id"), "workflow run id") for run in runs
    )


def _validate_run(
    value: object,
    *,
    expected_id: int,
    workflow_id: int,
    repository: str,
    release_sha: str,
) -> int:
    run = _object(value, "latest CI workflow run")
    trusted = (
        run.get("id") == expected_id
        and run.get("workflow_id") == workflow_id
        and run.get("path") == CI_WORKFLOW_PATH
        and run.get("event") == "push"
        and run.get("head_branch") == MAIN_BRANCH
        and run.get("head_sha") == release_sha
        and _nested_string(run.get("repository"), "full_name", "run repository") == repository
        and _nested_string(run.get("head_repository"), "full_name", "run head repository")
        == repository
    )
    if not trusted:
        raise EligibilityError("latest result is not a trusted CI workflow run for the release SHA")

    attempt = _integer(run.get("run_attempt"), "workflow run attempt")
    if attempt < 1:
        raise EligibilityError("GitHub returned an invalid workflow run attempt")
    if run.get("status") != "completed":
        raise EligibilityError(f"latest CI workflow run is still {run.get('status')}")
    if run.get("conclusion") != "success":
        raise EligibilityError(f"latest CI workflow run concluded {run.get('conclusion')}")
    return attempt


def _validate_gate_job(
    value: object,
    *,
    run_id: int,
    repository_sha: str,
) -> None:
    response = _object(value, "workflow jobs response")
    jobs = _array(response.get("jobs"), "workflow jobs")
    total_count = _integer(response.get("total_count"), "workflow jobs total_count")
    if total_count != len(jobs):
        raise EligibilityError("latest workflow-attempt job evidence is incomplete")

    gates = [
        job
        for job in (_object(entry, "workflow job") for entry in jobs)
        if job.get("name") == CI_GATE_NAME
    ]
    if len(gates) != 1:
        raise EligibilityError(
            f"latest trusted workflow attempt has {len(gates)} {CI_GATE_NAME!r} jobs"
        )
    gate = gates[0]
    if (
        gate.get("run_id") != run_id
        or gate.get("head_sha") != repository_sha
        or gate.get("workflow_name") != CI_WORKFLOW_NAME
    ):
        raise EligibilityError("CI gate does not belong to the trusted workflow attempt")
    if gate.get("status") != "completed":
        raise EligibilityError(f"CI gate is still {gate.get('status')}")
    if gate.get("conclusion") != "success":
        raise EligibilityError(f"CI gate concluded {gate.get('conclusion')}")


def verify_release_eligibility(
    api_get: ApiGet,
    repository: str,
    release_sha: str,
) -> EligibilityEvidence:
    """Verify protected-main ancestry and the latest trusted CI attempt."""
    owner, name = _validate_repository(repository)
    release_sha = _sha(release_sha, "release SHA")
    repo_path = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"

    branch = api_get(f"{repo_path}/branches/{MAIN_BRANCH}", None)
    branch_object = _object(branch, "main branch")
    main_sha = _sha(_object(branch_object.get("commit"), "main commit").get("sha"), "main SHA")
    comparison = api_get(f"{repo_path}/compare/{release_sha}...{main_sha}", None)
    main_sha = _validate_main_history(branch, comparison, release_sha)

    workflow = api_get(f"{repo_path}/actions/workflows/ci.yml", None)
    workflow_id = _validate_workflow(workflow)
    runs = api_get(
        f"{repo_path}/actions/workflows/{workflow_id}/runs",
        {
            "branch": MAIN_BRANCH,
            "event": "push",
            "head_sha": release_sha,
            "per_page": "100",
        },
    )
    run_id = _latest_run_id(runs)

    current_run = api_get(f"{repo_path}/actions/runs/{run_id}", None)
    attempt = _validate_run(
        current_run,
        expected_id=run_id,
        workflow_id=workflow_id,
        repository=repository,
        release_sha=release_sha,
    )
    jobs = api_get(
        f"{repo_path}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        {"per_page": "100"},
    )
    _validate_gate_job(jobs, run_id=run_id, repository_sha=release_sha)
    return EligibilityEvidence(main_sha, run_id, attempt)


class GitHubApi:
    """Small authenticated reader for the GitHub REST API."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    def get(self, path: str, params: dict[str, str] | None = None) -> object:
        """Return one decoded GitHub REST response."""
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(
            f"{self._base_url}{path}{query}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "hermesd-release-eligibility",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise EligibilityError(f"GitHub API returned HTTP {exc.code} for {path}") from exc
        except urllib.error.URLError as exc:
            raise EligibilityError(f"GitHub API request failed for {path}: {exc.reason}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EligibilityError(f"GitHub API returned invalid JSON for {path}") from exc


def main() -> int:
    """Verify the release described by the GitHub Actions environment."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    release_sha = os.environ.get("GITHUB_SHA", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not token:
        raise EligibilityError("GITHUB_TOKEN is required")

    evidence = verify_release_eligibility(
        GitHubApi(api_url, token).get,
        repository,
        release_sha,
    )
    print(
        f"release commit {release_sha} is in protected main history and passed "
        f"trusted CI run {evidence.workflow_run_id}, attempt {evidence.workflow_run_attempt}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EligibilityError as error:
        raise SystemExit(f"release is ineligible: {error}") from error
