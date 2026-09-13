# Remediation report — review findings on `audit-2026-09`

**Branch:** `fix/audit-review-followups` (stacked on `audit-2026-09` @ `6655f85`).
**Scope:** every confirmed finding in `reports/audit-2026-09-implementation-review.md`.
**Method:** repo TDD — failing test first (red captured), minimum implementation,
green, then one commit per finding. Five reviewers worked in isolated git
worktrees (`fix/kanban-findings`, `fix/operations-findings`, `fix/config-findings`,
`fix/gateway-findings`, `fix/sessions-findings`), merged back with `--no-ff` so
each original commit survives.

**Result:** 54 non-merge commits (fix/test/docs) + 10 merges, 47 files, +12 272/−119.
At the time of writing the suite was `2798 passed, 2 skipped`; the branch has since
grown to **74 fix/test/docs commits** and the numbers moved — the *current* measured
values are in §6 and are the only ones to quote. Every fix was additionally proven
pinned by backing it out again — see §8; §9 records the follow-up flag sweep, and
`reports/audit-2026-09-consolidated-review-report.md` covers the review that followed
this report, including the findings raised against it.

---

## 1. Blockers

| # | Finding | Fix | Commit | Pinned by |
| --- | --- | --- | --- | --- |
| 4.1 | Notify backlog counted the global `task_events` id gap (21 shown for 1 unseen event) | Correlated `COUNT(*) … e.task_id = s.task_id AND e.id > s.last_event_id`; `max_event_id` kept as "newest" | `5c4d57c` | `test_collect_kanban_notify_backlog_counts_only_this_tasks_unseen_events` (RED: `assert 21 == 1`), `…_zero_when_cursor_is_past_newest_event`, `…_zero_for_task_without_events` |
| 4.2 | `delegation_live` tail-read a log for **every** manifest entry, then displayed eight | Raw entries materialised, sliced to `_MAX_LIVE_TASKS`, only then tailed; `running_task_count`/`tasks_truncated` still over all entries; dead `_MAX_LIVE_LOG_TAILS` deleted | `0a08eec` | `test_delegation_live_manifest_tails_only_the_displayed_tasks` (RED: `assert 12 == 8` tail calls) |
| 4.3 | Deeply nested JSON raised `RecursionError` out of the cap helper, failing four sources on junk | `RecursionError` added to the suppressed set (and to the two other bare `json.loads` sites) | `3ab2f77` | `…_deeply_nested_json_is_healthy` ×2 + `test_json_decode_helpers_treat_deep_nesting_as_junk` (RED: `failed_sources == ['delegation_live']`) |
| 4.4 | Backup group cap sorted by reason, so it could drop the `good`/`corrupt` groups and print a false all-clear | Kinds ranked (`good`, `corrupt`, …) before the cut; truncation flagged in compact too | `001b66c` | `test_config_backup_rows_survive_the_group_cap` (RED: `'no good copy'`), `test_collect_config_backups_keep_good_and_corrupt_past_the_group_cap`, `test_config_compact_flags_truncated_backup_groups` |
| 4.5 | A corrupt catalog cache read as empty, so the panel said "plugins match the catalog" | `parse_catalog_cache` returns `None` for a payload upstream would refuse; new `plugin_catalog_cache_usable` renders "unreadable — checks unavailable"; a parsed-but-empty cache still counts as evidence | `e88d83f` | `test_parse_catalog_cache_rejects_payloads_upstream_would_refuse`, `test_collector_junk_cache_payload_makes_no_update_claims` (RED: `AttributeError`), `test_panel_reports_an_unusable_catalog_cache_as_unavailable`, `test_panel_still_reports_a_readable_empty_cache_as_a_match` |

## 2. Upstream-fidelity items

| # | Finding | Fix | Commit | Pinned by |
| --- | --- | --- | --- | --- |
| 4.6 | Storm cap/window hardcoded 5/120 s; `config.yaml` ignored | `_respawn_storm_policy` mirrors upstream's isinstance guards; reader takes cap+window; `restart_storm_window_seconds` rendered instead of "2m"; `max_starts <= 0` ⇒ no verdict; `gateway_starts_2m` → `gateway_starts_window` | `d936145` | `test_restart_storm_uses_the_configured_cap/window`, `…_disabled_writer_makes_no_claims`, `test_respawn_storm_policy_reads_only_upstream_shapes` |
| 4.7 | `max_retries = 0` treated as unset (upstream trips immediately) | NULL kept distinct from 0; `_breaker_limit(int \| None, int)` returns `max(0, v)`; wrong comment corrected with citations | `f722d2c` | `test_collect_kanban_breaker_trips_immediately_when_max_retries_is_zero` (RED: `assert 5 == 0`), `…_null_max_retries_uses_config_limit`, `…_task_max_retries_overrides_config_limit` |
| 4.8 | Mirror URLs gated by the ingress deny-list instead of upstream's serving allow-list | `_MIRROR_SERVING_STATES = {connected, connecting, retrying}` gates the mirror path only | `86ebb6d` | `test_listener_base_mirrors_require_a_serving_state[starting/paused/unknown]`, `…_absent_when_state_is_missing`, `…_synthesized_for_every_serving_state` (RED: 4 failures) |
| 4.9 | Reset-churn shrink warning unreachable when the table is wiped | Section gated on the flag as well as the total; compact marker added | `01fc6ec` | `test_detail_reset_churn_shrink_warning_survives_emptied_table`, `test_compact_reset_churn_shrink_marker`, `…_absent_when_healthy` |
| 4.10 | Loop-tick strikes shared across gateway lives | Counter keyed to the witness pid, reset on change and on a non-probing pass | `a912c8b` | `test_loop_tick_silence_strikes_are_keyed_to_the_witness_pid` (RED at the first post-restart refresh), `…_reset_when_the_gateway_stops_being_probed` |
| 4.11 | Bare credentials in routing free text survived redaction (reasons were not redacted at all) | `_redact_bare_credentials` (sk-/pk-/rk-, gh[pous]_, github_pat_, xox…-, JWT) applied to `display_name`, `resume_reason`, `auto_reset_reason` | `e5612dc` | `test_gateway_route_display_name_scrubs_bare_credential` (RED: secret survived), `…_scrubs_jwt_and_github_token`, `…_without_credentials_is_unchanged`, `test_gateway_route_reasons_scrub_bare_credentials` |
| 4.12 | Terminal scan materialised+sorted before its cap; truncation silent | `islice(bound+1)` before sorting; `TerminalSessionReadout.truncated`; panel says "at least N … (directory scan truncated)" | `b07b221` | `test_terminal_breadcrumb_scan_is_bounded_and_flags_truncation` (asserts ≤200 reads and the flag), `test_terminal_section_labels_a_truncated_scan_as_a_floor` |
| 4.13 | Dead-owner fire claims held the TTL; `last_fire_error` lifetime mis-described; dead "unacked" qualifier | Owner-pid release mirrored (same host, parseable); wording corrected to success-only clearing; compact warning carries the newest age; `(N unacked)` only when it differs | `a6df530` | `test_collect_cron_fire_claim_dead_owner_releases_before_the_ttl` (RED: `RUNNING is not ABANDONED_RUN`), `…_unverifiable_owner_stays_live`, `test_cron_compact_fire_forward_line_carries_the_newest_age`, `test_cron_compact_hides_unacked_when_it_cannot_differ` |
| 4.14 | Prune overdue window hardcoded; `min_interval_hours` ignored | Interval read from root `config.yaml` (positive numbers only, 24 h default); overdue at 2×; row states the effective value | `1568ed7` | `test_checkpoint_prune_interval_follows_the_configured_wrapper_cadence`, `…_ignores_unusable_config_values` (6 params) |

## 3. Hardening and copy

| # | Finding | Fix | Commit |
| --- | --- | --- | --- |
| 4.15 | Run-dir mtime presented as liveness | Renamed to "Dispatched" in model and panel | `3a9e9a9` |
| 4.16 | `DelegationLiveTask.goal` unredacted | Same redaction path as the tail | `e8399c7` |
| 4.17 | Capped lists presented as totals | `hygiene_total` counted in SQL; "showing the worst M"; route line adds "of N routed chats" only when capped | `1a3615d` |
| 4.18 | Empty/junk starts ledger reported as a recorded zero | `recorded` requires ≥1 parsed line; test renamed to its assertion | `7840c34` |
| 4.19 | Ops copy nits (duplicate "Tasks" row, double empty-state, "(shown)" scope) | All three fixed in one panel pass | `3a9e9a9` |
| 4.19 | Kanban `escape()` in a `Text` buffer injected literal backslashes | `sanitize_terminal_text` at both sites | `dfc0682` |
| 4.19 | `bool("false")` is `True` on six untrusted flag reads | Shared `_coerce_bool` (JSON true / SQLite 1 / "1|true|yes|on") | `ae28cdb` |

## 4. Documentation

| Gap | Fix | Commit |
| --- | --- | --- |
| README untouched for the whole batch | Panel sections for 1, 2, 5, 7, 11, 12, 13 and the feature table updated; panel 1's source list extended | `31c2c2d` |
| `gateway_loop_tick` credited a pin that never armed a witness; register preamble contradicted six decided rows; missing lifecycle fields; stale `gateway_starts_2m`; wrong citations; stale mixed-model register; rule-3 citation missing in the coordination reader | Rule file corrected, six rows marked **decided** with pins, citations fixed, register extended, docstring cited | `e53f5a1` |
| Enforcement test checked scope only | Now requires an upstream citation on every row and, *where a "pinned by" cell names tests*, a resolvable `test_*` in it (verified to fail on both mutations). It does not require a non-empty cell: divergence rows are exempt by design, which `gateway_lifecycle` and `curator` used. The consolidated review called that out; `gateway_lifecycle` now has a real behaviour pin (`test_gateway_launch_files_are_root_scoped_under_a_profile` reports the root sentinel's exit code, not the profile's) and its register row carries the **Decided.** marker its siblings had. | `e53f5a1`, `083a37a` |
| CHANGELOG | New `### Fixed` section covering all 22 user-visible fixes | `5fa7491` |

## 5. Test-suite strength

Mutation survivors from the review's audits were closed with discriminating
tests, each verified by re-running the mutation in a scratch clone:

- **Gateway** (`bfbf1ad`): single-byte witness read, 2 MiB exit-diag boundary, 24 h
  unclean window, 16-profile mirror roster (asserted as the literal 16), the
  exactly-at-cap storm boundary, companion logs stat-only. Mutations re-run: 6/6
  now fail (`recv(4096)`, 2 KiB warn, roster 1, `>= cap`, 48 h window, injected
  companion read).
- **Cross-area** (`fca154c`): lease expiry at `expires_at == now`, the 40-row lease
  cap with an exact `lease_total`, the routing `suspended` flag through the decode
  path, literal 64 KiB/256 KiB manifest caps, symlinked live root and symlinked
  receipts root refused.
- **Self-referential fixtures** (`fca154c`): the fire-claim TTL edge (300 s), the
  skill-window bound (20/25) and the mirror roster no longer import the constant
  they assert.
- **Loop-tick divergence pins** (`047ac4e`, `6e3674d`): a behavioural pin (root
  heartbeat, conflicting profile heartbeat) and a node-home pin; both verified to
  fail under the corresponding mutation, which previously survived the whole
  gateway + profiles set.
- **Read-only invariant** (`7f18a1b`): a `forensic_hermes_home` fixture composes
  every new artifact so the manifest guard now exercises the new readers; the
  split also exposed — and fixed — a whole-model equality assertion that only held
  while the home had no backups (it now freezes the clock).
- **Markup/ANSI matrix** (`64f21ed`): panels 1, 2, 11 and 12's new free-text fields
  now carry the injection payload (verified present in the detail renders).
- **Last survivors** (`5e18cf6`): small future-skew guard, strike reset on a
  non-probing pass, 24 h terminal window, 40-char display-name clip, corrupt-ledger
  contents never read, full notify last-good field set, orphan cap (5 shown, 8
  counted).

## 6. Verification

Measured at the head of the branch after the consolidated-review round (this is the
figure §1's summary and §9 quote; the earlier per-round counts in this file's history
are stale by construction):

```
ruff check .                          All checks passed!
ruff format --check .                 109 files already formatted
mypy hermesd                          Success: no issues found in 45 source files
python -m compileall -q hermesd       OK
pytest tests/ -q -W error::ResourceWarning --cov=hermesd   2843 passed, 2 skipped — 98.37% (gate 96%)
```

Live read-only run against the real `~/.hermes`:
`failed_sources: []`; witness `alive`/armed; 13 routes of 13; 2 CLI terminals;
**9 delegation run directories, 5 parsed into cards and none running** (the label
says `manifests`, not "live"); config backups present (1 good); catalog cache absent,
reported as unavailable rather than as a match. Panels 1, 2, 5, 7, 11, 12 and 13 all
render.

## 7. Deliberately not changed

- **Structural refactors** beyond the findings. Three of the four listed here have
  since been done in the consolidated-review round: `ConfigBackupGroup.kind` is a
  `StrEnum` answered in one panel pass, `_dashboard_client_status` returns a frozen
  dataclass, and the curator readers moved to `collect/curator.py`. Still open: the
  four repeated last-good prologues in `collector.py` and a full assertion tightening
  of panel 2's compact strings.
- **Env overrides** (`HERMES_GATEWAY_MAX_STARTS`, `HERMES_GATEWAY_START_WINDOW_S`)
  are not read: they belong to the gateway's environment, and the panel says the
  policy is "as recorded in config".
- **Real-socket tests** kept as-is (ephemeral ports, skip guard, ≤1 s timeout).
- README screenshots (versioned URLs) were not regenerated.
- The `4.19` compact-line deletions on panel 2 (`N cli tty`, `N compaction off`)
  now have render coverage through the hygiene/total tests, but the exact strings
  are still asserted loosely (`"60 hygiene cooldown(s)"`); tightening every
  compact string was left as follow-up.

---

## 8. Deep-dive re-validation (after the fixes landed)

### 8.1 Every fix is pinned — proven by backing it out again

A scratch clone was walked commit by commit: each fix's **implementation** was reverted
while its **tests were kept** (reverting the whole commit would remove the tests with the
fix and prove nothing), then the tests it touched were run. Where a whole-commit revert
conflicted with later work, a targeted mutation re-created the original bug instead.

| Method | Commits | Result |
| --- | --- | --- |
| clean `git revert` of the fix, tests kept | `01fc6ec`, `e88d83f`, `5c4d57c`, `001b66c`, `f722d2c`, `dfc0682`, `3ab2f77`, `e8399c7`, `3a9e9a9` | **RED-OK** — e.g. `5c4d57c` → `test_collect_kanban_notify_backlog_counts_only_this_tasks_unseen_events`; `3ab2f77` → 3 failures; `001b66c` → 4 failures |
| pre-fix files restored (`git checkout <sha>^`) | `a6df530`, `d936145`, `1568ed7`, `1a3615d` | **RED-OK** — 3, 15, 6 and 3 failures respectively |
| targeted mutation (revert conflicted) | `ae28cdb`, `b07b221`, `e5612dc`, `a912c8b`, `0a08eec`, `7840c34`, `86ebb6d` | **RED-OK** — 5, 1, 2, 1, 2, 3 and 4 failures |
| docs-only (`e53f5a1`) | — | enforcement test verified separately: blanking a citation or naming a nonexistent pin fails it |
| (correction) `92cb8ff` | — | listed here in an earlier revision of this report; it is the free-tier badge **feature** commit, not a reverted fix. Removed from the table. |
| test-only commits | — | each new pin verified against the mutation it was written for (witness byte, mirror roster, caps, boundaries, symlinks) |

No fix relies on a test that passes without it.

### 8.2 Defects the re-validation itself found (and fixed)

1. **`hygiene_total` was not in its last-good field list** (`d57b7f4`). Adding the field
   without adding it to `_HYGIENE_FIELDS` meant a good pass followed by a failed
   `gateway_hygiene` read restored the streak rows but reset the total to 0 — the panel
   rendered "0 hygiene cooldown(s)" above the rows it was still showing, i.e. the exact
   capped-list-as-total bug the field had just fixed, now on the failure path. New
   resilience test fails the read after a successful pass (red before the fix). The same
   audit confirmed the batch's other new fields (terminal `truncated`, catalog `usable`,
   storm window, prune interval) are restored wholesale by their specs.
2. **`_coerce_bool` rejected real numbers** (`90fe79a`): a JSON `1.0` or a REAL column —
   truthy under the `bool()` it replaced — read as False. Floats are accepted now; only
   the string forms stay restricted to upstream's spellings.
3. **The hygiene effect label still re-derived the ladder threshold** (`ead785c`), and the
   CHANGELOG bullet claimed otherwise. The label now follows
   `GatewayHygieneState.suspended`, and the three places describing the base say
   "default … (`hygiene_failure_cooldown_seconds`)" — upstream resolves it from config
   (`config_defaults.py:584`, `gateway/run.py:101-149`).
4. **The new config read's failure mode was unpinned** (`de41a8d`): a torn `config.yaml`
   now has a test proving the storm source keeps upstream's defaults and stays healthy
   instead of failing over an unrelated file.

### 8.3 Independent end-to-end re-checks (real code paths, no fixtures)

| Finding | Review's repro | Now |
| --- | --- | --- |
| 4.1 notify backlog | 21 for 1 unseen event | **1** |
| 4.2 live log tails | 12 opens for 8 displayed | **8 opens** |
| 4.3 nested-JSON payload | `failed_sources: [delegation_live, process_receipts]` | **`[]`** |
| 4.4 backup cap | `good`/`corrupt` dropped | **kinds `[corrupt, good, other ×6]`, truncated** |
| 4.5 corrupt catalog cache | "plugins match the catalog" | **`usable=False`, "catalog cache … is unreadable — update/removal checks unavailable"** |
| 4.19 strict bool | `bool("false") is True` | **`"false"→False`, `1→True`, `1.0→True`** |

Live run against the real `~/.hermes`: `failed_sources: []`; witness `alive`/armed;
storm `0/5 in 120 s`; routes `13/13`; terminals 2 (not truncated); 9 delegation run directories (5 parsed, 0 running);
prune interval 24 h, not overdue; catalog cache absent → `usable=False` (no match claim);
one `good` backup group. Panels 1, 2, 5, 7, 11, 12, 13 all render.

### 8.4 Residual items — the flag sweep has since landed

The truthy flag reads flagged here were fixed in a follow-up sweep (§9): every
machine-written payload read now goes through `_coerce_bool`, and the helper's
docstring states the boundary (strict for state payloads and DB rows, truthy for
human-authored settings, because upstream reads those truthily). What remains
truthy is deliberate: `config.yaml`/env policy values and SKILL.md frontmatter,
string/URL presence tests, and INTEGER-affinity SQLite columns.
- The two skips are environmental: the opt-in live contract test and a TUI test that needs
  a free pty.
- Panel-2 compact strings are asserted by substring, not in full.

---

## 9. Follow-up sweep: strict reads for every machine-written flag

**Out of the audited scope.** These commits go beyond the 25-item spec in
`reports/audit-2026-09-implementation-review.md` — that document names none of these
payloads (`needs_attention`, channel aliases, migration records, drain request,
`processes.json`, `no_agent`) — so they are recorded here as follow-up work rather
than as remediation of an audited item. The one exception is `skills/.usage.json`
`pinned`, which belongs to item 20, whose subject was already ✅.

After the deep dive, the same `"false"`-is-truthy hazard was swept across the whole
tree instead of just the batch's six sites. One commit per area, each with a test
that reads a stringified flag through the real reader:

| Commit | Payloads converted |
| --- | --- |
| `ccf16f1` | `gateway_state.json`: platform `needs_attention`, config-source `exists` |
| `726bd86` | `channel_aliases.json`: `stale` / `is_stale` / `expired` |
| `e28eaf7` | `skills/.usage.json`: `pinned` in the learned roll-up, the curator count and each window |
| `838db66` | `skills/.curator_state`: `paused`; migration manifest: `flag_was`, `service.system` |
| `96d05b1` | update receipt `restart_requested`; drain request `suppress_notification`; `active_sessions.json` `track_liveness`; `processes.json` `notify_on_complete` |
| `dd21837` | `cron/jobs.json` `no_agent`, plus the policy boundary in `_coerce_bool`'s docstring |

**Deliberately still truthy, with reasons recorded in code:**

- *Human-authored settings* — `config.yaml` policy keys (agent limits, cron
  `wrap_response`, kanban dispatch, MoA/streaming flags, `relay_only`,
  `multiplex_profiles`, `consolidate`, `redact_secrets`) and SKILL.md frontmatter
  `pinned`. Upstream reads these truthily when it decides what to do, so a strict read
  would describe a policy the agent does not apply.
- *Presence tests, not flags* — `stop_reason`, proxy/chronos URLs, env credentials,
  `shared_runtime_url`.
- *INTEGER-affinity columns* — this exception was **retired** in the consolidated
  review round: affinity converts a well-formed numeric spelling but leaves the text
  `'false'` as TEXT, and `bool('false')` is True. `projects.archived`, the sessions
  `archived` pair and `cron_executions.handoff_pending` now go through `_coerce_bool`
  too, so the rule is uniform: every flag read out of a payload or a row is strict.
- *Strict reads describe the record, not the agent's decision* — for `cron/jobs.json`
  `enabled`, upstream itself reads the value truthily (`cron/jobs.py:486`), so a
  corrupted string would still fire the job while hermesd renders it disabled. That is
  the honest reading of the file, and it is the only case where the two differ.

Verification after the sweep (superseded by §6): `2808 passed, 2 skipped`; coverage
98.29 %; ruff check/format, mypy and compileall clean; live snapshot unchanged
(`failed_sources: []`).
