# Implementation review — the 25-item audit batch on `audit-2026-09`

**Review window:** `cf19996..6655f85` (`git diff cf19996..HEAD`) — 8 998 insertions / 75 deletions.
Source: 19 files, +4 145 lines. Tests: 18 files, +4 685 lines, 192 new test functions.
**Method:** seven parallel adversarial reviews (one per panel area), three independent
mutation-test audits in `git clone --shared` copies, plus a hand-built synthetic
`~/.hermes` driven end-to-end through the real `Collector` (including a real
AF_UNIX loop-tick witness) and a per-panel `--snapshot-panel` / `--snapshot-format
json` render. No file in the working tree was modified by the review.

---

## 0. Bottom line

The batch is real and largely faithful: all 25 items exist end-to-end, every gate is
green, every reader is read-only, bounded and cited, and the ownership/divergence
discipline is mostly honoured. The branch is also genuinely unpushed
(`origin/audit-2026-09` is still at `cf19996`, 23 commits behind).

It is **not** ready to merge as-is. Five defects make the panel state something false —
an overcounting kanban backlog that fires the feature's own "watcher wedged" alarm
(4.1), an unbounded per-entry log tailing loop (4.2), a `RecursionError` escape that
fails sources on junk (4.3), a config-backup cap that can delete the corrupt/`good`
groups (4.4), and a plugin-catalog path that reports "plugins match the catalog" from an
unusable cache (4.5). Three documentation gaps matter for the repo's own rules: README
was never updated, one ownership row cites a pin that does not exist, and the
mixed-model register was not extended. Test quality is good on happy paths, clocks and
redaction but systematically weak on caps, exact boundaries and degenerate inputs —
three mutation audits scored 46–77 %, with the same shapes surviving everywhere.

---

## 1. Gates and headline numbers — measured, not taken on trust

| Claim | Measured | Verdict |
| --- | --- | --- |
| `ruff check`, `ruff format --check`, `mypy hermesd`, `compileall` | all clean | ✅ |
| "2691 passed / 1 skipped" | **2690 passed, 2 skipped** in 98 s | ⚠️ the second skip is `test_tui_integration.py` (`pty.openpty unavailable: out of pty devices`) — environment-gated, not a regression |
| "98 % coverage (gate 96 %)" | **98.17 %** total; gate enforced | ✅ |
| "~200 new tests" | **192** new top-level `def test_` (198 including nested); collected count 2 493 → 2 692 = **+199** | ✅ |
| "working tree clean, nothing pushed" | tree clean apart from this report; `origin/audit-2026-09` is at `cf19996`, local HEAD 23 commits ahead | ✅ |
| "6 feature merges + my gateway commit" | 5 merges in the window + the plain gateway commit `db30050` (23 commits total) | ⚠️ minor |
| "12 new sources added" | **14** new `source_name`s in the `_SourceSpec` table | ❌ undercounted |
| "4 new open-divergence rows" | **5** rows added to the open-divergence table | ❌ undercounted |
| "1 intentional-divergence entry" | 1 (`backups/config/`) | ✅ |
| "6 behaviour-pin tests in `test_collector_profiles.py`" | exactly 6, all named in the register | ✅ |
| ownership-doc cross-check green | `test_source_ownership_doc_matches_resolver_call_sites` passes | ✅ |
| read-only / no hermes-agent imports | window diff contains **no** write calls and **no** upstream imports; only socket use is the loop-tick probe (`connect` → `recv(1)` → `close`) | ✅ |

The 14 new source names: `gateway_loop_tick`, `gateway_restart_storm`,
`dashboard_client`, `gateway_exit_diag`, `gateway_routes`, `gateway_hygiene`,
`session_leases`, `generation_churn`, `terminal_sessions`, `kanban_notify`,
`delegation_live`, `process_receipts`, `config_backups`, `plugin_catalog`.

**Layer pattern:** every claimed item does have model fields, a reader, a
`_SourceSpec` (or an existing one for items 2 and 14–16), compact + detail
rendering, an ownership-table row and a CHANGELOG bullet. The one missing layer
is README (see §5).

---

## 2. Requirement verification, item by item

Legend: ✅ verified · ⚠️ verified with an overstated or partial clause · ❌ wrong.

| # | Item | Verdict | The gap that matters |
| --- | --- | --- | --- |
| 1 | Loop-tick socket probe | ⚠️ | Probe, plan, verdict, DI, 3-refresh strikes, ALIVE/LEGACY, panel line all real; my own live witness probe answered. But the strike counter is not keyed to the witness PID (`collector.py:1600-1607`), and the ownership row claims a pin that never touches a socket (§5). |
| 2 | Lifecycle OOM / unclean exit | ✅ | Strict `is True` reads match upstream, which writes only literal `true`. Ownership row 113 omits the two new fields. |
| 3 | Restart storm | ⚠️ | Windows, junk/future-line rejection, `> cap`, absent≠zero all correct. The cap/window are **hardcoded** 5/120 s while upstream reads `gateway.respawn_storm` (§4.6). |
| 4 | Exit-diagnostics ledger | ✅ | Tail cap, tag/age, 24 h count, 2 MiB warning, 3 stat-only companions, extras unparsed, ROOT divergence + real pin. |
| 5 | Dashboard client attachment | ⚠️ | mtime-only, missing = never, future clamp, chip + detail line, ROOT + pin. The 60 s "attached" window is hermesd's invention (upstream deliberately has no cutoff), so a connected-but-quiet tab reads as "no client attached". |
| 6 | Turn leases & compression locks | ✅ | Holder regex is byte-identical to upstream (`hermes_state.py:124`); dead-pid→orphaned, expired-but-live→"may revive", pid-less→unverifiable all pinned. |
| 7 | Hygiene failure streak | ⚠️ | Ladder, clamp, join on rotation-stable `session_key` correct; the `session_key` column is a `PRAGMA`-filtered SELECT, **not** a migration (older DBs degrade, verified). Panel copy hardcodes "300s base" though the base is config-driven. |
| 8 | Routing entry flags | ⚠️ | Decode, flags, 300 s unwind grace, dangling detection, watermark exclusion all correct. `display_name` is run through the key/value redactor but a *bare* credential in a chat name survives (§4.11). |
| 9 | Reset churn | ⚠️ | Counter and shrink flag correct, but the shrink warning is **unreachable in its own worst case** (§4.9). |
| 10 | Joinable chip | ✅ | Presence-only, URL never stored, vocabulary widened with citation. |
| 11 | Delegation process accounting | ✅ | Counts only; no id/command/tail reaches the model. (Pre-existing `goal`/`error_excerpt` on the same row still carry raw text — unchanged in this window.) |
| 12 | Delegation live manifests | ❌ | Scope/pin/caps/id-from-dir-name/log-name-derived-locally all correct, but the reader **tails every manifest entry before slicing to 8** (§4.2), and "directory mtime = liveness" is false. |
| 13 | Process completion receipts | ✅ | Field mapping matches upstream exactly (`output`, `id`, `started_at`, `exit_code`, `completion_reason`, `termination_source`); absent dir → honest empty state. |
| 14 | Cron fire claims / slots / fire error / preflight | ⚠️ | TTL and pending-slot semantics correct; the docstring claims to mirror `_claim_is_live` but omits upstream's dead-owner release, so a killed runner reads "running" for up to 300 s. |
| 15 | Cron status vocabulary & snapshots | ⚠️ | Vocabulary and `error_count` exclusion correct; "explicit model/provider pins beside snapshots" is **not implemented** — the pins are never rendered. |
| 16 | Cron incident ack | ✅ | "never alerted" + on-panel rationale, no invented age threshold. Overclaim: the row is minted `detected` before the ping leaves, so a seconds-old incident can render as a broken alert path. |
| 17 | Kanban notify backlog | ❌ | Everything except the maths. The backlog is the **global id gap**, not the count of this task's unseen events (§4.1) — reproduced 21 vs 1. |
| 18 | Kanban completion contract & breaker | ⚠️ | Contract column correct; `max_retries = 0` is discarded though upstream honours it (§4.7). |
| 19 | Config backup snapshots | ⚠️ | Parsing, kinds, caveats correct; the group cap can slice away the good/corrupt groups and print a false all-clear (§4.4). |
| 20 | Skill patch-reuse & curator thresholds | ✅ | Verified line-by-line against upstream: 3 states + separate pinned flag, `created_at` excluded, 14/30 defaults, `0`-keeps-config honoured. |
| 21 | Plugin catalog drift & removal | ❌ | Drift/removal logic correct, but an **unusable cache is reported as agreement** (§4.5) — the exact failure the CHANGELOG promises does not happen. |
| 22a | Terminal breadcrumbs | ⚠️ | PROFILE scope correct, 24 h window + cwd rendered; the 200-file cap is applied **after** materialising and sorting the directory (docstring says bounded). |
| 22b | Checkpoint prune overdue | ⚠️ | Fields, computed flag and caveat correct; the 48 h threshold is hardcoded and ignores `checkpoints.min_interval_hours` (upstream honours it) → false "overdue". |
| 23 | Multiplex listener mirrors | ⚠️ | URL shape and constants match `gateway/config.py:251-253` exactly; but the state gate is a deny-list where upstream uses an allow-list (§4.8). |
| 24 | Corrupt ledger marker | ✅ | ROOT, same ledger, mtime only, compact + detail. "Contents never read" is untested (a parser would pass). |
| 25 | Free-tier identity | ✅ | Independently falsified with a crafted `auth.json` (10 sentinels): no token/account value reached state or any of 28 panel renders. |

---

## 3. Defects, severity-ordered (all reproduced)

### Blockers — the panel states something false

**4.1 Kanban notify backlog overcounts by board-wide event churn (high).**
`hermesd/collect/kanban.py:326` computes `backlog = max(0, max_event_id - last_event_id)`
where `max_event_id` is this task's `MAX(task_events.id)` (`:349-355`).
`task_events.id` is a **global** autoincrement, and upstream's unseen set is
`WHERE task_id = ? AND id > cursor` (`hermes_cli/kanban_db_notify.py:310-335`) — a
count, not a gap. Every other task's interleaved events inflate the number.
Reproduced on an in-memory DB: task `t1` with events at ids 1 and 22, cursor 1 →
**hermesd 21, truth 1**; the panel prints `Notify Backlog 21 unseen (max 21)`
(`panels/kanban.py:175-180`) and a `Backlog 21` table row. The error is unbounded
and turns ordinary traffic into the panel's own "watcher is wedged" alarm — the one
signal the feature exists to give.
*Fix:* correlated `COUNT(*) FROM task_events e WHERE e.task_id = s.task_id AND e.id > s.last_event_id`;
keep `max_event_id` only as the "newest event" column.

**4.2 `delegation_live` tails every task log, then displays eight (high).**
`hermesd/collect/operations.py:480` builds `tasks = [_live_task_from_entry(...) for entry in task_entries]`
— each entry opens and tail-reads `task-N.log` — and only then applies `tasks[:_MAX_LIVE_TASKS]`.
`_MAX_LIVE_LOG_TAILS` (`:411`) is defined and never used. Measured: a 51 KB manifest
(1 500 entries, under the 64 KiB cap) with 1 500 task logs → 1 500 opens and 0.21 s per
pass on a warm cache; a review agent measured 3 916 entries → 2.38 s, ≈12 s for five
cards. A TUI that refreshes every few seconds cannot spend that on five cards.
*Fix:* slice to `_MAX_LIVE_TASKS` (or `_MAX_LIVE_LOG_TAILS`) **before** tailing, and
keep `running_task_count` from the cheap status field only.

**4.3 `_json_object_capped` lets `RecursionError` escape (high).**
`hermesd/collect/operations.py:314-330` suppresses only `JSONDecodeError`/`ValueError`.
Reproduced: a **6 011-byte** payload (`{"tasks": [[[…]]]}`) raises `RecursionError`,
which propagates to the per-source boundary, so `delegation_live` (and
`process_receipts`, and `gateway_routes` via the same helper) lands in
`health.failed_sources` on data that is merely junk. This contradicts the reader's own
docstring ("must never fail the source") and its test. The redaction module already
guards `RecursionError` for exactly this reason.
*Fix:* `contextlib.suppress(json.JSONDecodeError, ValueError, RecursionError)`.

**4.4 The config-backup group cap can delete the alert (high).**
`hermesd/collect/config.py:226-239` sorts groups by *reason* and then slices to
`_CONFIG_BACKUP_GROUP_LIMIT` (8), so with eight alphabetically earlier reasons the
`good` and `corrupt` groups are dropped. The panel then prints
`Last changed: no good copy` and `Corrupt snapshots: none`
(`panels/config_panel.py:279,284`) — a false all-clear on what is documented as a hard
alert — with only a low-key "Groups truncated" row in detail and nothing in compact.
*Fix:* rank kinds (`good`, `corrupt` first) before the cut, or keep the newest good and
the corrupt count outside the capped list.

**4.5 An unusable plugin-catalog cache reports agreement (high).**
`hermesd/collector.py:3499` → `plugins.parse_catalog_cache` returns `({}, [])` for any
non-conforming payload ("Any other shape is an empty cache, never an error"), while
`:3506` sets `plugin_catalog_cache_present=True`. The panel then prints
`catalog cache … plugins match the catalog` (`panels/overview.py:570`). Reproduced for
`{not json`, `{}` and `{"entries": 3}`. Absent-cache honesty works; corrupt-cache
honesty does not, though the CHANGELOG promises "never as up to date".
*Fix:* distinguish "file present but unusable" from "usable and empty" — e.g. have
`parse_catalog_cache` return `None` for a payload that is not the documented shape and
render "catalog cache unreadable — checks unavailable".

### Should-fix — divergence from upstream semantics or an unfaithful claim

**4.6 Restart-storm cap ignores `config.yaml` (medium).** `gateway.py:67-68` hardcodes
`5 / 120 s`; upstream's effective values come from `gateway.respawn_storm.{max_starts,
window_seconds}` (`hermes_cli/config_defaults.py:1978`, `hermes_cli/gateway.py:4666-4695`),
env-overridable, and hermesd **already parses that config** (`collector.py:1499`). With
`max_starts: 10` the panel cries "⚠ respawn backoff" at 6 starts; with `max_starts: 2` a
real storm renders clean.

**4.7 `max_retries = 0` is silently downgraded (medium).** `collect/kanban.py:199-205`
treats `<= 0` as unset; the comment at `:39-41` justifies it with "upstream stores NULL,
and coercion cannot tell 0 from NULL", which is **false** — upstream passes 0 through
(`kanban_db.py:1892-1894`) and keys on `is not None`
(`kanban_db_dispatch.py:1027-1031`). A task created with `--max-retries 0` is already
tripped upstream and shown as "1/2" here.

**4.8 Mirror URLs use a deny-list where upstream uses an allow-list (medium-low).**
`gateway.py:123` reuses `_INGRESS_SUPPRESSED_STATES = {fatal, disconnected, stopped}` at
`:232`; upstream's mirror rule requires `state in {connected, connecting, retrying}`
(`gateway/status.py:962-966`). Reproduced: `state="starting"` or a missing state still
yields `{'coding': 'https://x.test/p/coding/v1'}`. `paused` adapters keep `listener_base`
in the record, so an operator can be handed a callback URL upstream would refuse to
publish.

**4.9 The reset-churn shrink warning is unreachable in its worst case (medium).**
`panels/sessions.py:890` gates the whole section on `if coord.generation_chat_total:`, so
a *wiped* `conversation_generations` table — precisely the invariant break the flag
exists to report — renders nothing anywhere (no compact counterpart either).

**4.10 Loop-tick strikes are not keyed to the witness PID (low-medium).**
One `self._loop_tick_silent_strikes` (`collector.py:671`) survives a gateway restart: PID A
accumulates 2 strikes, dies, and PID B's first silent probe makes 3 → "wedged". The claim
is "silence on 3 consecutive refreshes" for *one* loop.

**4.11 A bare credential in `display_name` survives (low-medium).**
`collect/sessions.py:551` calls `_redact_text_fields`, which only rewrites `key = value`
shapes. Reproduced: `"Bob sk-live-abcdefghijklmnop"` passes through both redaction paths
unchanged and reaches the panel row (`panels/sessions.py:1022`) and the JSON snapshot. A
chat display name is remote-controlled, which is the case the redaction is there for.

**4.12 Terminal breadcrumb scan is not bounded (low-medium).**
`collector.py:2050-2052` materialises and sorts the whole directory before `[:200]`,
contradicting its docstring (`:2038-2041`) and the sibling implementation that
deliberately slices first (`:2389`). No `truncated` flag exists on
`TerminalSessionReadout`, so an over-cap directory silently *under*-counts while the panel
promises an upper bound.

**4.13 Cron liveness and lifetime wording (low-medium).** The fire-claim reader
(`collect/cron.py:792-798`) is TTL-only where upstream also releases a claim whose
same-host owner pid exited (`cron/jobs.py:2087-2098`) → "▶ running now" for a killed
runner. `models.py:943-945` says `last_fire_error` is cleared "on the next run outcome";
it is popped only on success (`cron/jobs.py:2228-2232`), and the compact warning has no age.

**4.14 Checkpoint prune thresholds (low-medium).** `collector.py:2795` / `models.py:2313-2314`
hardcode 24 h/48 h and ignore `checkpoints.min_interval_hours`
(`hermes_cli/cli.py:1133`, `gateway/run.py:3671`) → false "⚠ overdue" for a slower policy.

**4.15 "Dir Age" presented as liveness (low).** `models.py:1902-1903` calls the run-dir
mtime "the only liveness signal"; measured, it does not advance when `task-N.log` is
appended or the manifest is rewritten, so it is dispatch age.

**4.16 `DelegationLiveTask.goal` is unredacted (low).** `operations.py:505` clips but does
not redact, while the log tail beside it is redacted twice; it reaches
`--snapshot-format json`.

**4.17 Capped lists presented as totals (low).** `panels/sessions.py:160` prints
`len(coord.hygiene)` as the count (no `hygiene_total`), and `:1028` counts
waiting/dangling over ≤50 retained routes while the exact `route_total` is available —
both contradict the model's own rule that "a panel must never present a capped list as the
count".

**4.18 Restart-ledger present-but-unparseable (low).** `gateway.py:732-733` returns
`recorded=True` for a file with no parseable line, so the panel states
`Starts: 2m 0/5 1h 0` — the "absent ≠ zero" error one level deeper. The empty-file test is
named `..._empty_file_records_nothing` but asserts `recorded is True`.

**4.19 Smaller wording/UI nits.** Duplicate "Tasks" row on a truncated live card
(`panels/operations.py:716,724`); empty state prints "no receipts yet…" *and* "No
operations artifacts found" (`:224` vs `:269-287`); compact "N live · M running" mixes a
full count with a ≤5-card running sum (`:131-136`); `escape()` in a `Text` buffer injects
literal backslashes for an orphan profile name (`panels/kanban.py:61,76`); a
`_HYGIENE_SUSPENSION_STREAK` threshold duplicated in the panel
(`panels/sessions.py:962`).

---

## 4. Test quality

Three independent mutation audits were run in throwaway clones; the original tree was
never modified.

| Audit | Caught | Score |
| --- | --- | --- |
| Cross-area (30 mutants, 12 areas) | 23 | **76.7 %** |
| Gateway (15) | 9 | 60 % |
| Sessions (13) | 6 | 46 % |
| Operations (14) | 9 | 64 % |
| Cron/Kanban (13) | 9 | 69 % |
| Config/Skills/Plugins (7) | 5 | 71 % |

**The survivors are almost all the same three shapes** — caps, exact boundaries, and
degenerate inputs:

- **Cap enforcement:** `_MIRROR_PROFILE_LIMIT 16→1`, `_MAX_LIVE_TASKS` slice, 64 KiB
  manifest cap→256 KiB, `_COORDINATION_ROW_LIMIT 40→4`, orphan-list `[:5]`, 2 MiB exit-diag
  warning→2 KiB all survive. Fixtures exercise the cap with exactly one item, or assert
  `size_bytes > 0` instead of a magnitude.
- **Exact boundaries:** lease `expires_at == now`, `starts_2m > cap` vs `>=`, the 24 h
  unclean-exit window, the fire-claim TTL *at* 300 s, a rewound notify cursor
  (`last_event_id > MAX(id)` — the `max(0, …)` clamp is never tested).
- **Degenerate inputs:** deeply nested JSON (4.3), an empty/junk starts ledger, companion
  files read anyway, a symlinked `live/` root or receipts dir.

**Two boundary tests are self-referential** — the fixture and the expectation both
re-import the symbol under test, so the constant itself is unpinned:
`tests/test_collector_cron.py:2666-2679` builds the "exactly at the TTL" stamp with
`iso_ago(_FIRE_CLAIM_TTL_SECONDS, …)`, so `_FIRE_CLAIM_TTL_SECONDS 300→301` passes;
`tests/test_collector_skills.py:1200/1216` sizes the fixture with `_SKILL_WINDOW_LIMIT + 5`
and asserts `len(...) == _SKILL_WINDOW_LIMIT` — that mutation survives only because an
incidental hardcoded-age assertion at `:1221` happens to bite. Literal values (300.0, 20)
would fix both.

**Reader gaps masked by panel tests:** `tests/test_sessions_panel.py:876` hand-builds
`GatewayRouteState(suspended=True)`, so dropping `suspended=bool(...)` from the *reader*
(`collect/sessions.py:550`) survives — no test drives that flag through the decode path.

**Presence-only assertions:** `assert "1" in rendered`
(`test_sessions_panel.py:786-797` — deleting the entire "1 compaction off" badge still
passes), `assert "dangling" in rendered` (`:735-760` — satisfied by unrelated prose),
`test_operations_panel.py:411` / `test_sessions_panel.py:653` (heading only).

**Unpinned divergence decisions (my own mutations, run independently):**
- Switching `dashboard_client` from `shared_path` to `profile_path` — reversing the
  divergence the register calls pinned — leaves
  `test_gateway_launch_files_are_root_scoped_under_a_profile` **green** (its assertion is
  `dashboard_client_last_frame_age_seconds is not None`, true for either home). It is
  caught only by the *structural* ownership-doc test, which a doc edit would silence.
- Changing `_probed_loop_tick` to resolve the socket node under the **profile** home
  survives the entire `test_collector_gateway.py` + `test_collector_profiles.py` set
  (248 passed) — the `gateway_loop_tick` ROOT decision is behaviourally untested.

**Verification gaps in the safety net itself:**
- `tests/test_readonly_invariant.py` is unchanged in the window and its
  `populated_hermes_home` fixture was **not** extended with the new artifacts, so the
  manifest test runs the new readers over absent paths only (`gateway-starts.log`,
  `dashboard_clients.heartbeat`, `plugin-catalog.json`, `logs/process-results/`,
  `terminal-sessions/`, `backups/config/`, `logs/gateway-exit-diag.log` all have zero
  fixture hits). The read-only property still holds by inspection and has a dedicated
  probe test, but the strongest guard no longer exercises the code it guards.
- `tests/test_markup_safety.py` gained only four cron fields; the new panel-1/2/7/11/12/13
  fields (`catalog_removed_reason`, notify fields, delegation `goal`/`log_tail`, terminal
  `cwd`, backup stamps) are not covered by the injection/ANSI matrix. (The new operations
  surfaces do have their own hostile-text tests, and a hand-trace of the new render paths
  found no unescaped text.)
- The only host-coupled new tests are the two real-socket loop-tick probes
  (`tests/test_collector_gateway.py:2658-2712`): ephemeral 127.0.0.1 port, `_short_socket_dir()`
  skip guard, daemon thread with a 5 s join. Low flake risk; no new test reads the real
  `~/.hermes`, the real process table, or asserts against the real clock at a boundary.

**Genuinely strong tests** worth keeping as the model: the profile pins that plant
conflicting root/profile data and assert which one wins; the loop-tick ladder tests; the
redaction assertion against `model_dump(mode="json")`; injected clock/`pid_exists` instead
of sleeps or real processes; the tail-cap test with `log_tail_bytes=96`; and
`tests/test_collector_kanban.py`'s exact-value assertions.

---

## 5. Documentation alignment

**Complete:** every one of the 25 items has a CHANGELOG bullet (verified by keyword across
the file), with the caveats stated. The ownership table has all 14 new rows with scope and
upstream `file:line` citations; the intentional-divergence register has its one new entry
with a real pin; six new profile pins exist and are named correctly.

**Gaps:**

1. **README.md was not touched at all** in this window (`git diff cf19996..HEAD -- README.md`
   is empty), although it documents each panel down to column names and does cover the
   *previous* batch (`api_runs`, `hosted_rooms`, `desktop-plugins`, `state-snapshots`).
   Zero mentions of loop-tick, respawn storm, process receipts, notify subscriptions,
   completion contracts, `backups/config`, terminal breadcrumbs, turn leases, plugin
   catalog, free tier or breakers. `CONTRIBUTING.md` ("user-facing and release text stays
   documented") and its release checklist item 2 both require it.
2. **A false pin.** `source-ownership.md:109` credits
   `test_gateway_launch_files_are_root_scoped_under_a_profile` as `gateway_loop_tick`'s
   pin; that test creates no loop-tick node and never probes (`grep loop-tick
   tests/test_collector_profiles.py` = 0 hits). Per the doc's own rule 4, a divergence
   whose named pin does not pin it is a bug. Row 113 (`gateway_lifecycle`) also omits the
   two new fields.
3. **Mixed-model register is stale.** `OperationsState` (`:285`) still lists only
   `api_runs`/`hosted_rooms` as its cross-home pair; the new PROFILE
   `process_receipts`/`checkpoint_prune_*` beside ROOT `delegation_live_manifests`/
   `spawn_ledger_corrupt_*` are exactly what that table exists to record. The
   `GatewayState` row likewise enumerates four field-group constants where there are now
   more.
4. **Wrong upstream citations** in the notify reader: `kanban_db_notify.py:186-232` is
   `count_notify_subs`; the cursor/unseen logic is `:310-335`. `hermes_state_common.py:153`
   is cited as the `conversation_generations` writer (the increment is
   `hermes_state_messages.py:30-33`).
5. **The enforcement test does not check citations.** `_documented_scopes`
   (`tests/test_collector_profiles.py:1230-1245`) parses only the source_name and scope
   cells, so a row with an empty upstream-citation cell still passes. All 14 new rows do
   have real citations (verified by hand), and every `test_*` name cited in the doc
   resolves — but the doc's rule 3 ("cite the upstream `file.py:line`") is unenforced.
6. Summary counts in the hand-off narrative: 14 sources, not 12; 5 open-divergence rows,
   not 4; 5 merges in the window, not 6; the test totals are host-conditional (2 690/2 here).
7. **Register semantics are muddled for the new ROOT decisions.** The four gateway
   launch-file rows and `plugin_catalog`/`delegation_live` *do* carry explicit decisions
   and pins, yet they were filed under "Open divergences (not blessed)", whose preamble
   says "None of them is covered by an intentional-divergence decision". The four new
   gateway rows also name no pin in their own row (the pin lives only in the ownership
   table), unlike the plugin-catalog row. Either bless them explicitly or reword the
   preamble.
8. **Citation rule 3 is unevenly applied.** `_read_session_coordination_rows`
   (`hermesd/collect/sessions.py:356`) — the reader behind all four new `state.db`
   coordination sources — carries no upstream `file.py:line` citation in its docstring or
   the surrounding block; 21 of the 68 new functions have no citation nearby (12 have no
   docstring at all). Most of the rest are exemplary.
9. "Strict boolean checks" is only partly true: `bool(record.get("pinned"))`,
   `bool(entry.get("suspended"))`, `bool(entry.get("resume_pending"))`,
   `bool(j.get("preflight_alerted"))` still coerce, so a string `"false"` in a corrupted
   payload reads as `True`. The lifecycle and port readers do use strict `is True` /
   `isinstance(…, bool)` checks.

---

## 6. Code quality

The window is unusually disciplined: dependency injection for the clock, `pid_exists`,
the loop-tick probe and the DB factory; `@dataclass(frozen=True, slots=True)` for the
internal readouts; `or 0`/`or ""` NULL tolerance with no `.get(col, default)` on rows;
every reader bounded, symlink-checked and citation-annotated; error paths fail the source
and keep last-good. No write to `~/.hermes` and no upstream import anywhere in the diff.

Judgement-call smells (none blocking):

- **Speculative generality:** dead `_MAX_LIVE_LOG_TAILS` (`operations.py:411`), dead
  `now` parameter (`gateway.py:705`), dead `_CONFIG_BACKUP_IMPORT` in tests, unreachable
  malformed-sha guard (`plugins.py:433-436`), unreachable inner entry cap
  (`config.py:206-211`).
- **Duplicated code:** `_read_delegation_live_manifests` (`:417-466`) vs
  `_read_process_receipts` (`:544-587`) share the whole scan shape;
  `_read_checkpoint_prune_marker` (`:617-646`) and `_read_corrupt_ledger_marker`
  (`:649-672`) are near-twins; four identical last-good prologues in `collector.py`
  (`:1944-2027`); three find-by-kind loops in `config_panel.py:248-260,297-302`.
- **Primitive obsession:** `ConfigBackupGroup.kind` is a stringly-typed closed set
  (`python-patterns.md` wants `StrEnum`, and `PluginActivation` is the precedent);
  `getattr(theme, _LAST_STATUS_STYLES…)` (`panels/cron.py:404`) fails at render time.
- **Feature envy / duplicate read:** `_with_loop_tick` re-reads and re-parses the
  heartbeat the heartbeat source already owns (`collector.py:1591-1594`); `kanban.db` is
  opened a second time per refresh for the notify source (`collector.py:2723-2733`),
  duplicating WAL-snapshot work.
- **Inconsistent return shape:** `_dashboard_client_status` returns a tuple where every
  sibling reader returns a frozen dataclass.

---

## 7. Recommended order of work

**Before calling the batch done** (false statements reach the operator):
1. Fix the notify backlog to a correlated count (4.1) and pin it with a foreign-event fixture.
2. Slice before tailing in the live-manifest reader (4.2); delete `_MAX_LIVE_LOG_TAILS`.
3. Add `RecursionError` to `_json_object_capped` (4.3) and test a nested manifest.
4. Rank backup kinds before truncating, or keep good/corrupt outside the cap (4.4).
5. Distinguish "cache present but unusable" from "empty" for the plugin catalog (4.5).
6. Fix the two false-pin/README gaps (5.1, 5.2) — a one-line doc row plus the README
   sections for panels 1, 2, 5, 7, 11, 12, 13.

**Next** (upstream fidelity): 4.6 (respawn cap from `gateway.respawn_storm`), 4.7
(`max_retries = 0`), 4.8 (mirror allow-list), 4.9 (shrink gate), 4.10 (strikes keyed by
PID), 4.14 (`min_interval_hours`).

**Then** (hardening + tests): 4.11–4.13, 4.15–4.19; extend
`populated_hermes_home` so the read-only manifest invariant actually exercises the new
readers; extend `tests/test_markup_safety.py` to the six panels' new fields; make the
ownership test assert a non-empty citation cell and an existing pin for every divergence
row; add the missing tests — one per mutation survivor named in §4 — the highest-value
five being (a) a 20-profile mirror roster asserting `len == 16`, (b) a lease exactly at
`expires_at == now` plus a `LIMIT+5` row set asserting `len(rows) == 40 and lease_total == 45`,
(c) `suspended=True` driven through the reader, (d) `oversized` false at 1 MiB / true at
2 MiB+1 with a 5 s-old fire claim reading running, (e) a rewound notify cursor reading
backlog 0 — and replace the two symbol-re-importing tests with literal expectations.
