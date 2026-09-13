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
| Nix | CI `nix` job (Linux + macOS) | flake builds the package, runs its pytest suite, smokes the installed CLI |
| **CI gate** | CI `gate` job | single aggregate required check: every expected job succeeded |

Rules that keep the gate honest:

- The gate always runs and treats **any non-success outcome of expected
  work** as failure — including cancelled jobs and skipped-by-classification
  jobs whose filters say they should have run. Nothing but `success` passes.
- Docker and Nix run on pull requests only when their classified inputs
  changed. The classification includes application source (`hermesd/**`),
  package metadata (`pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`), and
  the CI workflow itself, so relevant changes cannot silently skip them.
  Pushes to `main` and manual dispatch always run everything.
- pip-audit outcomes are classified by `scripts/pip_audit_gate.py`:
  vulnerabilities block; scanner/network failures retry once, then fail as
  infrastructure errors so a broken scan can never look clean.

## Branch protection (CI-03)

`main` is protected by ruleset. Policy:

1. Integration into `main` is pull-request-only; direct pushes are rejected.
2. Required check: the **`CI gate`** context (plus the job contexts, if
   required explicitly). Red or missing checks block merge.
3. Strict-freshness ("require branches to be up to date") is enabled so every
   merge carries current validation evidence.
4. Force pushes and branch deletion are denied.
5. Administrator/bot bypass: **disabled** — no role merges without the gate.
   Emergency exceptions happen by reverting through a PR, not by bypass.

## Release eligibility (CI-04)

A tag is publish-eligible only when all of the following hold:

1. The tagged commit is the tip of `main` (or an ancestor reached through the
   protected merge history above).
2. The exact tagged commit carries a completed, successful **`CI gate`** run.
   The `release-eligibility` job in `python-publish.yml` enforces this
   mechanically: missing, failed, cancelled, stale, or still-running
   validation refuses to publish.
3. Tag/package-version/changelog agreement (checked in `release-build`).
4. The `pypi` GitHub environment is restricted to `main`-based deployments
   and publishes only through OIDC trusted publishing.

Tag protection (CI-04): `v*` tags are created only from protected `main`
history; tag creation/update rights follow the same restriction as pushes.

## Dependency policy (CI-06/07/08/13)

- **Published runtime requirements are exact pins** matching `uv.lock`
  (currently enforced by `scripts/check_wheel_pins.py` in the package and
  release builds). A fresh install resolves to the same versions exercised by
  CI. Bump the pin and the lock together.
- Dev toolchain extras keep floor pins; `uv.lock` is the real development
  environment. The Docker environment installs the same locked pins; the Nix
  environment satisfies runtime requirements from nixpkgs, whose in-tree
  versions may run ahead of the lock, and validates them with the build-time
  pytest suite plus the installed-CLI smoke rather than wheel metadata.
- Dependabot opens weekly PRs for GitHub Actions (minor/patch grouped,
  majors separate), uv, and Docker. Security alerts and security updates
  from repository settings are always active and unaffected by cadence.
- Dependency changes in PRs pass the SHA-pinned `dependency-review` gate
  (moderate severity or higher blocks; missing snapshots are retried and
  reported, never silently passed).
- Pydantic-style cross-project alignment is a hint, not a justification:
  upgrades land only after the full matrix and type gates pass on hermesd,
  recorded in [`dependency-decisions.md`](dependency-decisions.md).

## Retained by decision (CI-15/16/18/23)

- **Release matrix re-validation is retained** until branch protection and
  the eligibility gate above have been observed working in practice; only
  then consider reusing exact-SHA evidence (CI-15).
- **Release-build caching stays disabled** (the locked-env composite is
  explicitly `enable-cache: "false"` there). Changing release-sensitive
  caching requires a written cache-trust assessment first (CI-16).
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
