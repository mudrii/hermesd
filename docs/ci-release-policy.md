# CI/CD & Release Policy

This is the canonical statement of hermesd's CI/CD and release policy
(audit CI-24). Workflows, `AGENTS.md`, `CONTRIBUTING.md`, the README, and PR
checklists link here instead of restating it. Dependency-upgrade decisions
and their evidence live in [`dependency-decisions.md`](dependency-decisions.md).

## Validation gates

| Gate | Where | What it proves |
|------|-------|----------------|
| Static analysis | CI `static` job | lint, format, types, compilation |
| Dependency audit | CI `security` job + weekly `security.yml` | locked environment has no known advisories, per supported Python |
| Tests | CI `test` matrix + `macos` job | behavior on Python 3.11–3.14 and macOS |
| Package | CI `package` job | wheel/sdist build, exact-pin metadata, install + behavior smoke, twine |
| Docker | CI `docker` job | image builds; installed CLI and git-checkpoint support work |
| Nix | CI `nix` matrix (Linux + macOS, x86_64 + arm64) | flake builds the package, runs its pytest suite, smokes the installed CLI and Git checkpoint collection |
| Dependency review | CI `dependency-review` job (pull requests) | changed runtime, development, and unknown-scope dependencies meet the vulnerability policy |
| **CI gate** | CI `gate` job | single aggregate required check: every expected job succeeded or every intentionally omitted job was skipped |

Rules that keep the gate honest:

- The gate always runs and treats **any non-success outcome of expected
  work** as failure. An intentionally omitted job must report `skipped`; a
  cancelled, missing, failed, or unexpectedly executed job fails closed.
- Docker and Nix run on pull requests only when their classified inputs
  changed. The conservative classification includes application source,
  tests, CI helpers, package/release metadata, project rules, documentation,
  scripts, and workflows, so a relevant change cannot silently skip them.
  The classifier has explicit read access to pull-request file metadata.
  Pushes to `main` and manual dispatch always run Docker and Nix.
- pip-audit outcomes are classified by `scripts/pip_audit_gate.py`:
  vulnerabilities block; scanner/network failures retry once, then fail as
  infrastructure errors so a broken scan can never look clean.

## Branch protection (CI-03)

`main` is protected by a classic branch protection rule. The live settings were
verified on 13 September 2026:

1. Integration into `main` is pull-request-only; direct pushes are rejected.
2. Required check: the **`CI gate`** context, bound to the GitHub Actions app
   (app ID 15368). Red or missing checks block merge.
3. Strict-freshness ("require branches to be up to date") is enabled so every
   merge carries current validation evidence.
4. Force pushes and branch deletion are denied.
5. Administrator enforcement is enabled (`enforce_admins: true`), so no role
   merges without the gate. Emergency exceptions happen by reverting through a
   PR, not by bypass.

## Release eligibility (CI-04)

A tag is publish-eligible only when all of the following hold:

1. The tagged commit belongs to the currently protected `main` history.
2. The `release-eligibility` job resolves the CI workflow currently present
   on `main`, selects the newest `main` push run for the exact release SHA,
   refreshes its latest attempt, and requires that attempt's **`CI gate`** job
   to have the expected workflow provenance, completed status, and successful
   conclusion. Missing, failed, cancelled, stale, or still-running evidence
   refuses publication.
3. Tag/package-version/changelog agreement (checked in `release-build`).
4. The `pypi` GitHub environment accepts only `v*` tag refs. Publication uses
   OIDC trusted publishing after the eligibility and release-test jobs succeed.

**Verified repository controls (13 September 2026):** the `pypi` environment
has one deployment policy: pattern `v*`, type `tag`. The former `main`-branch
policy was deleted. Publishing is automated with no required reviewers; its
administrator bypass is disabled. The workflow still requires protected-`main`
ancestry, exact-SHA CI evidence, and fresh dependency audits before the publish
job can reach the environment.

Two active rulesets target `refs/tags/v*`:

- **Release tag creation** (ID 23175145) applies a `creation` rule. Its only
  bypass actor is the administrator repository role (role ID 5), and that
  bypass applies only to creation.
- **Immutable release tags** (ID 23175146) applies `update`, `deletion`, and
  `non_fast_forward` rules, with no bypass actor.

These tag rulesets control who can create a release tag and make it immutable
after creation. They do not prove protected-`main` ancestry; the
`release-eligibility` job enforces that separately before publication.

## Dependency policy (CI-06/07/08/13)

- **Published direct runtime requirements are exact pins** matching their
  entries in `uv.lock` (enforced by `scripts/check_wheel_pins.py` in package
  and release builds). This controls Rich, PyYAML, and Pydantic directly; it
  does not claim that the complete set of transitive dependencies in an
  independent PyPI install matches the development lock. Bump each direct pin
  and the lock together.
- Dev toolchain extras keep floor pins; `uv.lock` is the real development
  environment. Docker installs that lock. Nix deliberately uses the versions
  in its commit-pinned nixpkgs package set and validates that set independently
  with the full tests and installed CLI/Git smoke checks.
- Intel macOS uses the separately pinned `nixpkgs-26.05-darwin` package set;
  the primary Nixpkgs revision no longer supports `x86_64-darwin`. All four
  systems remain required in CI. Review this Intel package-set choice before
  its upstream security-support window ends at the end of 2026.
- Dependabot opens weekly PRs for GitHub Actions (minor/patch grouped,
  majors separate), uv, and Docker. Security alerts and security updates
  from repository settings are always active and unaffected by cadence.
- Dependency changes in PRs pass the SHA-pinned `dependency-review` job inside
  the required **`CI gate`**. Moderate-or-higher findings in runtime,
  development, or unknown scope block; missing snapshots are retried and
  reported rather than silently passed. GitHub's dependency snapshot includes
  `uv.lock` changes, as verified on retained dependency PRs.
- Pydantic-style cross-project alignment is a hint, not a justification:
  upgrades land only after the full matrix and type gates pass on hermesd,
  recorded in [`dependency-decisions.md`](dependency-decisions.md).

## Retained by decision (CI-15/16/18/23)

- **Release matrix re-validation is retained** until branch protection and
  the eligibility gate above have been observed working in practice; only
  then consider reusing exact-SHA evidence (CI-15).
- **Release test and release-build caching stay disabled** (both locked-env
  invocations explicitly set `enable-cache: "false"`). Changing
  release-sensitive caching requires a written cache-trust assessment first
  (CI-16).
- **Containers are local-build only.** hermesd documents `docker build` for
  local use; there is no published image registry. If that changes, define
  registry, architectures, tags, cadence, and scanning first — and keep any
  Python interpreter-baseline bump separate from image maintenance (CI-18).
- **Provenance:** PyPI publish attestations are generated by the trusted
  publishing action. Additional build attestations wait until a consumer or
  policy actually verifies them; Scorecard adoption waits until the required
  controls above are enforced (CI-23).

## Manual checks CI cannot replace (CI-24)

CI green plus a changelog entry is necessary but not sufficient. Releases
also require human verification of:

- TUI usability in a real terminal (SSH and tmux sessions),
- screenshots used by the README reflecting the current UI,
- real-world read-only behavior against a populated `~/.hermes`,
- the release notes' accuracy against the changelog.
