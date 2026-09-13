# CI/CD implementation review — 13 September 2026

## Scope and baseline

Reviewed the supplied CI-01–CI-24 audit against the current checkout, live GitHub
controls, workflow execution, packaging, helper code, and tests. Confirmed fixes
were assigned to `gpt-5.6-sol` with high reasoning; the parent independently
reviewed the changes, ran verification, and returned additional findings for
correction.

- Checkout at review start: `fix/audit-review-followups`, `d46c9f1`, clean.
- Concurrent work merged cron and kanban corrections while this review ran,
  advancing local HEAD to `f16ef62`. Those changes were preserved. Final Python
  verification uses separate immutable snapshots of that tree plus this
  review's uncommitted changes.
- Refreshed remote main: `db350e4383beb86391cb0704c5dc1d5e1965c42a`.
- [Latest main CI](https://github.com/mudrii/hermesd/actions/runs/34752491586)
  succeeded, including both existing Nix platforms and all dependency audits.
- [Fresh Security workflow](https://github.com/mudrii/hermesd/actions/runs/34756696976)
  was manually dispatched during this review and passed all four installed
  interpreter audits, the lockfile scan, and its aggregate gate.
- Latest published release remains
  [v2026.7.11](https://github.com/mudrii/hermesd/releases/tag/v2026.7.11).
  Checkout version `2026.9.8` is not evidence of a published release.
- Baseline local suite: **2,844 passed, one skipped, 98.37% coverage**, Python 3.11.
  The skip is the opt-in contract test against the live Hermes home.

The original report's red-main baseline is obsolete. The current branch did,
however, remove Nix corrections already present on main. A green main run did
not validate this branch. The changes described below are local; no commit,
push, PR merge, release, or protection-setting mutation was performed.

## Confirmed findings and corrections

### P1 — Release admission trusted a check name without trusted provenance

The publication workflow accepted the newest check named `CI gate` without
requiring protected-main ancestry or binding it to the intended CI workflow,
main-push event, repository, and current run attempt. Checkouts also followed
the event ref rather than explicitly fixing the source SHA.

[`release_eligibility.py`](../scripts/release_eligibility.py) now checks protected
main ancestry, resolves the active `ci.yml` workflow, selects the newest exact-SHA
main-push run, refreshes its attempt, and requires one successful gate from that
attempt. Incomplete or unsuccessful evidence fails closed. All release
checkouts explicitly use `github.sha`. Behavioral tests cover wrong provenance,
missing evidence, failed newer runs, stale attempts, and unsuccessful gates.
The helper also passed a live read-only validation against main's actual run.

### P1 — Publication environment and release tags remain incorrectly configured

Live API verification found one `pypi` deployment policy: `main`, type `branch`.
Publication runs on a release-tag ref, which this branch-only policy does not
allow. There are no required reviewers, administrator environment bypass is
enabled, and there is no active tag ruleset. The sole repository ruleset has
empty branch targets and is not the mechanism protecting main.

This remains an external control gap. The outstanding policy choice is whether
publication should be automated after protected-history validation or require
human approval. Configure permitted `v*` tags, creation authority, immutable tag
updates/deletion, and environment bypass consistently with that choice. Branch
protection alone does not implement tag protection. GitHub documents that
[environment deployment policies match the deployment ref](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
and that [tag rules are configured separately](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).

### P1 — Nix corrections already on main were lost on the current branch

The branch dropped the hash-verified Hatchling 1.32 build dependency, the explicit
Nix runtime-dependency policy, and Git in the test environment. Its pinned nixpkgs
Hatchling could not satisfy the project build floor, while Nix package versions
do not necessarily equal the exact PyPI pins.

The flake restores those corrections and puts Git on the installed application's
PATH. It explicitly runs the tests, checks the installed CLI, and adds a public
checkpoint smoke test. The CI matrix now covers all four declared systems:
x86_64/arm64 on Linux/macOS. Nix uses its commit-pinned package set; PyPI direct
requirements and Docker's lock remain separate, documented policies.

### P1 — The dependency-review result did not block the required CI gate

Dependency review ran in a separate workflow and was not required by main's
protection rule. It now runs as a PR-only job inside CI, and the required gate
depends on its outcome. Moderate-or-higher findings block across runtime,
development, and unknown scopes. Permissions remain read-only. Actual retained
PR logs confirm that GitHub's dependency comparison includes `uv.lock` changes.

### P1 — Change classification and tests did not cover validation inputs

Docker/Nix filters omitted tests, helper scripts, release metadata, and shared
workflow/action inputs. The classifier now has explicit PR metadata read access
and conservatively includes these inputs. The aggregate rejects unsuccessful
required jobs and unexpected outcomes for intentionally omitted jobs.

New tests execute the actual shell gate with success, failure, cancellation,
missing-result, and skipped-result combinations. They do not merely look for
an error string in the workflow.

### P1 — Release test caching still overrode the release trust policy

The release matrix explicitly enabled caching while release builds disabled it.
Both now explicitly disable caching. Release retesting and fresh dependency
auditing remain in place.

### P2 — Scanner classification and metadata checking could misrepresent evidence

The audit wrapper treated exit 1 plus a JSON file as vulnerability evidence,
even for empty or malformed output, and leaked report directories. It now
validates report shape and exit/result agreement, retries scanner failures once,
rejects skipped third-party dependencies, and removes temporary files. The
expected local `hermesd` skip is reported explicitly and remains allowed.
Tests exercise a temporary fake scanner executable through the actual process
boundary, including clean, vulnerable, malformed, inconsistent, and incomplete
reports.

The wheel checker silently collapsed duplicate requirements and used an
overbroad marker heuristic. It now parses PEP 508 requirements and rejects
duplicates. Published exact pins control the three direct runtime dependencies;
they do not freeze the entire transitive graph of an independent PyPI install.

### P2 — Installed smoke tests and the sdist were incomplete

The installed smoke now checks a nonexistent home, collected fixture values,
real log input, snapshot/panel behavior, and unchanged fixture contents. Its
temporary working directories are cleaned up. Wheel and sdist installs are
tested in separate temporary environments outside the checkout.

The sdist omitted helper scripts and the lockfile. Independent testing then
caught that a new policy test also needed the policy documents. All are now
included, and the focused CI tests have been executed from an extracted sdist.

### P2 — Toolchain maintenance and malformed-JSON test portability

Dependabot grouping used ignore-rule values (`version-update:semver-*`) where
grouping requires `minor` and `patch`; the configuration is corrected. Selected
lock updates are pytest **9.1.1**, Ruff **0.16.6**, and types-PyYAML
**6.0.12.20260906**. Cryptography **50.0.1** is retained.

The independent full matrix exposed three existing malformed-JSON tests that
passed on Python 3.11 but failed on 3.12–3.14: their fixed nesting depth did not
consistently trigger the decoder's recursion guard. Real Nix execution also
exposed a timestamp test assuming that the same date overflows on every OS.
Its timezone restoration happened before the environment fixture restored
`TZ`, which could leave the process timezone changed for later tests.

These tests now inject the relevant failures at the standard-library boundary:
marker-specific JSON recursion failures and each supported timestamp-conversion
exception. They exercise hermesd's existing fallback behavior deterministically.
No application runtime behavior or accepted JSON-depth policy was changed.
The parent rejected a broader runtime-depth restriction during recheck and
verified that the final diff contains no `hermesd/` changes.

## Audit acceptance map

| Item | Review outcome |
|---|---|
| CI-01 | Integrated on main; fresh Linux and GitHub security checks pass. Cryptography 50.0.1 preserved. |
| CI-02 | Main's structural fix verified; current-branch Nix regressions corrected locally. |
| CI-03 | Classic main protection verified: PR requirement, strict `CI gate`, administrator enforcement, no force pushes/deletion. Check source is not bound to an app (`app_id: null`). |
| CI-04 | Local exact-SHA eligibility corrected; external environment/tag configuration remains open. |
| CI-05 | Package realization, explicit tests, installed CLI/Git smoke, version derivation, and four-system matrix configured. See execution evidence below. |
| CI-06 | Direct exact pins match built-wheel metadata; duplicate/marker checks strengthened; transitive and Nix distinctions documented. |
| CI-07 | Pydantic 2.13.4/core 2.46.4 already present; current full matrix verifies hermesd's own behavior. |
| CI-08 | Group syntax fixed; weekly Actions/uv/Docker updates and security updates retained. |
| CI-09 | PRs #12/#13/#14/#16/#17/#21/#22 are still closed; no branches deleted. |
| CI-10 | Three selected toolchain versions integrated into the local lock and tested. PRs #18/#19/#20 remain open with green checks; no PR merge/closure performed. |
| CI-11 | Static and security signals separate; per-interpreter audits retained. Static and release type checks now include helper scripts. |
| CI-12 | Weekly installed/lock scans configured; fresh manual run succeeded. Wrapper now rejects incomplete/malformed evidence. |
| CI-13 | Dependency review included in required gate; uv.lock comparison observed. No deliberately vulnerable remote PR was introduced during this review. |
| CI-14 | Classification expanded and actual shell-gate failure behavior tested; trusted integration events retain full validation. |
| CI-15 | Release matrix and fresh security checks retained; no premature deduplication. |
| CI-16 | Both release caches disabled. |
| CI-17 | Cross-run Docker caching remains deferred. Main Docker job took approximately 22 seconds; no measured optimization benefit is claimed. |
| CI-18 | Local-build-only container policy retained; Docker build/runtime validation retained. |
| CI-19 | Isolated installed behavior checks strengthened; sdist helpers, lockfile, and policy files included and tested. |
| CI-20 | Existing JUnit/coverage uploads retained for all outcomes; scanner diagnostics corrected. No claim that XML alone creates annotations. |
| CI-21 | CI smoke environments use RUNNER_TEMP; helper temporary directories now clean up. |
| CI-22 | Docker checkpoint smoke uses the public CLI; Nix now also tests that integration. |
| CI-23 | Existing trusted-publishing attestations retained; extra provenance/Scorecard remain optional. No publication was performed. |
| CI-24 | Canonical policy corrected, command surfaces aligned, and manual terminal/SSH/tmux/read-only checks preserved. |

## Verification and remaining limits

| Final check | Result |
|---|---|
| Python 3.11, isolated source/environment | 2,905 passed, one skipped; 98.37% coverage |
| Python 3.12, isolated source/environment | 2,905 passed, one skipped; 98.37% coverage |
| Python 3.13, isolated source/environment | 2,905 passed, one skipped; 98.37% coverage |
| Python 3.14, isolated source/environment | 2,905 passed, one skipped; 98.36% coverage |
| Focused CI tests from extracted sdist | 62 passed |
| Ruff lint/format | Passed |
| Mypy, application and helper scripts | Passed, 49 files |
| Actionlint | Passed |
| Lockfile freshness, compile, diff checks | Passed |
| Wheel/sdist build, wheel pins, Twine | Passed |
| Independent installed wheel/sdist behavior smoke | Passed |
| Docker image build, installed CLI, checkpoint smoke | Passed |
| Fresh Linux installed dependency audit | Passed; expected local-project skip reported |
| Fresh hosted installed/lock security workflow | Passed on recorded main SHA |
| Final ARM Linux Nix build and installed checks | 2,905 passed, one skipped; package, CLI, and checkpoint checks passed |

Python verification logs are under
`/var/folders/xf/mw036rf17qjg_fcbz4_r_zw00000gn/T/hermesd-cicd-final-snapshots-3eof2b7s/`.
Each interpreter has its own source directory, environment, coverage data, and
JUnit file. This avoids mixing coverage from source revisions changed by the
concurrent merges. The intermediate shared-tree coverage run was rejected;
only the successful fixed-snapshot results above are final acceptance evidence.

The final Nix command ran `nix flake check path:/src --no-write-lock-file -L`
in a `nixos/nix:2.31.3` container with that same source snapshot mounted read-only.
It exited 0, executed the complete pytest hook, and realized both installed
smoke derivations. The log is `/tmp/hermesd-cicd-nix-final.log`. Native Nix
validation of x86_64 Linux and both Darwin targets still requires the configured
GitHub matrix; the local ARM Linux result is not evidence for those targets.

Final comparison confirmed that the working tree's Python source, tests, and
helpers match the successfully validated snapshots. Local HEAD remained
`f16ef62` at completion.

Hosted validation of the new local changes remains necessary after integration.
Live-main CI and Security results above validate their recorded SHA, not the
uncommitted working tree. No actual PyPI publication or destructive protection
probe was performed.

Manual TUI/SSH/tmux, screenshot accuracy, and a live-home contract run were not
part of this CI/CD review. Read-only runtime checks used synthetic homes.
