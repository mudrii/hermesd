# Dependency decisions

Recorded compatibility and upgrade decisions for hermesd's dependency
baseline. Each entry states the decision, the evidence gathered on hermesd
itself, and the policy that produced it. Versions always come from
`uv.lock`; the exact-pins policy for published runtime requirements is
documented in [`ci-release-policy.md`](ci-release-policy.md).

## 2026-09: retained development-tool updates

**Decision: upgrade.** The three retained maintenance changes were applied to
the development lock together: pytest 9.0.3 → 9.1.1, Ruff 0.15.11 → 0.16.6,
and types-PyYAML 6.0.12.20260408 → 6.0.12.20260906. They remain development
dependencies and do not change hermesd's published runtime requirements.

The refreshed lock passes `uv lock --check`; the selected Ruff version passes
the repository lint and format gates, and the CI/package policy tests pass
with pytest 9.1.1 and the newer type stubs installed. Complete supported-Python,
artifact, and audit evidence is recorded in
[`../reports/cicd-implementation-review-2026-09-13.md`](../reports/cicd-implementation-review-2026-09-13.md).

**Policy applied:** minor/patch development tooling updates may be reviewed
together, but each selected version must pass hermesd's own gates. A matching
version in another project is supporting context, not acceptance evidence.

## 2026-09: Pydantic 2.12.5 → 2.13.4 (core 2.41.5 → 2.46.4)

**Decision: upgrade.** hermes-agent upgraded to pydantic 2.13.4/core 2.46.4
to fix a crash in its threaded OpenAI Responses path. That failure mode does
not exist in hermesd (no OpenAI Responses usage), so version matching alone
was not a justification — the upgrade was validated against hermesd's own
behavior first, per the dependency policy.

**Validation evidence (pydantic 2.13.4 + pydantic-core 2.46.4):**

| Gate | Python | Result |
|------|--------|--------|
| Full suite (`pytest tests/ -q -W error::ResourceWarning`) | 3.11 | 2487 passed, 1 skipped |
| Full suite | 3.12 | 2487 passed, 1 skipped |
| Full suite | 3.13 | 2487 passed, 1 skipped |
| Full suite | 3.14 | 2487 passed, 1 skipped |
| `mypy hermesd` (strict-family settings) | 3.11 | no issues in 44 files |

Model validation, collector threading, and JSON snapshot behavior are
exercised directly by `tests/test_main.py` (snapshot modes),
`tests/test_collector*.py` (per-source readers, last-good fallback),
`tests/test_app_input.py` (input/collect threading), and
`tests/test_contract.py` (state contract) — all green under the newer pair.

**Scope note:** this validates hermesd's usage surface (model construction,
serialization, threaded collection, snapshot rendering). hermes-agent's
crash was in code hermesd does not import; the upgrade here removes version
divergence and inherits the newer core's fixes, not a repair for a shared
defect.

**Policy applied:** upgrades land only after the complete supported Python
matrix and type gate pass on hermesd itself; alignment with hermes-agent is
a hint, never a substitute for evidence.
