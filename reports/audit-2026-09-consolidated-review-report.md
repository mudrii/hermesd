# Consolidated review — validation and remediation report

**Input:** the consolidated review of `audit-2026-09` (1 blocker, 8 major findings,
22 minors, 12 standards/smell items and 6 documentation inaccuracies), handed over
after `reports/audit-2026-09-remediation-report.md` had already been written.
**Branch:** `fix/audit-review-followups`, from `12cbac4` to the head named in §6.
**Method:** every claim was validated first by an independent read-only reviewer
(six of them, one per area) against the code *and* the upstream reference tree at
`~/.hermes/hermes-agent` (`2bc9edf23`), then fixed test-first with one commit per
finding. Verdicts are recorded per finding below, including the ones that turned out
to be wrong about the code they cited.

Reference tree note: `/Users/mudrii/src/hermes/hermes-agent` is a *newer* checkout
where the loop-tick witness code has been removed; every upstream citation in the
review resolves only against `~/.hermes/hermes-agent`. Reviewers were told which one
to use, and all of them confirmed the citations landed there.

---

## 1. Validation summary

| Finding | Verdict | What was wrong with the finding itself |
| --- | --- | --- |
| **B1** curator hygiene dropped by a populated run | CONFIRMED | Live home has 20 run dirs, not three |
| **M1** wedged escalates at 300 s, upstream at 90 s | CONFIRMED | Lines 385-417 → the body is 378-411 |
| **M2** breaker fires with `max_retries = 0` | CONFIRMED | The fix must be a failure floor, **not** "or status blocked" (sticky human blocks are skipped independently upstream) |
| **M3** expired leases flagged with no grace window | PARTIAL | The collect line was 488, not 520; and the proposed grace constant contradicts upstream, so it was rejected (see §4) |
| **M4** completed delegations labelled "live" | CONFIRMED | 8 of 9 manifests were completed, not all 9; the shown 5 were |
| **M5** shrink warning is a one-refresh blip | CONFIRMED | — |
| **M6** notify query unbounded | CONFIRMED | The cited fix (cap + aggregates) is bigger than described: five derived values, not one |
| **M7** cron `enabled` missed by the sweep | CONFIRMED | Upstream *reads* that flag truthily; the strict read is the repo's own policy for machine-written payloads, and the divergence is now documented |
| **M8** routing reasons unclipped | CONFIRMED | Line 573-576 → 584-590; panel 652-658 → 1017-1030 |
| Minor 1 TCP arming | **REFUTED** | Upstream writes `loop_tick_socket` *and* the port from the same server; no port-only payload exists. Only the comment was wrong |
| Minor 2 dangling routes over-fire | CONFIRMED | — |
| Minor 3 "never alerted" too eager | **REFUTED** | `alerted` means the ping reached the operator, so the label is exact; an age gate would hide a dead alert path (and breaks a pinned test) |
| Minor 4 backups section unreachable | CONFIRMED | Stronger than claimed: unreachable in both views, and `config_panel.py:70` is not the last section |
| Minor 5 free-tier predicate | CONFIRMED | Live home unaffected (no entry carries either key) |
| Minor 6 truncation label | CONFIRMED | Line 265/270 → 502/507 |
| Minor 7 cap mismatch | CONFIRMED | — |
| Minor 8 receipt tail sliced before redaction | CONFIRMED | Line 388 → 625 |
| Minor 9 platform counts unrendered | CONFIRMED | They *are* in the JSON snapshot; no human rendering |
| Minor 10 `process_notes` never read | PARTIAL | No spec text names it; upstream's value embeds commands and output tails, so surfacing it would break the counts-only policy. **No fix** |
| Minor 11 vocabulary widening comment-only | CONFIRMED | The complaint is the CHANGELOG sentence, not the code: `surface` is stored verbatim |
| Minor 12 NULL vs 0 collapsed | PARTIAL | Line 274 → 275; the earlier remediation's storage reasoning was right, its CLI example wrong |
| Minor 13 exit tag unredacted, lines run together | CONFIRMED | Line 829 → 861; the gateway panel is 1, not 12 |
| Minor 14 future stamps counted | CONFIRMED | — |
| Minor 15 mirror roster truncated silently | CONFIRMED | — |
| Minor 16 handover caveat missing | PARTIAL | The upstream mechanism is real, but neither "handover" nor "--replace" appears in the cited document |
| Minor 17 contract URL unredacted | PARTIAL | The cited panel line does not exist (file is 371 lines); the right seam is the collector, and upstream's contract validation makes a credentialed URL unreachable except by direct SQL |
| Minor 18 `_curator_threshold_days` misses OverflowError | CONFIRMED | Lines 520-523 → 920-930 |
| Minor 19 `_coerce_bool(nan)` is True | CONFIRMED | Real site is `common.py:239`, and `inf` is affected too, so the fix is `isfinite`, not `value == value` |
| Minor 20 RecursionError inconsistent | CONFIRMED | Breadcrumbs are `collector.py:2097`, not 853; `file_cache.py` has three sites, and it also re-parsed the file every refresh |
| Minor 21 INTEGER-affinity exception | PARTIAL | The exception holds for upstream writers at all three sites; retired anyway for uniformity (see §3) |
| Minor 22 hostname in the reader | CONFIRMED | — |
| Standards: `# type: ignore` at `gateway.py:445` | CONFIRMED | — |
| Standards: monkeypatched internals | CONFIRMED (6/6) | Two of the six are read-count properties that need an injected seam, not a fixture |
| Standards: dead code | PARTIAL | 13/14 dead ignores, not 16; `CHECKPOINT_PRUNE_OVERDUE_AFTER_SECONDS` has test readers, so it was replaced by a shared helper rather than deleted |
| Standards: stringly typed backup kind | CONFIRMED | Six traversals, not four |
| Standards: second kanban.db connection | CONFIRMED (measured) | Two WAL copies per refresh |
| Standards: `_count_rows` for a SUM | PARTIAL | The helper is a generic scalar read; only its docstring was wrong |
| Standards: defaulted positional total | **REFUTED** | `sessions.py:502` is a comprehension; the earlier half-restore bug was `_HYGIENE_FIELDS`, a different mechanism |
| Standards: `_json_object_capped` placement | CONFIRMED | — |
| Standards: curator logic in `operations.py` | CONFIRMED | 147 lines (146 non-blank) |
| Standards: `_StartStorm.model_fields()` | **REFUTED** | `_StartStorm` is a plain dataclass, not Pydantic; renamed anyway for clarity |
| Standards: duplicated hygiene line | CONFIRMED | — |
| Standards: docstring drift | PARTIAL | Three of four cites were stale; all four descriptions were wrong |
| Standards: `_dashboard_client_status` tuple | CONFIRMED (low) | — |
| D1 pass counts differ | CONFIRMED | The failure it observed in a full-suite run was an order-dependent flake, since fixed |
| D2 §4 pin claim + missing Decided marker | CONFIRMED | — |
| D3 `92cb8ff` listed as a reverted fix | CONFIRMED | It is the free-tier feature commit |
| D4 "9 live manifests" | CONFIRMED | — |
| D5 CHANGELOG contradictions | CONFIRMED | The fourth sub-claim as written was wrong (`git pinned` is a provenance token); the real three-vs-four omission is the curator's `unknown` state |
| D6 sweep outside the spec | CONFIRMED | — |

---

## 2. Fixes, one commit per finding

**Blocker**

| Commit | Issue | Fix |
| --- | --- | --- |
| `510c7e8` | `_collect_curator` overlaid the thresholds and the `skills/.usage.json` rollup onto the *empty* run shape, then returned the populated run built from a fresh `CuratorRun`, so every hygiene field fell back to its default as soon as a run report existed. Panel 13 showed no hygiene section at all on the live home (72 skills in the usage file) and a configured 7/9 threshold pair rendered as 14/30 | Both shapes apply one shared overlay dict via `model_copy(update=overlay)`, so a field cannot land on only one branch; pinned by a test that stages `run.json` + `.usage.json` + config overrides together |

**Majors**

| Commit | Issue | Fix |
| --- | --- | --- |
| `36c3b08` | `_loop_tick_verdict` kept a 90-300 s band that always answered `stale`, so an armed-and-silent witness could not escalate inside it (upstream's escalation band opens at its 90 s budget) and a witness-less legacy payload in that band read as a slow heartbeat | Band removed: past 90 s a silent armed witness walks the three-strike ladder to `wedged` (`stale` for the first strikes), no witness key is `legacy`, unprobeable/disarmed is `unknown`. The heartbeat-only fallback keeps its ten-write cutoff for a stopped gateway; the parametrised threshold test now pins 91 s and 300 s as the witness's call |
| `f7af73d` | `breaker_tripped` compared the raw failure counter against the limit, so a fresh task with `max_retries: 0` rendered `0/0 breaker tripped` in alert style | Floor the limit at one failure in the verdict (upstream increments before comparing), still reporting the stored `0`; the module comment that cited `--max-retries 0` as CLI behaviour was corrected (the CLI rejects anything below 1) |
| `3fd5cf9` | The compact lease row printed `1 expired` in warning style with nothing to separate it from the orphaned case | `1 expired (holder may revive)`, and the lease note states that a compression lock only blocks other compressions. **No grace window** — see §4 |
| `0261395` | The compact row called every delegation run directory "live": `9 live` on a home where every parsed manifest had completed | The total is labelled `manifests` (what it counts) and the running sum keeps `(shown)` |
| `1bd04e7` | The `conversation_generations` shrink warning compared against the previous count and then stored it, so it cleared on the next refresh | The remembered count is a high-water mark; the warning stands through partial recovery and clears only when the table returns to it. The test now runs four refreshes instead of two |
| `a968cb2` | The notify read selected every `kanban_notify_subs` row with two correlated `task_events` subqueries each and no `LIMIT` — the only unbounded read in the module | Worst-ten backlog list with `LIMIT 10`, and *every* total from its own aggregate: `COUNT(*)`, `GROUP BY platform`, a `SUM/MAX` over a derived unseen table, and `DISTINCT notifier_profile` for orphan detection. A traced-SQL test pins that no listing statement is unbounded while all totals stay exact over 40 watchers |
| `5eec010` | `cron/jobs.json` `enabled` was read truthily while its siblings in the same record were strict | `_coerce_bool`, keeping the deliberate null/absent fallback to the model default |
| `f561ff7` | `resume_reason` and `auto_reset_reason` were redacted but never clipped, though both render in the routes table and the snapshot | One clipped reader for all three route strings, applied *after* redaction so a credential cannot survive as a truncated prefix |

**Minors**

| Commit | Issue | Fix |
| --- | --- | --- |
| `0d3cb2b` | Dangling was decided against the filtered session listing, so a route to a hidden (still resumable) session was reported as pointing at nothing | The state.db readout carries the unfiltered id set from the same cached connection; routes resolve against it |
| `b9326cf` | The card's `task_count` came from the manifest's self-report, so a torn file could render `showing 8 of 3` | Derived from the entry list the card actually holds |
| `1c802c1` | A manifest over the 64 KiB parse cap was counted with no card and no explanation; an oversized-only home rendered nothing | The reader reports how many counted manifests produced no card, the field has its own last-good restore, and the section (and the compact line) render the count with "too large to read or unreadable" |
| `08e82ba` | `output[-400:]` was redacted after slicing, so a credential whose marker fell outside the cap kept 400 raw characters | Redact first, then bound |
| `842d54b` | `notify_platform_counts` had no consumer outside the JSON snapshot | Rendered as a `Notify Platforms` row in the kanban detail view |
| `38b688c` | `max_retries` collapsed NULL to 0, erasing the override/unset distinction the breaker logic relies on | `int \| None` carrying the raw column |
| `d46a61d` | The completion contract (a PR URL) reached the panel and the snapshot unredacted | Redacted at the collection seam, like every other URL reader; a plain `OWNER/REPO` passes through unchanged |
| `6dfb741` | `socket.gethostname()` was called inside the cron reader, so the dead-owner path could only be tested against the live machine | Injected through the collector like `pid_exists`/`clock`, and the test pins a fixed host |
| `d36f0b1` | `_coerce_bool` read `NaN`, `inf` and `-inf` as set (`value != 0`) | `isfinite(value) and value != 0`, mirroring `_coerce_float` |
| `dafbed0` | A nesting bomb failed the terminal-breadcrumb source and made `file_cache` re-parse the file on every refresh | `RecursionError` joins the suppressed/`load_errors` sets at both sites, so the bomb is cached as bad for its mtime |
| `aacc384` | A YAML `.inf` raised `OverflowError` out of the threshold cast and failed the whole curator source | Caught, as the docstring always claimed |
| `ac655fd` | The free-tier badge required `account_tier == "anonymous"` as well as the auth method; upstream's `is_guest_state` tests the method alone | Single condition, with the model comment and the README/CHANGELOG corrected |
| `db23afe` | Two comments described mechanics that do not exist (a POSIX/Windows witness split, a lease-surface vocabulary) | Both corrected; no code change was needed |
| `2876072` | The Config detail had no viewport, so its ~72 lines could not reach the corrupt-snapshot alert at 80x24 | Config joins the rendered-viewport panels, and the `[g/G]` footer hint follows the same `scrollable` flag as `[j/k]` instead of a panel list that can drift |
| `7747cef` | The unclean-exit flags read as a verdict, though a `gateway --replace` takeover that outlived its SIGTERM grace window leaves the same record | The caveat is printed beside them |
| `4315063` | The exit-diag tag was clipped but never redacted, and five liveness facts ran together on one line | Tag redacted after the display clip; each fact gets its own line; the value is labelled `last record` |
| `2cc2a9d` | A future-dated exit-diag record counted as an unclean exit in the last 24 h | `0 <= now - stamp <= day`, matching the starts reader |
| `3cb9c0c` | The mirror roster was sliced to 16 profiles with no flag, so the slice read as the whole roster | `mirror_urls_truncated` on the platform, rendered as its own warning line |
| `8e897d2` | `bool(row.get("archived"))` trusted INTEGER affinity, which leaves a text `'false'` as TEXT | `_coerce_bool` at all three reads; the affinity exception is retired |

**Standards and smells**

| Commit | Issue | Fix |
| --- | --- | --- |
| `dc6816b` | An unqualified `# type: ignore[arg-type]` caused by typing the probe address as `object` | `tuple[str, int] \| str`, ignore removed |
| `8eb3ece` | Dead `_ExitDiag.forensic_files`; an overdue constant with no production reader; an in-function cap its only caller had already applied; 14 unused `# type: ignore` in tests | Field deleted, the two overdue computations share `checkpoint_prune_overdue_after()`, the dead branch deleted, ignores removed (`mypy --warn-unused-ignores tests/` is clean) |
| `68f77e2` | `ConfigBackupGroup.kind` was a bare `str` ranked through a dict with a silent `99` fallback, and the panel re-traversed the list six times | `ConfigBackupKind` StrEnum, ranked with a total dict, one `_BackupIndex` traversal per render |
| `7ddd374` | `_json_object_capped` lived in `operations.py` and was imported by two sibling readers; the curator panel built the same label twice; three docstrings described something else | Helper and its cap moved to `collect/common.py`; one `_state_counts_label`; docstrings corrected |
| `2586e89` | Three kanban readers each snapshotted the WAL database — up to three full copies per refresh, the cost `040769e` had removed for state.db | One snapshot per database per source mtime, shared by all readers; each still runs its own SQL on its own connection, so a torn notify read cannot blank the board. The test counts real `snapshot_wal_database` calls |
| `6db1363` | `_dashboard_client_status` returned a positional tuple while every sibling readout is a dataclass | `_DashboardClientStatus` (frozen, slotted) |
| `5295159` | `_StartStorm.model_fields()` shadowed Pydantic's accessor name; `_count_rows` was documented as a row counter though its body is a generic scalar read | `as_update()`; docstring widened |
| `3cd9e47` | 147 lines of curator logic lived in `collect/operations.py` | `collect/curator.py`, pure move |
| `7d623e3` | Six tests patched module internals to simulate failures a real filesystem or database can produce | Real fixtures (symlinked catalog cache, unreadable backups directory, unreadable breadcrumbs/companions/parked ledger, dropped or hostile-schema kanban tables) plus two injected readers (`text_reader`, `live_log_tail`) for the two read-*count* properties a fixture cannot observe |
| `1c51c2b` | `gateway_lifecycle` was the only divergence row with neither a pin nor a **Decided.** marker; the CHANGELOG contradicted itself in four places; the remediation report's numbers disagreed with each other and with the branch | The launch-files profile test now distinguishes the root and profile lifecycle sentinels by exit code, the rules file records the pin and the marker, the CHANGELOG is corrected and extended, and the report's §1/§4/§6/§7/§9 are reconciled |

---

## 3. What the review got wrong, and what was done instead

- **Minor 1 (TCP arming) — refuted.** Upstream derives both `loop_tick_socket` and
  `loop_tick_tcp_port` from the same asyncio server, so the flag is true on Windows
  too and a port-only payload cannot exist; no Windows payload can render `legacy`.
  The misleading POSIX/Windows comment was fixed in `db23afe`.
- **Minor 3 ("never alerted") — refuted.** `alerted` means the failure ping reached
  the operator (`cron/incidents.py`), and rows are only promoted by a delivered
  notification, so the label is exact rather than age-dependent. The proposed gate
  would have hidden the signal it exists to show: a job failing in a loop with a dead
  alert path keeps refreshing `last_seen_at`, so it would have been relabelled out of
  "never alerted".
- **Minor 10 (`process_notes`) — no fix.** Upstream derives those lines from the same
  three lists hermesd already counts, and they embed the command and the output tail
  — exactly what the counts-only policy excludes.
- **Minor 12 (`max_retries`) — partially accepted.** NULL vs 0 is real at the row and
  dispatch layers but unreachable through the CLI (the parser rejects `< 1`) and
  behaviourally identical except on a blocked row. The model field was still changed
  to `int | None` so the snapshot stops erasing the distinction.
- **Minor 21 (INTEGER affinity) — the exception was retired.** Affinity converts a
  well-formed numeric spelling; the text `'false'` stays TEXT and `bool('false')` is
  True, so the "unreachable" claim only holds for upstream's own writers. Reading all
  three sites strictly costs nothing and makes the policy uniform: every flag from a
  payload or a row is strict.
- **M3 (lease grace) — the proposed fix was rejected.** Upstream revives an expired
  lease whose holder still matches and has no grace concept; the inclusive
  `expires_at <= now` boundary is pinned by an earlier deliberate fix
  (`test_lease_expiry_boundary_is_inclusive`). Only the copy changed.
- **Standards: defaulted positional total — refuted.** The cited line is a
  comprehension; the earlier half-restore bug was a missing entry in
  `_HYGIENE_FIELDS`, a different mechanism.
- **Standards: `_StartStorm.model_fields()` — refuted as a bug** (plain dataclass, no
  Pydantic base); renamed for clarity only.

---

## 4. Verification

```
ruff check .                                                All checks passed!
ruff format --check .                                       110 files already formatted
mypy hermesd                                                Success: no issues found in 45 source files
python -m compileall -q hermesd                             OK
pytest tests/ -q -W error::ResourceWarning --cov=hermesd     2842 passed, 2 skipped — 98.37 % (gate 96 %)
```

Two skips are environmental (a `pty.openpty` test and a TUI integration test), and
`pytest-randomly`/`pytest-timeout` are not installed here.

Live read-only validation against the real `~/.hermes` after the last fix:

- `failed_sources: []` across all 55 sources.
- Curator: panel 13 now renders **Skill Hygiene — Managed 72, Windows 59 active ·
  13 stale**, which is the exact state B1 hid. This is the end-to-end confirmation of
  the blocker fix.
- Gateway: `Witness: loop-tick witness answered`, `Starts: 2m 0/5 1h 0 last start 4h
  ago`, `Web client: … last frame 3s ago`, `Exit diagnostics: last record gateway.start
  4h ago unclean exits 24h: 0 ledger 241 KB`, `Event logs: …` — each on its own line,
  the ledger value labelled as a record.
- Operations: `9 manifests · 0 running (shown)` for the nine July run directories,
  and the unparsed-manifest count is 0 because all five parsed cards came from
  readable files.
- Platform ownership: `needs_attention` false for `api_server` and `telegram`;
  config sources `defaults` (absent) and `config` (present); 0 stale channel aliases;
  13 processes; curator not paused.

---

## 5. Deliberately not done

- **`ConfigBackupGroup` is typed, but panel 2's compact strings are still asserted by
  substring.** Tightening every compact string to a full-line assertion is a
  test-suite project of its own.
- **The four repeated last-good prologues in `collector.py`** were left alone: each
  has a slightly different "disappeared vs. unsafe path" message and condition, and
  collapsing them risks changing which failure wins.
- **Merging the two near-identical operations scan readers** was not attempted; it is
  a larger refactor with no finding behind it.
- **Reading gateway env overrides** (`HERMES_GATEWAY_MAX_STARTS`,
  `HERMES_GATEWAY_START_WINDOW_S`) is still not done: they belong to the gateway's own
  environment and the panel says the policy is "as recorded in config".
- **`cron_executions.handoff_pending`** was left truthy in §9's original list; it is now
  strict along with the other affinity columns, so that exception list is empty.
- **README screenshots** (versioned URLs) were not regenerated.
