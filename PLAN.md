# hermesd Implementation Plan — Drift Fixes + hermes-agent Integrations

**Status:** plan only, no code written. Validated (Pass 1) + revalidated (Pass 2) against live source,
on-disk `~/.hermes`, and the hermes-agent producer (`/Users/mudrii/.hermes/hermes-agent` @ `526a1e24b`,
schema_version 16, config_version 29). Comparison baseline date: 2026-06-14.

Legend — **Existence**: `LIVE` (data present & populated now) · `SCHEMA` (table/producer exists, 0 rows) ·
`ABSENT` (not on this host). **Effort**: S/M/L. **Value**: HIGH/MED/LOW.

## Invariants every step must hold

- **Read-only** to `~/.hermes` (DB `mode=ro&immutable=1`; files via `file_cache`).
- **No hermes-agent import.**
- **NULL-tolerant** (`or 0` / `or ""`).
- **Cache-preservation** (each `safe_collect` source returns last-good on error).
- **TDD** (red fixture/test → minimal collector/model/panel → green).
- **Markup-safe** (escape all untrusted free-text in panel cells).
- Update `CHANGELOG.md` for every user-visible change.

## Sequencing

```
FIX B ──┬─> C2  (context/limit gauge)
        ├─> NF1 (billing-endpoint breakdown)
A1, A2, A3, A4, A5, A6, C1   (independent, parallelizable)
A4 ──────> NF2 (cost reconciliation)
                                       └─> C3 (new panel, independent)
```

Recommended order: **B → A1 → C1 → A4 → A2 → A3 → A5 → A6 → C3 → C2 → NF1 → NF2 → NF3 → NF4 → NF5**.

---

# PART 1 — FIXES (repair broken / drifted reads)

## FIX B — surface `end_reason` / `billing_base_url` / `billing_mode` — `LIVE` · HIGH · S
*(enhancement: columns exist in schema, unread; do first — unblocks C2 & NF1)*

Live: `end_reason` (cron_complete 1329, cli_close 36, compression 17, session_reset 32, tui_close 11, new_session 5);
`billing_base_url` (15 endpoints: kimi/minimax/codex/z.ai/anthropic…); `billing_mode` (subscription_included 114).

**Change-set**
1. `db.py:200-226` `wanted_columns` — add `"end_reason"`, `"billing_base_url"`, `"billing_mode"`
   (PRAGMA-guarded allowlist drops absent columns — safe).
2. `models.py:26` `SessionInfo` — add `end_reason: str = ""`, `billing_base_url: str = ""`, `billing_mode: str = ""`.
3. `collector.py:542-569` `_collect_sessions` row→`SessionInfo` — `end_reason=r.get("end_reason") or ""` etc.
4. `panels/sessions.py` `_render_detail` (~78-151) — add columns (`escape(...) if val else "—"`).

**Test-first**
- Red: extend `test_session_active.py:93-111` (mapping) + `test_panels_extended.py:32-72` (detail render).
  `sample_db` schema already has the columns (`conftest.py:73,82,83`); positional INSERTs populate them — no fixture edit.
- **Gotcha (will break, intended):** `test_db_extended.py:63` `assert "billing_base_url" not in row` — update it
  (now allowed; keep `system_prompt`/`model_config` excluded).
- JSON snapshot auto-includes via `model_dump` (`app.py:279`).

## FIX A1 — `credential_pool` list-vs-dict — `LIVE` · HIGH · M
*(dead credential panel — the flagship bug)*

Live `auth.json` → `{"openai-codex": [ {label,auth_type,source,last_status,last_status_at,priority,request_count,
last_error_*,base_url,id} ]}`. Entries self-identify by `id`/`label` only — **provider is the outer dict key**.
hermesd does `_as_dict(raw_entry)` on a list → `{}` → every credential field blanks. Tests miss it: `sample_auth`
fixture still uses the old dict-of-dict shape.

**Decision (revalidated):** accept **both** shapes; thread the outer key down as provider name.

**Change-set**
1. `collector.py:1170-1190` `_collect_credential_pools` — `entries = raw if isinstance(raw, list) else [raw]`;
   pick `entries[0]` (or min-`priority`); `entry = _as_dict(entries[0])`; use outer `name` as `CredentialPoolEntry.name`.
2. `collector.py:1161` `_collect_providers` — verify `pool.keys()` not collapsed by `_as_dict`.
3. `models.py:204` `CredentialPoolEntry` — **defer** new fields unless `overview.py:77-99` renders a column
   (optional follow-up: add `last_error_message` + a detail column — live `last_error_*` is informative).

**Test-first:** new list-shaped `auth.json` test asserting fields populated (today blank). Add `sample_auth_list`
fixture **alongside** `sample_auth` (don't churn the ~6 dict-fixture tests).

## FIX A2 — desktop-build-stamp camelCase — `LIVE` · MED · S

Live keys `contentHash`/`sourceMode`/`builtAt`; hermesd reads `version`/`stamp`/`built_at`/`created_at` → always `""`.

**Change-set:** `collector.py:894-900` — add `builtAt` (+ `contentHash[:12]`) to the `or` chain.
**Test-first:** no fixture exists today (parse block never exercised). Add `sample_desktop_stamp` fixture (camelCase)
+ collector test; wire into `populated_hermes_home`.

## FIX A3 — pr-monitor `monitored`/`tracked` keys gone — `LIVE` · MED · S

Live keys `tracked_numbers`/`prs`/`author_prs`/`checked_at`; `monitored`/`tracked` absent → counts stuck at 0.

**Change-set:** `collector.py:943-944` — `tracked_count = len(data.get("tracked_numbers") or [])`;
map `monitored_count` to `len(data.get("prs") or [])` or drop.
**Test-first:** update `conftest.py sample_pr_monitor` (628-641) to real keys; flip `test_collector.py:305-306`
assertion together. `test_collector_extended.py:610` unaffected.

## FIX A4 — cost prefix: map `included`/`exact` to authoritative `$` — `LIVE` · MED · S

Producer vocabulary = `unknown`/`estimated`/`exact`/`included`; **never `reported`** (`run_agent.py:639`,
`gatewayTypes.ts:231`). `"reported"` is a hermesd/fixture-only token (23 test occurrences). Live `included`(110, all
`subscription_included`, cost `0.0`). So the `$` (authoritative) branch is **never taken** on live data — every cost
renders `~$`.

**Decision:** do NOT delete the `reported` branch (test-load-bearing). **MAP** `cost_status in
{"reported","included","exact"}` → authoritative.

**Change-set**
1. `collector.py:1469-1473` `_session_cost_is_reported` → `cost_status in {"reported","included","exact"}`
   (feeds `cost_is_estimated` at `:1395`).
2. `panels/tokens.py:62` → `"$" if s.cost_status in {"reported","included","exact"} else "~$"`.

**Test-first:** new cases — `included`→`$0.00`, `exact`→`$`. No FLIP (no live-value fixtures exist).
CHANGELOG caveat: `included` rows now show `$0.00` (subscription-covered = genuinely $0).

## FIX A5 — dead `checkedAt` camelCase first-guess — `LIVE` · LOW · S
*(cosmetic cleanup; harmless today via fallback)*

`collector.py:942` tries `data.get("checkedAt")` first; live key is snake `checked_at` (fallback already works, so no
runtime breakage). Same camelCase-first anti-pattern as A2/A3.

**Change-set:** drop the dead `checkedAt` first-guess (or keep — purely tidy). **Test-first:** none required (no
behavior change); fold into A3's pr-monitor test if touched.

## FIX A6 — pr-monitor underscore family not globbed — `LIVE` · MED · S
*(missed PR state; can ship as a fix to A3's glob or as NF6)*

hermesd globs `pr-monitor-*.json` (hyphen). Live also has `pr_monitor_state.json` (61K), `pr_monitor/state.json`,
`pr-monitor/<repo>-prs.json` — large active PR state, never read.

**Change-set:** `collector.py:932-948` — broaden glob / add a second read for the underscore + subdir families, or
explicitly choose the canonical one (verify which the agent currently writes as authoritative).
**Test-first:** add an underscore-family fixture + assertion. Validate the real shape before merging (fields differ
from the hyphen family).

---

# PART 2 — NEW FEATURES (near-term — data present now)

## C1 — Gateway health enrichment — `LIVE` · HIGH · S — EXTEND gateway panel

Live `gateway_state.json`: per-platform `error_message` (discord "failed to reconnect") + `error_code`; top-level
`active_agents`, `restart_requested`. hermesd reads none.

**Change-set**
1. `models.py:11` `PlatformStatus` — `error_code: str = ""`, `error_message: str = ""`.
2. `models.py:17` `GatewayState` — `active_agents: int = 0`, `restart_requested: bool = False`.
3. `collector.py:465-471` / `:492-499` — populate (NULL-tolerant `or ""`, `_coerce_int`, `bool(...)`).
4. `panels/gateway.py` — compact ⚠ + error_message; detail error column.

**Test-first:** extend `sample_gateway_state` (205) + `populated_hermes_home` (903); assert discord error renders.
`safe_collect` already registers `"gateway"` — no new source.

## C3 — Curator panel — `LIVE` · HIGH · L — NEW panel 13 "Curator"

`~/.hermes/logs/curator/<UTC-stamp>/run.json` — 7 runs; rich (`counts{before,after,delta}`, `archived[]`, `pruned[]`,
`added[]`, `model`, `provider`, `duration_seconds`, `tool_call_counts{}`, `llm_error`, `state_transitions[]`).

**Change-set (full new-panel registration)**
1. `models.py` — `CuratorRun(BaseModel)` + `curator: CuratorRun` on `DashboardState` (408).
2. `collector.py` — `_collect_curator()` (newest = `sorted(glob("logs/curator/*/"))[-1]`, parse `run.json`,
   `or` defaults, `empty_curator()` factory).
3. `collector.py:~315` — `safe_collect(..., "curator", self._collect_curator, CuratorRun)` (**new source**).
4. `collector.py:357` — pass `curator=curator`.
5. `panels/__init__.py:137,153` — `PANEL_NAMES[13]="Curator"` + `_RENDERERS[13]`.
6. **new** `panels/curator_panel.py` — compact + detail (escape free-text).
7. `app.py:44,53` — add `13` to `_WIDE` + `_COMPACT` (`_TALL_NARROW` auto; snapshot auto via `PANEL_NAMES`).
8. Docs: README panel table + shortcuts, CHANGELOG, CLAUDE.md panel count.

**Test-first:** `sample_curator` fixture → `populated_hermes_home`; `test_curator_panel.py`, `test_collector` curator,
`test_curator_resilience.py` (cache-preservation), empty-state ("No curation runs").

## C2 — Token/limit gauge — `LIVE` · MED · M — EXTEND sessions detail — **needs FIX B**

`context_length_cache.yaml` → `context_lengths: { "model@base_url": int }`. Join needs `billing_base_url` (FIX B).

**⚠ Rescope (Pass 2):** `sessions.input_tokens` is **lifetime-cumulative**, NOT live context occupancy — label it
"lifetime tokens vs model context limit", not "% used".

**Change-set (after B):** `_read_context_lengths()` (cached YAML, `.get("context_lengths", {})`);
`SessionInfo.context_limit: int = 0`; join `f"{model}@{billing_base_url}"` with trailing-slash normalize (don't
lowercase — keys case-mixed); `panels/sessions.py` detail column.
**Test-first:** yaml fixture + base_url rows; assert ratio + slash fallback; resilience (missing yaml → 0, no crash).

## NF1 — Billing-endpoint breakdown — `LIVE` · MED · M — **needs FIX B** — EXTEND tokens panel

From `billing_base_url`: aggregate spend + tokens **per actual endpoint** (kimi 873, minimax 441, codex 111, z.ai…).
Today the Tokens/Cost panel breaks down by provider/model — endpoint is finer (same provider, different base_url).

**Change-set:** new aggregate in `_collect_token_analytics`-style summarizer keyed on `billing_base_url`; render as a
"By Endpoint" sub-table in `panels/tokens.py` detail. Reuse the existing window/breakdown rendering pattern.
**Test-first:** sessions across ≥2 endpoints; assert per-endpoint totals.

## NF2 — Cost reconciliation view — `LIVE` · MED · S — **pairs with A4** — EXTEND tokens panel

Surface the `cost_status` distribution (live: unknown 1471, included 110, estimated 10) so the user sees how much
spend is unknown vs subscription-covered vs estimated — a one-line health bar in the Tokens/Cost panel.

**Change-set:** count sessions by `cost_status` in the token summarizer; render a compact breakdown line.
**Test-first:** mixed-status sessions; assert the counts render.

## NF3 — Response-store cache stats — `LIVE` · LOW/MED · S — EXTEND operations panel

`~/.hermes/response_store.db` (tables `conversations`, `responses`) — gateway idempotency/response cache. Surface row
counts + size in Operations.

**Change-set:** read-only `?mode=ro` open (reuse db helpers); `OperationsState` fields; `panels/operations.py` line.
**Test-first:** sample response_store.db fixture; assert counts.

## NF4 — Extra log streams (`audit.log`, `mcp-stderr.log`) — `LIVE`(audit) · LOW/MED · S — EXTEND logs panel

hermesd already tails agent/gateway/errors/desktop/etc. Add `logs/audit.log` (present) and `logs/mcp-stderr.log` as
new Tab sub-views in panel 8.

**Change-set:** add streams to the log-collection list + `Tab` cycle; NULL/missing-tolerant.
**Test-first:** fixture logs; assert the new sub-views appear and tail correctly.

## NF5 — Kanban run/task enrichment — `LIVE`(tasks/runs) · MED · M — EXTEND kanban panel

hermesd reads a subset. Surface (when present): `tasks.completed_at`, `workspace_path`/`branch_name`, `goal_mode`,
`current_step_key`; and `task_links` (parent→child decomposition tree), `task_attachments` count
(both `SCHEMA`, 0 rows now — render only when populated, guarded).

**Change-set:** extend kanban SELECT/model/panel (kanban.db read is already `SELECT *`-tolerant — additive).
**Test-first:** kanban fixture rows with the extra columns + a `task_links` row; assert render. Guard empty case.

---

# PART 3 — FUTURE FEATURES (build when data present — `SCHEMA`/`ABSENT` now)

Each is a confirmed hermes-agent producer with **no live data on this host** — defer until populated, then build with
the same collector→model→panel→resilience pattern.

| Feature | Source | Status | Trigger to build | Value |
|---|---|---|---|---|
| **Pairing/approval queue** | `pairing/<platform>-pending.json` (`gateway/pairing.py`) | `ABSENT`/empty | a platform starts using pairing | MED |
| **Shutdown forensics** | `logs/gateway-shutdown-diag.log` + `.gateway-takeover.json` / `.gateway-planned-stop.json` | `SCHEMA` (log 0 bytes; markers transient) | an unclean shutdown occurs | MED |
| **Holographic memory** | `memory_store.db` (`agent/holographic_*`) | `ABSENT` | memory provider that writes the DB is enabled | MED |
| **Snapshots** | `singularity_snapshots.json` / `modal_snapshots.json` | `ABSENT` | remote-snapshot feature used | LOW |
| **Telegram DM topics** | `state.db` `telegram_dm_topic_*` tables | `SCHEMA` | telegram DM topic mode used | LOW |
| **Spawn trees** | `spawn-trees/` dir | `ABSENT` | sub-agent spawning used | LOW |
| **Retain queue** | `retaindb_queue.db` | `ABSENT` | retention queue active | LOW |

---

# PART 4 — META / ROOT-CAUSE GUARD

**Contract test.** A1–A6 all stem from the agent rewriting JSON/DB shapes (dict→list, snake→camel, retired enum,
moved glob) while hermesd fixtures froze the old shape — invisible to a green suite. Add an **opt-in contract test**
(`HERMESD_CONTRACT_TEST=1`, skipped in CI default) that runs the collector against the real `~/.hermes` and asserts
key fields are non-empty (credential-pool labels present, gateway platforms populated, desktop stamp non-blank,
pr-monitor counts plausible, sessions billing fields present). Catches the *next* drift the moment it lands without
coupling CI to a machine's data.

**Per-step verification gate:** `pytest tests/ -W error::ResourceWarning` + `ruff check` + `ruff format --check` +
`mypy hermesd` + `pip-audit` + targeted `hermesd --snapshot-panel N` against live data. New panels/fields → CHANGELOG.

---

# Effort / value index

| Item | Type | Value | Effort | Dep | Existence |
|---|---|---|---|---|---|
| B end_reason/billing_* | enhancement | HIGH | S | — | LIVE |
| A1 credential list | fix | HIGH | M | — | LIVE |
| C1 gateway rich | feature | HIGH | S | — | LIVE |
| A4 cost map | fix | MED | S | — | LIVE |
| A2 desktop stamp | fix | MED | S | — | LIVE |
| A3 pr-monitor keys | fix | MED | S | — | LIVE |
| A5 checkedAt cleanup | fix | LOW | S | — | LIVE |
| A6 pr-monitor underscore | fix | MED | S | — | LIVE |
| C3 curator panel | feature | HIGH | L | — | LIVE |
| C2 token/limit gauge | feature | MED | M | B | LIVE |
| NF1 billing-endpoint breakdown | feature | MED | M | B | LIVE |
| NF2 cost reconciliation | feature | MED | S | A4 | LIVE |
| NF3 response-store stats | feature | LOW/MED | S | — | LIVE |
| NF4 extra log streams | feature | LOW/MED | S | — | LIVE |
| NF5 kanban enrichment | feature | MED | M | — | LIVE/SCHEMA |
| Part 3 future features | feature | varies | — | data | SCHEMA/ABSENT |
| Contract test | infra | HIGH | S | — | — |

---

## Refuted during investigation (documented non-issues — do NOT "fix")

- **`smart_model_routing`** — valid *optional* config section (`AGENTS.md:584`); hermesd reads defensively. Not a phantom.
- **`BOOT.md`** — user-authored optional convention, never agent-written (`tips.py:364`); presence indicator is correct.
- **`_config_version`** — hermesd never reads it; no drift.
- **`actual_cost_usd` / `cost_source`** — read-omitted but 0 populated live → no payoff until the agent records billed costs.

---

_Generated from a 3-pass investigation (map → revalidate → revalidate) plus 2-pass plan validation.
No source code was modified in producing this plan._
