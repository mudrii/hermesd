from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

import pytest

from scripts.release_eligibility import EligibilityError, verify_release_eligibility

REPOSITORY = "mudrii/hermesd"
RELEASE_SHA = "a" * 40
MAIN_SHA = "b" * 40


def _responses() -> dict[str, object]:
    run = {
        "id": 42,
        "workflow_id": 7,
        "run_number": 12,
        "run_attempt": 2,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": RELEASE_SHA,
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
    }
    return {
        "branch": {
            "name": "main",
            "protected": True,
            "commit": {"sha": MAIN_SHA},
        },
        "comparison": {
            "status": "ahead",
            "behind_by": 0,
            "base_commit": {"sha": RELEASE_SHA},
            "merge_base_commit": {"sha": RELEASE_SHA},
        },
        "workflow": {
            "id": 7,
            "path": ".github/workflows/ci.yml",
            "state": "active",
        },
        "runs": {"total_count": 1, "workflow_runs": [run]},
        "run": run,
        "jobs": {
            "total_count": 1,
            "jobs": [
                {
                    "name": "CI gate",
                    "run_id": 42,
                    "head_sha": RELEASE_SHA,
                    "status": "completed",
                    "conclusion": "success",
                    "workflow_name": "CI",
                }
            ],
        },
    }


def _api(
    responses: dict[str, object], requests: list[str] | None = None
) -> Callable[[str, dict[str, str] | None], object]:
    def get(path: str, params: dict[str, str] | None = None) -> object:
        if requests is not None:
            requests.append(path)
        if path.endswith("/branches/main"):
            return responses["branch"]
        if "/compare/" in path:
            return responses["comparison"]
        if path.endswith("/actions/workflows/ci.yml"):
            return responses["workflow"]
        if path.endswith("/actions/workflows/7/runs"):
            assert params == {
                "branch": "main",
                "event": "push",
                "head_sha": RELEASE_SHA,
                "per_page": "100",
            }
            return responses["runs"]
        if path.endswith("/actions/runs/42"):
            return responses["run"]
        if path.endswith("/actions/runs/42/attempts/2/jobs"):
            assert params == {"per_page": "100"}
            return responses["jobs"]
        if path.endswith("/actions/runs/42/attempts/3/jobs"):
            raise EligibilityError("latest workflow attempt job evidence is missing")
        raise AssertionError(f"unexpected API request: {path} {params}")

    return get


def test_release_requires_protected_main_ancestry_and_latest_successful_ci_attempt() -> None:
    evidence = verify_release_eligibility(_api(_responses()), REPOSITORY, RELEASE_SHA)

    assert evidence.main_sha == MAIN_SHA
    assert evidence.workflow_run_id == 42
    assert evidence.workflow_run_attempt == 2


@pytest.mark.parametrize(
    ("key", "replacement", "message"),
    [
        ("branch", {"name": "main", "protected": False, "commit": {"sha": MAIN_SHA}}, "protected"),
        (
            "comparison",
            {
                "status": "diverged",
                "behind_by": 1,
                "base_commit": {"sha": RELEASE_SHA},
                "merge_base_commit": {"sha": "c" * 40},
            },
            "protected main history",
        ),
        (
            "workflow",
            {"id": 7, "path": ".github/workflows/ci.yml", "state": "disabled_manually"},
            "active",
        ),
        ("runs", {"total_count": 0, "workflow_runs": []}, "no CI workflow run"),
    ],
)
def test_release_refuses_missing_or_unapproved_history_and_workflow_evidence(
    key: str,
    replacement: object,
    message: str,
) -> None:
    responses = _responses()
    responses[key] = replacement

    with pytest.raises(EligibilityError, match=message):
        verify_release_eligibility(_api(responses), REPOSITORY, RELEASE_SHA)


def test_release_refuses_a_failed_newer_run_even_when_an_older_run_succeeded() -> None:
    responses = _responses()
    old_success = deepcopy(responses["run"])
    assert isinstance(old_success, dict)
    old_success["id"] = 41
    old_success["run_number"] = 11
    responses["runs"] = {
        "total_count": 2,
        "workflow_runs": [old_success, responses["run"]],
    }
    current = deepcopy(responses["run"])
    assert isinstance(current, dict)
    current["conclusion"] = "failure"
    responses["run"] = current

    with pytest.raises(EligibilityError, match=r"latest CI workflow run.*failure"):
        verify_release_eligibility(_api(responses), REPOSITORY, RELEASE_SHA)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "workflow_dispatch"),
        ("head_branch", "release"),
        ("head_sha", "c" * 40),
        ("path", ".github/workflows/other.yml"),
        ("repository", {"full_name": "attacker/fork"}),
        ("head_repository", {"full_name": "attacker/fork"}),
    ],
)
def test_release_refuses_a_run_without_exact_trusted_ci_provenance(
    field: str, value: object
) -> None:
    responses = _responses()
    run = deepcopy(responses["run"])
    assert isinstance(run, dict)
    run[field] = value
    responses["run"] = run

    with pytest.raises(EligibilityError, match="trusted CI workflow run"):
        verify_release_eligibility(_api(responses), REPOSITORY, RELEASE_SHA)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", "in_progress", "still in_progress"),
        ("conclusion", "cancelled", "concluded cancelled"),
        ("run_id", 999, "trusted workflow attempt"),
        ("head_sha", "c" * 40, "trusted workflow attempt"),
        ("workflow_name", "Other", "trusted workflow attempt"),
    ],
)
def test_release_refuses_missing_unsuccessful_or_untrusted_gate_job(
    field: str, value: object, message: str
) -> None:
    responses = _responses()
    jobs = responses["jobs"]
    assert isinstance(jobs, dict)
    gate = deepcopy(jobs["jobs"][0])
    assert isinstance(gate, dict)
    gate[field] = value
    jobs["jobs"] = [gate]

    with pytest.raises(EligibilityError, match=message):
        verify_release_eligibility(_api(responses), REPOSITORY, RELEASE_SHA)


@pytest.mark.parametrize("gate_count", [0, 2])
def test_release_requires_exactly_one_gate_in_the_latest_attempt(gate_count: int) -> None:
    responses = _responses()
    jobs = responses["jobs"]
    assert isinstance(jobs, dict)
    gate = deepcopy(jobs["jobs"][0])
    jobs["jobs"] = [deepcopy(gate) for _ in range(gate_count)]
    jobs["total_count"] = gate_count

    with pytest.raises(EligibilityError, match=f"has {gate_count}"):
        verify_release_eligibility(_api(responses), REPOSITORY, RELEASE_SHA)


def test_release_refuses_stale_job_data_from_an_earlier_attempt() -> None:
    responses = _responses()
    current = deepcopy(responses["run"])
    assert isinstance(current, dict)
    current["run_attempt"] = 3
    responses["run"] = current
    requests: list[str] = []

    with pytest.raises(EligibilityError, match=r"latest workflow attempt.*missing"):
        verify_release_eligibility(_api(responses, requests), REPOSITORY, RELEASE_SHA)

    assert requests[-1].endswith("/actions/runs/42/attempts/3/jobs")
