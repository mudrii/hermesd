# Source Ownership: which Hermes home each source is read from

Every on-disk source hermesd reads is resolved through exactly two methods on
`HermesPaths` (`hermesd/paths.py`). This file records **which of the two owns
which source**, and why. Read it before adding a reader; the ownership table at
the bottom is checked against the code by
`tests/test_collector_profiles.py::test_source_ownership_doc_matches_resolver_call_sites`,
so a new reader that is not in the table fails the suite.

The reason this file exists: hermes-agent's default is *profile*-scoped. Its
`hermes_constants.get_hermes_home()` returns the profile home whenever a profile
is active, and the root is the exception — always explicitly justified at the
call site (`get_default_hermes_root()`). hermesd inverts the default: it stays
root-only unless `--profile`/`HERMES_PROFILE` opts in, and it deliberately does
not follow the `active_profile` file. So "where does upstream put this?" and
"where does hermesd look for it?" are two different questions, and conflating
them silently produces a panel that reads the wrong profile's data — or no data
at all, with green health.

## Scope vocabulary

There are exactly three classes. Do not add a fourth.

| class | meaning |
| --- | --- |
| `ROOT` | `~/.hermes` — `HermesPaths.shared_path()`. Same directory whether or not a profile is selected. |
| `PROFILE` | `~/.hermes/profiles/<name>` when a profile is selected, `~/.hermes` otherwise — `HermesPaths.profile_path()`. |
| `PROCESS-ENV` | Not on disk at all: a value from hermesd's own process environment. |

There is deliberately **no `SHARED` class**. `HermesPaths.shared_home` is a hard
alias for `root_home` — `shared_path()` *is* the root resolver — so a `SHARED`
label would name a distinction the code does not have and would misdocument it.

`SourceScope` in `hermesd/models.py` carries `ROOT` and `PROFILE` only.
`PROCESS-ENV` has no model consumer yet, so it has no enum member; add one when a
model needs to distinguish a value that did not come from disk.

A row whose scope is `MIXED` is not a fourth class: it is one `source_name` that
resolves some of its paths `ROOT` and others `PROFILE`. Those rows are the ones
that need the mixed-model register below.

`PROFILE (derived)` marks a source that resolves no path of its own — it computes
from the session rows another source already read — and therefore inherits that
source's scope.

## Scope is re-derived per call, never cached

`profile_path()` calls `_validate_profile_home()` on **every** call. That is not
redundant work: it is what defeats a symlink swapped in *after* the `HermesPaths`
was constructed, which construction-time validation alone cannot see. Pinned by
`test_profiled_collector_rejects_profile_root_swapped_to_outside` and
`test_collect_profiles_preserves_last_good_when_profile_child_becomes_unsafe_symlink`.

Do not "optimize" a reader by hoisting a resolved path into `Collector.__init__`
or a module-level constant. Upstream does exactly that in places and then has to
re-resolve at call time anyway (see the `_CHECKPOINT_BASE_AT_IMPORT` /
`_IMPORT_STORE` / `_HOOKS_DIR_AT_IMPORT` dance in `tools/checkpoint_manager.py:34-42`,
`cron/jobs.py:114-133`, `gateway/hooks.py:26-37`), because a multiplexed gateway
serves every profile from one process. hermesd resolves per call and stays
correct by construction.

## Rule for new readers (normative)

1. Default to `profile_path()`. If hermes-agent resolves the source through
   `get_hermes_home()`, it is `PROFILE` — that is the overwhelming majority of
   sources (see the table: upstream is profile-scoped for `cron`, `logs`,
   `cache/`, `state/`, `processes.json`, `plugins/`, `hooks/`, `config.yaml`).
2. Use `shared_path()` only when one of these is true:
   - upstream explicitly anchors at `get_default_hermes_root()` (or an equivalent
     root derivation) *and says why* — e.g. `hermes_cli/kanban_db.py:382-390`
     ("Shared across profiles BY DESIGN"), `hermes_cli/process_identity.py:127-131`
     ("Machine-root ledger path");
   - the source is machine-global rather than per-home — e.g. the desktop app's
     `HERMES_HOME` *is* the root (`apps/desktop/electron/main.ts:826,837`), so
     `desktop.log` and `hermes-agent/` live at the root.
3. Cite the upstream `file.py:line` in the reader's docstring, not just in this
   file. The citation is what makes the choice reviewable two years later.
4. If you choose `shared_path()` for something upstream scopes per profile, add a
   pinning test to `tests/test_collector_profiles.py` that names the intent, and
   add the row to the intentional-divergence register below. A divergence with no
   test and no register entry is a bug, not a decision.
5. Add the row to the ownership table with its `source_name`. The test fails
   until you do.

## Ownership table

Keyed by the `source_name` string from the `_SourceSpec` table in
`collector.py::_build_dashboard_state` — that is exactly what appears in
`health.failed_sources`, so an operator can grep a health failure straight to its
owner here. "resolver" is what the scope class means in code: `ROOT` →
`shared_path`, `PROFILE` → `profile_path`, `MIXED` → both.

"verdict" compares hermesd's resolver with upstream's, per path:
`agrees` = same home; `diverges` = different home; `ambiguous` = upstream has
both anchors, or no upstream writer was found. A divergence is **not** an
endorsement: only the rows in the intentional-divergence register are decided.
Everything else that diverges is listed under open divergences.

Upstream paths are relative to `/Users/mudrii/.hermes/hermes-agent/`.

| source_name | scope | DashboardState field(s) | on-disk path(s) | resolver | upstream resolver (file:line) | verdict | pinned by |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `sessions` | PROFILE | (raw session rows; no field of its own) | `state.db` | `profile_path` | `get_hermes_home()/"state.db"` — `hermes_state.py:160,178` | agrees | `test_profiled_collector_reads_profile_scoped_runtime_data` |
| `session_models` | MIXED | `sessions` | `state.db` (profile); `context_length_cache.yaml` (root) | both | `hermes_state.py:160,178`; `get_hermes_home()/"context_length_cache.yaml"` — `agent/model_metadata.py:1041-1044` | diverges (context cache) | UNPINNED |
| `tools_index` | MIXED | `available_tools`, `available_tool_names` | `cache/banner_snapshot.json` (root); `sessions/`, `sessions/session_*.json` (profile) | both | `hermes_cli/banner.py:625-626`; `hermes_cli/status.py:295` | diverges (banner snapshot) | `test_profiled_collector_reads_profile_scoped_runtime_data` (sessions half only) |
| `toolset_availability` | ROOT | `toolset_availability` | `cache/banner_snapshot.json` | `shared_path` | `get_hermes_home()/"cache"/"banner_snapshot.json"` — `hermes_cli/banner.py:625-626` | diverges | UNPINNED |
| `gateway` | ROOT | `gateway` | `gateway_state.json`, `gateway.pid`, `.drain_request.json`, `.update_check`, `config.yaml`, `hermes-agent/pyproject.toml` | `shared_path` | `gateway/status.py:157-158,165-166` (process home); root-anchored readers `hermes_cli/gateway_multiplex_served.py:23,36,54` + `gateway/status.py:927` (#97120); `gateway/drain_control.py:29,61`; `hermes_cli/banner.py:266`; `hermes_constants.py:1132-1135`; `apps/desktop/electron/main.ts:837`; mirror constants `gateway/config.py:251-253` and mirror synthesis `gateway/status.py:951-974` | ambiguous (`gateway.pid`/`gateway_state.json`); diverges (`.drain_request.json`, `.update_check`) | `test_profiled_collector_reads_root_scoped_cron_kanban_and_gateway` (behaviour pin) |
| `gateway_heartbeat` | ROOT | `gateway.heartbeat_age_seconds`, `gateway.loop_health` | `state/gateway.heartbeat` | `shared_path` | `<HERMES_HOME>/state/gateway.heartbeat` — `gateway/shutdown_watchdog.py:27,44` | diverges | UNPINNED |
| `gateway_loop_tick` | ROOT | `gateway.loop_health`, `gateway.loop_tick_armed` | `state/gateway.heartbeat` (reread), witness node `state/gateway.loop-tick.<pid>.sock` (POSIX) / 127.0.0.1:`loop_tick_tcp_port` (Windows) | `shared_path` | `<HERMES_HOME>/state/gateway.loop-tick.<pid>.sock` — `gateway/shutdown_watchdog.py:169`; probe client `hermes_cli/gateway.py:363-376`, verdict classify `:425-497` | diverges | `test_loop_tick_witness_is_probed_from_the_root_home_under_a_profile`, `test_probed_loop_tick_resolves_the_node_under_the_root_home` (behaviour pins) |
| `gateway_restart_storm` | ROOT | `gateway.gateway_starts_recorded`, `gateway.gateway_starts_window`, `gateway.gateway_starts_1h`, `gateway.restart_storm_cap`, `gateway.restart_storm_window_seconds`, `gateway.seconds_since_last_gateway_start`, `gateway.in_respawn_backoff` | `gateway-starts.log` | `shared_path` | `get_hermes_home()/"gateway-starts.log"` — `gateway/status.py:57-83` | diverges | `test_gateway_launch_files_are_root_scoped_under_a_profile` (behaviour pin) |
| `dashboard_client` | ROOT | `gateway.dashboard_client_attached`, `gateway.dashboard_client_last_frame_age_seconds` | `state/dashboard_clients.heartbeat` | `shared_path` | `<HERMES_HOME>/state/dashboard_clients.heartbeat` — `gateway/scale_to_zero.py:43,97-111` | diverges | `test_gateway_launch_files_are_root_scoped_under_a_profile` (behaviour pin) |
| `gateway_exit_diag` | ROOT | `gateway.exit_diag_*`, `gateway.forensic_files` | `logs/gateway-exit-diag.log` (tail-read); stat-only: `logs/gateway-shutdown-diag.log`, `logs/gateway_faulthandler.log`, `logs/launchd-reload.log` | `shared_path` | `_exit_diag` writes `get_hermes_home()/"logs"/"gateway-exit-diag.log"` — `hermes_cli/gateway.py:4643-4665`; `_append_exit_diag` `gateway/lifecycle_ledger.py:96-104` | diverges | `test_gateway_launch_files_are_root_scoped_under_a_profile` (behaviour pin) |
| `gateway_lifecycle` | ROOT | `gateway.lifecycle_phase`, `gateway.last_exit_code`, `gateway.last_exit_reason`, `gateway.unclean_previous_exit`, `gateway.prior_unclean_exit`, `gateway.prior_suspected_oom` | `state/gateway.lifecycle.json` | `shared_path` | `gateway/lifecycle_ledger.py:27-41` (`HERMES_HOME` env, else `get_hermes_home()`) | diverges | `test_gateway_launch_files_are_root_scoped_under_a_profile` (behaviour pin) |
| `update_receipt` | ROOT | `gateway.last_update_*`, `gateway.runtime_code_skew*` | `logs/update_receipts/latest.json` | `shared_path` | `hermes_cli/update_receipt.py:120-122` | diverges | UNPINNED |
| `gateway_ledgers` | PROFILE | `gateway.gateway_incarnation_count`, `gateway.gateway_restarts_24h`, `gateway.current_incarnation_uptime_seconds`, `gateway.pending_delivery_count`, `gateway.failed_delivery_count`, `gateway.pending_deliveries` | `state.db` (`gateway_heartbeats`, `delivery_obligations`) | `profile_path` | `hermes_state.py:160,178` | agrees | UNPINNED |
| `migration` | ROOT | `migration` | `gateway_migration.json`, `config.yaml` | `shared_path` | `<default home>/gateway_migration.json` — `hermes_cli/gateway_migrate.py:37` (`MANIFEST_NAME`) + `:741-742` (`_manifest_path`), where the home is `_default_home()` = `get_default_hermes_root()` at `:212-214`, never a secondary's; `_read_multiplex_flag` (`:335-340`) → `explicit_multiplex_flag` reads `default_home/"config.yaml"` — `hermes_cli/gateway_multiplex_mode.py:49-72` | agrees (`gateway_migration.json`); diverges — **intentional** (`config.yaml`, same decision as `config`) | `test_profiled_collector_reads_the_root_migration_manifest`, `test_profiled_collector_ignores_a_profile_local_migration_manifest` |
| `tokens_today` | PROFILE (derived) | `tokens_today` | (session rows) | none | `hermes_state.py:160,178` | agrees | UNPINNED |
| `tokens_total` | PROFILE (derived) | `tokens_total` | (session rows) | none | `hermes_state.py:160,178` | agrees | UNPINNED |
| `token_analytics` | PROFILE (derived) | `token_analytics` | (session rows) | none | `hermes_state.py:160,178` | agrees | UNPINNED |
| `tool_stats` | PROFILE (derived) | `tool_stats` | (session rows) | none | `hermes_state.py:160,178` | agrees | UNPINNED |
| `tool_call_total` | PROFILE (derived) | `total_tool_calls` | (session rows) | none | `hermes_state.py:160,178` | agrees | UNPINNED |
| `model_usage` | PROFILE (derived) | `token_analytics.usage_source`, `token_analytics.model_usage_*` | (session rows: `session_model_usage`) | none | `hermes_state.py:160,178` | agrees | UNPINNED |
| `background_processes` | ROOT | `background_processes` | `spawn-ledger.json`, `processes.json` | `shared_path` | `get_default_hermes_root()/LEDGER_FILENAME` — `hermes_cli/process_identity.py:27,127-131` ("Machine-root ledger path"); `get_hermes_home()/"processes.json"` — `tools/process_registry.py:41,45-50` | agrees (`spawn-ledger.json`); diverges (`processes.json`) | UNPINNED |
| `checkpoints` | PROFILE | `checkpoints` | `checkpoints/` | `profile_path` | `tools/checkpoint_manager.py:33,37-42` | agrees | `test_profiled_collector_does_not_read_root_scoped_profile_sources` |
| `config` | ROOT | `config` | `config.yaml` | `shared_path` | `get_hermes_home()/"config.yaml"` — `hermes_constants.py:1132-1135` (`get_config_path()`) | diverges — **intentional, see register** | `test_profiled_collector_keeps_shared_root_config_and_auth` |
| `config_backups` | ROOT | `config.config_backups_present`, `config.config_backup_groups`, `config.config_backup_groups_truncated` | `backups/config/config.yaml.<reason>.<YYYYMMDD-HHMMSS>` | `shared_path` | `backups_dir(config_path)` = `config_path.parent/"backups"/"config"` — `hermes_cli/config_backups.py:29-30,45-69`, with `config_path` = `get_config_path()` — `hermes_constants.py:1132-1135` | diverges — **intentional, see register** | `test_profiled_collector_keeps_config_backups_on_the_shared_root` |
| `cron` | ROOT | `cron` | `cron/jobs.json`, `cron/.tick.lock`, `cron/output/`, `cron/ticker_heartbeat`, `cron/ticker_last_success`, `cron/ticker_last_error`, `cron/catch_up_occurrences`, `cron/suggestions.json`, `channel_directory.json`, `config.yaml` | `shared_path` | `cron/jobs.py:60-72,96` + `_current_cron_store()` `cron/jobs.py:114-133`; `cron/scheduler.py:1017-1032`; `cron/suggestions.py:31,47-48` — all `get_hermes_home()`, "Cron is per-profile by design (#4707) … Do NOT change this to the default root" | diverges | `test_profiled_collector_reads_root_scoped_cron_kanban_and_gateway` (behaviour pin), `test_cron_catch_up_markers_are_read_from_the_root_store_under_a_profile` (marker pin) |
| `cron_executions` | ROOT | `cron_executions` | `cron/executions.db` | `shared_path` | `cron/executions.py:37`; `cron/incidents.py:52` | diverges | UNPINNED |
| `channels` | ROOT | `channels` | `channel_directory.json`, `channel_aliases.json` | `shared_path` | `gateway/channel_directory.py:40-45` | diverges | UNPINNED |
| `kanban` | ROOT | `kanban` | `kanban.db`, `kanban/boards/<slug>/kanban.db`, `kanban/current`, `config.yaml` | `shared_path` | `kanban_home()` = `get_default_hermes_root()` — `hermes_cli/kanban_db.py:382-401`, "Shared across profiles BY DESIGN: resolving through the active profile's HERMES_HOME would fork the board per profile and break the dispatcher/worker handoff" | agrees | `test_profiled_collector_reads_root_scoped_cron_kanban_and_gateway` |
| `kanban_notify` | ROOT | `kanban` (`notify_sub_count`, `notify_platform_counts`, `notify_backlog_total`, `notify_max_backlog`, `notify_backlog_subs`, `notify_orphan_profile_count`, `notify_orphan_profiles`) | `kanban.db` (`kanban_notify_subs`, `task_events`), `profiles/` | `shared_path` | `kanban_home()` = `get_default_hermes_root()` — `hermes_cli/kanban_db.py:382-401` (same root store as `kanban`, "Shared across profiles BY DESIGN"); subs writer `hermes_cli/kanban_db_notify.py:78-130`; profile names `hermes_cli/profiles.py:1368-1382` | agrees | `test_profiled_collector_reads_root_scoped_cron_kanban_and_gateway` |
| `operations` | MIXED | `operations` | profile: `projects.db`, `verification_evidence.db`, `state.db`, `checkpoints/.last_prune`; root: `desktop-build-stamp.json`, `web-ui-build-stamp.json`, `response_store.db`, `moa-traces/`, `cache/delegation/live/`, `spawn-ledger.json.corrupt`, `kanban.db`, `kanban/boards/*/kanban.db`, `config.yaml` | both | `hermes_cli/projects_db.py:23-25`; `agent/verification_evidence.py:111`; `hermes_state.py:160`; `hermes_cli/main_desktop.py:42-45`; `hermes_cli/main_web_build.py:187-190`; `gateway/platforms/api_server.py:688`; `agent/moa_trace.py:25-38`; `tools/delegation_live_log.py:40-43`; `hermes_cli/kanban_db.py:382-401`; `tools/checkpoint_manager.py:37-42,1094-1130`; `hermes_cli/process_identity.py:128-137,160-171` | agrees (`projects.db`, `verification_evidence.db`, `state.db`, `kanban*`, `checkpoints/.last_prune`, `spawn-ledger.json.corrupt`); diverges (`response_store.db`, `moa-traces/`, `cache/delegation/live/`, both build stamps) | `test_profiled_operations_readers_confine_to_selected_profile_home`, `test_root_mode_operations_confinement_is_unchanged`, `test_profiled_collector_rejects_cross_profile_symlinked_projects_db` |
| `delegation_live` | ROOT | `operations.delegation_live_manifests`, `operations.delegation_live_manifest_count` | `cache/delegation/live/<id>/manifest.json`, `cache/delegation/live/<id>/task-*.log` (tail) | `shared_path` | `get_hermes_dir("cache/delegation", "delegation_cache")/"live"` — `tools/delegation_live_log.py:40-43` ("profile-safe, never ~/.hermes"); manifest written at `_write_manifest` `:255-267`, statuses amended at `:270-286` | diverges (same divergence as the `cache/delegation/live/` path the `operations` row already carries; kept on the root resolver so the whole delegation cluster reads one home) | `test_delegation_live_is_root_scoped_under_a_profile` (behaviour pin) |
| `process_receipts` | PROFILE | `operations.process_receipts` | `logs/process-results/proc_*.json` | `profile_path` | `get_hermes_home()/"logs"/"process-results"` — `tools/process_registry_results.py:30,58`; retention `:19-20,28-45` | agrees | `test_process_receipts_are_profile_scoped_under_a_profile` |
| `state_snapshots` | ROOT | `operations.snapshot_count`, `operations.snapshot_total_bytes`, `operations.newest_snapshot_age_seconds` | `state-snapshots/` | `shared_path` | `home/"state-snapshots"` where `home = get_hermes_home()` — `hermes_cli/backup.py:35,1088-1090`; "each lands under its OWN `<home>/state-snapshots/`" `hermes_cli/backup.py:1380` | diverges | UNPINNED |
| `blocked_scripts` | ROOT | `operations.blocked_script_count`, `operations.newest_blocked_script_age_seconds`, `operations.blocked_script_names` | `cache/blocked-scripts/` | `shared_path` | `tools/approval_floors.py:63-64` | diverges | UNPINNED |
| `db_recovery` | PROFILE | `operations.db_recovery` | siblings of `state.db`: `state.db.repair-attempts.json`, `state.db.malformed-backup-*` (plus `-wal`/`-shm`/`-journal` sidecars), `state.db.backup-staging-*`, `state.db.retired-wal-*/` (incl. `manifest.json`), `state.db.repair.lock`, `state.db.auto-maintenance.lock` | `profile_path` | the repaired database is `get_hermes_home()/"state.db"` — `hermes_state.py:160,178`, repair invoked at `:535`; ledger `_repair_ledger_path` `hermes_state_repair.py:317-318`; forensic backups `_backup_db_file` `:481-513`; retired-WAL generations `hermes_state_dbfile.py:228,334-425`; locks `_open_lock_file` `hermes_state_repair.py:176-229` | agrees | `test_profiled_db_recovery_reads_the_selected_profile_home`, `test_profiled_db_recovery_refuses_a_cross_profile_symlink` |
| `hosted_rooms` | ROOT | `operations.hosted_rooms` | `shared-state.db` (**not** `state.db`) | `shared_path` | `(home.parent.parent if home.parent.name == "profiles" else home)/"shared-state.db"` — `gateway/hosted_rooms.py:398-414` (`default_db_path`), whose docstring is explicit: "Profile gateways … resolve to the shared ROOT `shared-state.db` instead of the master `state.db`", because pointing them at the session store makes every profile process a long-lived writer on it; upstream pins the choice with `tests/gateway/test_hosted_rooms.py:1344-1364` (`test_default_db_path_never_names_the_master_session_store`) | agrees | `test_hosted_rooms_is_root_scoped_under_a_profile`, `test_hosted_rooms_reader_targets_shared_state_db_not_the_master_session_store`, `test_hosted_rooms_reader_issues_no_query_against_the_master_session_store` |
| `api_runs` | PROFILE | `operations.api_runs` | `runs_idempotency.db` (plus its `-wal`/`-shm` sidecars, read through a temp snapshot) | `profile_path` | `get_hermes_home()/"runs_idempotency.db"` — `gateway/platforms/api_server_run_idempotency.py:67`; permissions tightened to 0600 including sidecars at `:116-124` | agrees | `test_api_runs_is_profile_scoped_under_a_profile`, `test_api_runs_reads_the_root_store_when_no_profile_is_selected` |
| `skills` | MIXED | `skills_memory` | profile: `skills/`, `memories/`; root: `auth.json`, `BOOT.md`, `hooks/`, `plugins/` (incl. `plugins/.install-metadata.json` and each plugin's `.hermes-catalog.json`), `config.yaml` | both | `hermes_constants.py:1137-1140` (`get_skills_dir()`); `tools/memory_tool.py:40`; `hermes_cli/auth.py:471-472` (profile) + `hermes_cli/auth.py:484-493` (root read-only fallback); `gateway/hooks.py:25,29-37`; `plugins/plugin_loader.py:28-34`; `hermes_cli/plugins_cmd.py:425-426`; `hermes_cli/plugins_cmd_catalog.py:24`; `hermes_constants.py:1132-1135` | agrees (`skills/`, `memories/`); diverges (`hooks/`, `plugins/` and both plugin sidecars); diverges — **intentional** (`auth.json`); ambiguous (`BOOT.md`) | `test_profiled_collector_reads_profile_scoped_skills`, `test_profiled_collector_keeps_shared_root_config_and_auth` |
| `plugin_catalog` | ROOT | `skills_memory.plugins[].catalog_update_available/catalog_removed/catalog_removed_reason`, `skills_memory.plugin_catalog_cache_*` | `cache/plugin-catalog.json` | `shared_path` | `get_hermes_home()/"cache"/"plugin-catalog.json"` — `hermes_cli/plugin_catalog.py:216-218` | diverges | `test_plugin_catalog_cache_is_read_from_the_shared_root_under_a_profile` |
| `desktop_plugins` | ROOT | `skills_memory.desktop_plugins`, `skills_memory.desktop_plugin_scan_truncated` | `desktop-plugins/*/plugin.js` (presence only; JavaScript is never read) | `shared_path` | app-level `<HERMES_HOME>/desktop-plugins`, explicitly never profile-scoped — `apps/desktop/electron/desktop-plugins-root.ts:1-38`; regular `plugin.js` entry — `apps/desktop/src/contrib/runtime-loader.ts:219-251` | agrees | `test_desktop_inventory_is_root_scoped_when_a_profile_is_selected` |
| `mcp_cache` | ROOT | `mcp_cache` | `cache/mcp_schema_cache.json`, `config.yaml` | `shared_path` | `tools/mcp_schema_cache.py:18,22-24` | diverges | UNPINNED |
| `skills_prompt` | ROOT | `skills_prompt` | `.skills_prompt_snapshot.json` | `shared_path` | `agent/prompt_builder.py:1077-1078` | diverges | UNPINNED |
| `memory` | MIXED | `memory` | profile: `memories/`, `SOUL.md`, `skills/`, `skills/.usage.json`; root: `config.yaml` | both | `tools/memory_tool.py:40`; `hermes_cli/config.py:600`; `hermes_constants.py:1137-1140`; `tools/skill_usage.py:50`; `hermes_constants.py:1132-1135` | agrees (profile paths); diverges — **intentional** (`config.yaml`) | `test_profiled_collector_does_not_read_root_scoped_profile_sources` |
| `profiles` | ROOT | `profiles` | `profiles/*/` | `shared_path` | `hermes_constants.py:165-181`; `hermes_cli/profiles.py:29` | agrees | `test_collect_profiles_lists_profile_directories` |
| `logs` | MIXED | `logs` | profile: `logs/agent.log`, `logs/gateway.log`, `logs/errors.log`; root: `logs/desktop.log`, `logs/dashboard.log`, `logs/gui.log`, `logs/update.log`, `logs/gateway.error.log`, `logs/tui_gateway_crash.log`, `logs/audit.log`, `logs/mcp-stderr.log`, `logs/workspace.log`, `logs/workspace.error.log`, `cron/output/` | both | `hermes_logging.py:180-181,194-199` (agent/errors/gateway/gui all `<home>/logs`); `hermes_cli/gateway.py:3762-3768`; `tools/mcp_tool_config.py:31-35`; `tui_gateway/server.py:43,49`; `hermes_cli/update_cmd.py:437`; `apps/desktop/electron/main.ts:826,880` (desktop.log at the root); `cron/jobs.py:96` | agrees (`agent`, `gateway`, `errors`, `desktop`); diverges (`gui`, `update`, `gateway.error`, `tui crash`, `mcp.stderr`, `cron/output`); ambiguous (`dashboard`, `workspace`, `workspace.error`, `audit` — no upstream writer found) | `test_profiled_collector_labels_log_stream_scope`, `test_root_collector_keeps_ownership_labels_on_log_streams` |
| `version_check` | ROOT | `version_check` | `.update_check` | `shared_path` | `hermes_cli/banner.py:266,390` | diverges | UNPINNED |
| `skin` | ROOT | `skin` | `config.yaml` (`display.skin`) | `shared_path` | `hermes_constants.py:1132-1135`; skins themselves live at `get_hermes_home()/"skins"` — `hermes_cli/skin_engine.py:365` | diverges — **intentional** (same decision as `config`) | `test_profiled_collector_keeps_shared_root_config_and_auth` |
| `curator` | MIXED | `curator` | profile: `skills/.curator_state`, `skills/.usage.json`; root: `logs/curator/`, `config.yaml` | both | `agent/curator.py:38`; `tools/skill_usage.py:49-50`; `agent/curator.py:455-457` ("telemetry next to agent.log, not under skills/"); `hermes_cli/config.py:615-617,623` | agrees (`.curator_state`, `.usage.json`); diverges (`logs/curator/`) | UNPINNED |
| `active_sessions` | PROFILE | `active_surfaces`, `active_surface_count` | `runtime/active_sessions.json` | `profile_path` | `hermes_cli/active_sessions.py:164-168` | agrees | `test_profiled_collector_does_not_read_root_scoped_profile_sources` |
| `session_leases` | PROFILE | `session_coordination.leases`, `session_coordination.lease_total` | `state.db` (`session_turn_leases`, `compression_locks`) | `profile_path` | `get_hermes_home()/"state.db"` — `hermes_state.py:160,178`; writers `hermes_state_compression.py:433-605` | agrees | `test_profiled_collector_reads_session_coordination_from_the_profile_db` |
| `gateway_hygiene` | PROFILE | `session_coordination.hygiene` | `state.db` (`gateway_hygiene_state`) | `profile_path` | `get_hermes_home()/"state.db"` — `hermes_state.py:160,178`; writer `hermes_state_gateway.py:513-535` | agrees | `test_profiled_collector_reads_session_coordination_from_the_profile_db` |
| `gateway_routes` | PROFILE | `session_coordination.routes`, `session_coordination.route_total` | `state.db` (`gateway_routing`) | `profile_path` | `get_hermes_home()/"state.db"` — `hermes_state.py:160,178`; payload writer `gateway/session.py:535-545` | agrees | `test_profiled_collector_reads_session_coordination_from_the_profile_db` |
| `generation_churn` | PROFILE | `session_coordination.generations`, `session_coordination.generation_*` | `state.db` (`conversation_generations`) | `profile_path` | `get_hermes_home()/"state.db"` — `hermes_state.py:160,178`; bump writer `_BUMP_GENERATION_SQL` `hermes_state_messages.py:30-34`; table DDL `hermes_state_common.py:482-487` | agrees | `test_profiled_collector_reads_session_coordination_from_the_profile_db` |
| `terminal_sessions` | PROFILE | `terminal_sessions` | `terminal-sessions/tty-*` (plus multiplexer-named breadcrumbs) | `profile_path` | `get_hermes_home()/"terminal-sessions"` — `hermes_cli/terminal_breadcrumbs.py:26-28`, write `:85-92`, 30-day prune `:19-21,63-73` | agrees | `test_profiled_collector_reads_session_coordination_from_the_profile_db` |
| `runtime` | MIXED | `runtime` | profile: `state.db`, `sessions/sessions.json`, `logs/agent.log`; root: `gateway_state.json` | both | `hermes_state.py:160`; `hermes_cli/status.py:295`; `hermes_logging.py:180-195`; `gateway/status.py:165-166` | agrees (profile paths); ambiguous (`gateway_state.json`) | UNPINNED |

## Scope notes

Consequences of a row's scope that are easy to misread in a panel. These are not
divergences — the resolver matches upstream — but the *reach* of the read differs
from the reach of upstream's own maintenance code.

**`active_sessions` (PROFILE) reports one registry, not the install.** hermesd
reads `profile_path("runtime", "active_sessions.json")`, exactly where upstream's
`_state_path` puts it (`hermes_cli/active_sessions.py:164-168`), so the row
agrees. But upstream's orphan reclamation sweeps the root home **and every profile
home** (`release_orphaned_leases`, `:660-687`), because a multiplexed gateway
leases across all of them. The occupancy the Sessions panel shows is therefore the
selected profile's registry only: with no `--profile` that is the root registry,
and leases held under any profile home are invisible. The `max_concurrent_sessions`
cap it is compared against is read from the root `config.yaml` (see the `config`
row), which is the file the root-registry acquirer resolves — but a profile-scoped
backend passes its own profile home as `registry_home` while still resolving the
cap through `get_hermes_home()`. Capacity and occupancy can therefore come from
different homes under `--profile`. The resolver is deliberately unchanged: reading
every profile's registry would make panel 2 an install-wide aggregate that no
single upstream enforcement point corresponds to.

**`hosted_rooms` (ROOT) and `api_runs` (PROFILE) are two databases, two homes, one panel.** They sit beside each other in panel 12's detail view and are the easiest pair in hermesd to conflate, because both are gateway coordination stores and both are usually empty. They are not the same scope and not the same kind of thing:

- `shared-state.db` is ROOT even under `--profile`, and is deliberately *not* the master `state.db`. Upstream says why at `gateway/hosted_rooms.py:398-414`: pointing a profile gateway at the session store makes it a long-lived writer on `state.db`, which is the multi-writer corruption vector that file exists to avoid. The live `~/.hermes/state.db` nonetheless still carries `hosted_rooms`, `hosted_room_events` and every sibling table with **zero rows** and no DDL in `hermes_state_common.py` — legacy leftovers. Reading them would report a dead table as the coordination state, so the reader never opens `state.db` at all and a test pins the SQL target.
- `runs_idempotency.db` is PROFILE, matching upstream's `get_hermes_home()`. With no `--profile` that resolves to the root copy, so reservations made by a profile-scoped API server are invisible in root mode — the same shape as every other PROFILE row here.

Neither store's emptiness means what it looks like. `hosted_room*` is empty on any install that has never hosted a room. `run_idempotency` is a **replay window, not an activity ledger**: `_prune_stale_terminal_locked` (`api_server_run_idempotency.py:168-186`) runs inside every `reserve`/`lookup` and deletes an aged row only once its stored status is terminal, long room runs extend `retention_until`, and when the file cannot be opened upstream falls back to `":memory:"` (`:63-84`) — a fallback hermesd cannot detect, because the `durable` capability is advertised only over HTTP (`api_server.py:2276`) and never written to disk. Panel 12 says all of this in words rather than leaving the operator to infer it from a zero.

**`db_recovery` (PROFILE) reports one database's artifacts.** Every recovery
artifact is a sibling of the `state.db` upstream repairs, and that database is
`get_hermes_home()/"state.db"`, so the scan is confined to `profile_home`. A root
ledger is not reported for a selected profile and vice versa; unlike
`active_sessions`, upstream has no cross-home sweep here to diverge from.

## Intentional-divergence register

Rows where hermesd **deliberately** reads a different home than upstream, each
pinned by a test that names the intent. This list is the only place a divergence
is a decision. Adding a row here requires a pinning test in
`tests/test_collector_profiles.py`.

| path | hermesd | upstream | why | pinned by |
| --- | --- | --- | --- | --- |
| `~/.hermes/config.yaml` | ROOT | PROFILE — `hermes_constants.py:1132-1135` | hermesd is one dashboard over the whole install. Following a profile's `config.yaml` would make the Config, Skin, Cron-config and MoA panels change meaning depending on a flag, and hermesd does not follow `active_profile`, so the root file is the only one whose provenance is stable. | `test_profiled_collector_keeps_shared_root_config_and_auth` |
| `~/.hermes/auth.json` | ROOT | PROFILE primary — `hermes_cli/auth.py:471-472`, with a documented **read-only root fallback** at `hermes_cli/auth.py:484-493` (also `hermes_cli/auth_oauth_grants.py:79`) | hermesd reads the same root copy upstream falls back to, so provider and credential-pool names stay comparable across profiles. Known cost: a profile-local `auth.json` that *overrides* the root store is invisible to hermesd. | `test_profiled_collector_keeps_shared_root_config_and_auth` |
| `~/.hermes/backups/config/` | ROOT | PROFILE — `hermes_constants.py:1132-1135` via `hermes_cli/config_backups.py:29-30,45-69` | Point-in-time copies of the same root `config.yaml` the `config` row reads; reporting a profile-local backups directory would date a config the dashboard never displays. Same decision and same cost as the `config.yaml` row above. | `test_profiled_collector_keeps_config_backups_on_the_shared_root` |

Nothing else belongs here yet. In particular `cron` is **not** an intentional
divergence: upstream comments it as per-profile by design in three separate
places and warns against exactly what hermesd does.

## Open divergences (not blessed)

hermesd reads these `ROOT` while upstream resolves them through
`get_hermes_home()`, i.e. `PROFILE`. With a profile selected, hermesd shows the
root copy and silently misses the selected profile's data.

Most rows are open questions: the tests named "behaviour pin" record what the
code does today so a change is deliberate, not so that the behaviour is
endorsed.

Six rows are **decided**, not open, and are marked as such in their note: the
root gateway's launch cluster (`gateway-starts.log`,
`state/dashboard_clients.heartbeat`, `state/gateway.loop-tick.<pid>.sock`,
`logs/gateway-exit-diag.log`) belongs to the one root gateway process whose
heartbeat and lifecycle sentinel hermesd already reads, and `cache/delegation/live/`
plus `cache/plugin-catalog.json` are each kept on the resolver of the data they
are compared against. They are not listed in the intentional-divergence register
because that register is for *blessed* decisions with a cost the operator should
weigh (a profile-local file made invisible); these six read the only copy that
exists for the process they describe. Each carries its pin.

| path | upstream | note |
| --- | --- | --- |
| `~/.hermes/cron/jobs.json` | PROFILE — `cron/jobs.py:60-72,114-133` | Upstream: "Cron is per-profile by design (#4707) … Do NOT change this to the default root: that re-breaks per-profile isolation." Strongest divergence in this list. Behaviour pin: `test_profiled_collector_reads_root_scoped_cron_kanban_and_gateway`. |
| `~/.hermes/cron/executions.db` | PROFILE — `cron/executions.py:37`, `cron/incidents.py:52` | Same `cron/` store as `jobs.json`; must move with it. |
| `~/.hermes/cron/.tick.lock`, `cron/ticker_heartbeat`, `cron/ticker_last_success` | PROFILE — `cron/scheduler.py:1017-1032`, `cron/jobs.py:76-77` | Ticker health is currently reported for the root store only. |
| `~/.hermes/cron/ticker_last_error`, `cron/catch_up_occurrences` | PROFILE — `_current_cron_store()` `cron/jobs.py:114-133`; written by `_write_marker` `:1129-1136`, read by `get_ticker_last_error` `:1212-1220` and `get_catch_up_occurrence_count` `:1186-1193` | Kept on the root resolver deliberately, with the rest of `cron/`, so the `cron` row above stays accurate for every path it names. Consequence: under `--profile`, a selected profile's recorded tick failure and its missed-fire catch-up counter are invisible, exactly as its `jobs.json` and ticker stamps already are. Making only these two markers profile-scoped would make the row `MIXED` for no operator benefit while the jobs they describe still come from the root store. |
| `~/.hermes/cron/output/` | PROFILE — `cron/jobs.py:96` (`OUTPUT_DIR = CRON_DIR/"output"`) | Feeds both the `cron` excerpts and the `logs` "cron" stream. |
| `~/.hermes/cron/suggestions.json` | PROFILE — `cron/suggestions.py:31,47-48` | "Per-profile by design (issue #4707)". |
| `~/.hermes/logs/update_receipts/latest.json` | PROFILE — `hermes_cli/update_receipt.py:120-122` | Update/code-skew verdicts describe the root home's last update only. |
| `~/.hermes/.update_check` | PROFILE — `hermes_cli/banner.py:266,390` | Drives `version_check` and `gateway.updates_behind`. |
| `~/.hermes/context_length_cache.yaml` | PROFILE — `agent/model_metadata.py:1041-1044` | Feeds `SessionInfo.context_limit`. |
| `~/.hermes/cache/banner_snapshot.json` | PROFILE — `hermes_cli/banner.py:625-626` | Feeds `tools_index` and `toolset_availability`. |
| `~/.hermes/.skills_prompt_snapshot.json` | PROFILE — `agent/prompt_builder.py:1077-1078` | |
| `~/.hermes/state/gateway.heartbeat` | PROFILE — `gateway/shutdown_watchdog.py:27,44` | Loop-liveness for the root gateway only. |
| `~/.hermes/state/gateway.lifecycle.json` | PROFILE — `gateway/lifecycle_ledger.py:27-41` | **Decided.** Exit code / unclean-exit record of the root gateway only — the same process whose heartbeat and witness hermesd reads from the root, so the sentinel is read from the root beside them. Behaviour pin: `test_gateway_launch_files_are_root_scoped_under_a_profile` (root and profile sentinels carry different exit codes and reasons; only the root one is reported). |
| `~/.hermes/state/gateway.loop-tick.<pid>.sock` | PROFILE — `gateway/shutdown_watchdog.py:169` | **Decided.** Loop-scheduling witness of the root gateway. Probed read-only: the client sends nothing, reads at most one byte (`hermes_cli/gateway.py:363-376`). Behaviour pins: `test_loop_tick_witness_is_probed_from_the_root_home_under_a_profile`, `test_probed_loop_tick_resolves_the_node_under_the_root_home`. |
| `~/.hermes/gateway-starts.log` | PROFILE — `gateway/status.py:57-83` | Respawn-storm ledger for the root gateway only; kept beside the other launch-home gateway files so the storm count and the heartbeat that clocks restarts describe the same process. The cap and window are the *decided* policy from root `config.yaml` (`gateway.respawn_storm`, `hermes_cli/gateway.py:4673-4685`), read as the root file reads it. Behaviour pin: `test_gateway_launch_files_are_root_scoped_under_a_profile`. |
| `~/.hermes/state/dashboard_clients.heartbeat` | PROFILE — `gateway/scale_to_zero.py:97-111` | **Decided.** Attachment marker of the web dashboard served by the root gateway; stat'd for its mtime, never read. Behaviour pin: `test_gateway_launch_files_are_root_scoped_under_a_profile` (asserts `attached is False` from the aged root marker, which a profile read cannot produce). |
| `~/.hermes/logs/gateway-exit-diag.log` | PROFILE — `hermes_cli/gateway.py:4643-4665`, `gateway/lifecycle_ledger.py:96-104` | **Decided.** Crash forensics of the root gateway's exit paths (plus its stat-only companions: shutdown-diag, faulthandler, launchd-reload). Tail-read with the log-tail-bytes cap; extras such as tracebacks and argv are never parsed. Behaviour pin: `test_gateway_launch_files_are_root_scoped_under_a_profile`. |
| `~/.hermes/response_store.db` | PROFILE — `gateway/platforms/api_server.py:688` | |
| `~/.hermes/state-snapshots/` | PROFILE — `hermes_cli/backup.py:35,1088-1090,1380` | |
| `~/.hermes/cache/blocked-scripts/` | PROFILE — `tools/approval_floors.py:63-64` | |
| `~/.hermes/cache/delegation/live/` | PROFILE — `tools/delegation_live_log.py:40-43` ("profile-safe, never ~/.hermes") | **Decided.** Feeds the live-log count inside `operations` and the whole `delegation_live` source (manifests + task-log tails); kept on the root resolver so both read the same home. |
| `~/.hermes/cache/mcp_schema_cache.json` | PROFILE — `tools/mcp_schema_cache.py:18,22-24` | |
| `~/.hermes/processes.json` | PROFILE — `tools/process_registry.py:41,45-50` | Contrast `spawn-ledger.json`, which upstream *does* anchor at the root — the two registries are not the same scope. |
| `~/.hermes/desktop-build-stamp.json` | PROFILE — `hermes_cli/main_desktop.py:42-45` | In practice the desktop backend runs with the root `HERMES_HOME`, so the root copy is usually the real one; that is an observation about one deployment, not an upstream guarantee. |
| `~/.hermes/web-ui-build-stamp.json` | PROFILE — `hermes_cli/main_web_build.py:187-190` | Same caveat as the desktop stamp. |
| `~/.hermes/channel_directory.json`, `channel_aliases.json` | PROFILE — `gateway/channel_directory.py:40-45` | |
| `~/.hermes/.drain_request.json` | PROFILE — `gateway/drain_control.py:29,61` | |
| `~/.hermes/logs/curator/` | PROFILE — `agent/curator.py:455-457`, `hermes_cli/config.py:615-617,623` | The Curator panel's run reports come from the root copy; `skills/.curator_state` next to it is already profile-scoped, so panel 13 mixes both. |
| `~/.hermes/hooks/` | PROFILE — `gateway/hooks.py:25,29-37` | Upstream is explicit: "under `gateway.multiplex_profiles` every served profile has its own `hooks/`". Created per home at `hermes_cli/config.py:615-617,623`. |
| `~/.hermes/plugins/` | PROFILE — `plugins/plugin_loader.py:28-34`, `hermes_cli/config_migrations.py:271` | `user_plugins_dir()` is `get_hermes_home()/"plugins"`. Both provenance sidecars inherit the same divergence, because upstream resolves them off the same home: `_install_metadata_path()` is `get_hermes_home()/"plugins"/".install-metadata.json"` (`hermes_cli/plugins_cmd.py:425-426`) and `.hermes-catalog.json` is written inside the install directory (`hermes_cli/plugins_cmd_catalog.py:24,62-73`). So under `--profile`, a selected profile's own plugins *and* their install provenance are invisible. |
| `~/.hermes/cache/plugin-catalog.json` | PROFILE — `hermes_cli/plugin_catalog.py:216-218` | **Decided.** The live-catalog cache hermesd compares the plugin sidecars against. Kept on the root resolver with the `plugins/` row above so both sides of the drift comparison describe one home: comparing a root-installed plugin's sidecar against a profile's cache would manufacture drift. Behaviour pin: `test_plugin_catalog_cache_is_read_from_the_shared_root_under_a_profile`. |
| `~/.hermes/moa-traces/` | PROFILE (default) — `agent/moa_trace.py:25-38` | Default is `get_hermes_home()/"moa-traces"`; `moa.trace_dir` overrides it. Upstream resolves a *relative* override against the process cwd (`expandvars`/`expanduser` only), hermesd resolves it against `root_home` — a second, separate difference. |
| `~/.hermes/logs/gui.log` | PROFILE — `hermes_logging.py:180-181,198` | |
| `~/.hermes/logs/update.log` | PROFILE — `hermes_cli/update_cmd.py:437` | |
| `~/.hermes/logs/gateway.error.log` | PROFILE — `hermes_cli/gateway.py:3762-3768` | Note `logs/gateway.log` is written by the same `log_dir` and hermesd already reads *that* one profile-scoped. |
| `~/.hermes/logs/tui_gateway_crash.log` | PROFILE — `tui_gateway/server.py:43,49` | Frozen at import time upstream, but still `get_hermes_home()`. |
| `~/.hermes/logs/mcp-stderr.log` | PROFILE — `tools/mcp_tool_config.py:31-35` | |

### Ambiguous upstream

Neither `_PROFILE_DIRS` (`hermes_cli/profiles.py:29`) nor an exclude set settles
these, or upstream has both anchors at once. Do not "fix" them without deciding
the question first.

| path | evidence |
| --- | --- |
| `~/.hermes/gateway_state.json`, `~/.hermes/gateway.pid` | The *writer* uses the process home (`gateway/status.py:157-158,165-166`), which is the profile home for a profile-launched gateway. But the root-anchored readers are deliberate (`hermes_cli/gateway_multiplex_served.py:23,36,54`), and `gateway/status.py:927` records that "a served profile owns no `gateway.pid`/`gateway_state.json` (#97120)" — so under the multiplexer the root copy is the only one that exists. Per-profile copies are nonetheless read at `hermes_cli/web_routers/status.py:235` and `hermes_cli/gateway.py:769,773`. hermesd's root-only read is correct for the multiplexed case and blind for a standalone profile gateway. |
| `~/.hermes/BOOT.md` | No path builder anywhere in the upstream tree. The only reference is prose — `hermes_cli/tips.py:334`: "Drop a `~/.hermes/BOOT.md` checklist…" — which names the root. hermesd's `shared_path("BOOT.md")` follows that prose. |
| `~/.hermes/logs/dashboard.log`, `logs/workspace.log`, `logs/workspace.error.log`, `logs/audit.log` | No writer for these four names anywhere in the upstream checkout (searched `.py`, `.ts`, `.mjs` and `.sh`). Upstream's `audit.log` lives at `skills/.hub/audit.log` (`tools/skills_hub.py:60`) and in the proxy state dir (`hermes_cli/proxy_cli.py:237`), not under `logs/`. Three of the four nonetheless exist in the root `~/.hermes/logs/` on the maintainer's machine — `dashboard.log`, `workspace.log` and `workspace.error.log`, written alongside `dashboard.error.log` and `dashboard-restart.log` — so something outside this checkout produces them. hermesd discovers all four opportunistically and shows a stream only when its file exists. They are `ROOT` by observation, not by decision. |

## Mixed-model register

Models whose fields span more than one scope. A single panel rendering one of
these is showing more than one home at once; per-field labels would be a redesign,
so they are recorded here instead. `LogStream` is the exception: it now carries
`scope` per entry, and the Logs detail view renders it.

| model | what it mixes |
| --- | --- |
| `LogStream` | **Labelled.** `scope` per stream; panel 8's detail view renders `Scope: root` / `Scope: profile` for the selected stream, and `--snapshot-format json` carries it. The scope names the resolver that *owns* the stream, not the directory it resolved to — in root mode a profile-owned stream reads the root copy and is still labelled `profile`. |
| `RuntimeStatus` | `agent_running` / `last_activity_age_seconds` come from profile `state.db`, `sessions/sessions.json` and `logs/agent.log`, but also from root `gateway_state.json` (`collect/system.py:227-237`). A "runtime idle" banner can therefore mix one profile's activity with the root gateway's. |
| `LogState` | `streams` mixes 3 profile-scoped and ~11 root-scoped entries (see `logs` above). The four legacy convenience fields (`agent_lines`, `gateway_lines`, `error_lines`, `cron_lines`) inherit whichever stream they came from: the first three are profile-owned, `cron_lines` is root-owned. |
| `SkillsMemory` | `skills`/`memory_file_count` are profile-scoped; `providers`/`credential_pools` come from root `auth.json` (intentional); `hooks` from root `hooks/`; agent `plugins` from root `plugins/`; `desktop_plugins` from the distinct app-level root `desktop-plugins/`; `boot_md_*` from root `BOOT.md` (ambiguous). One panel, at least three provenances. |
| `OperationsState` | Profile: `projects*`, `verification_*`, goals/delegations/`state_db_*`, `db_recovery`, **`api_runs`**, **`process_receipts`**, **`checkpoint_prune_marker_*`**. Root: `response_store_*`, `moa_trace_*`, `snapshot_*`, `blocked_script_*`, `delegation_live_log_count`, **`delegation_live_manifests`/`delegation_live_manifest_count`**, **`spawn_ledger_corrupt_*`**, both build stamps, `pr_monitors`, **`hosted_rooms`**. Panel 12 therefore renders at least four different homes: the hosted-room/api-run pair side by side, and the live-manifest cards (root cache) beside the process receipts (profile `logs/`). |
| `CuratorRun` | `skills/.curator_state` is profile-scoped; the `logs/curator/<stamp>/run.json` report tree is root-scoped. The two halves of one panel describe different homes. |
| `GatewayState` | Each field group is its own `source_name`, restored independently: `_HEARTBEAT_FIELDS`, `_LOOP_TICK_FIELDS`, `_LIFECYCLE_FIELDS`, `_RESTART_STORM_FIELDS` (cap/window come from root `config.yaml`), `_EXIT_DIAG_FIELDS`, `_DASHBOARD_CLIENT_FIELDS`, `_UPDATE_RECEIPT_FIELDS` and `_LEDGER_FIELDS`. All are root-scoped except **`_LEDGER_FIELDS`, the only profile-scoped contributor to this nominally root panel** — it reads `gateway_heartbeats`/`delivery_obligations` out of the profile's `state.db`. The base `gateway` group is root-scoped but ambiguous (see above); the restart-storm policy is the one field group whose *value* comes from root `config.yaml` rather than a state file. |
| `ConfigSummary` | Three provenances in one model: root `config.yaml`; `PROCESS-ENV` values from hermesd's own environment (below); and `kanban_dispatch_in_gateway`-style keys that describe a root-anchored subsystem. The code already comments "these values come from the dashboard process environment, not Hermes runtime state" — but no field distinguishes them, so a reader cannot tell a Hermes setting from a hermesd one. |

### Process-environment values (`PROCESS-ENV`)

Read in `collector.py::_collect_config` / `_collect_tool_gateway_routes` from
`self._env` (hermesd's own process environment, injectable for tests). These are
not Hermes state and never come from `~/.hermes`. Checked against the code by
`test_source_ownership_doc_covers_every_process_env_read`.

| env var | ConfigSummary field |
| --- | --- |
| `TOOL_GATEWAY_DOMAIN` | `tool_gateway_domain` |
| `TOOL_GATEWAY_SCHEME` | `tool_gateway_scheme` |
| `FIRECRAWL_GATEWAY_URL` | `firecrawl_gateway_url` (URL-redacted) |
| `TOOL_GATEWAY_USER_TOKEN` | `tool_gateway_routes[].token_present` (presence only) |
| `HERMES_DASHBOARD_AUTH_PROVIDER` | `dashboard_auth_provider` (fallback after `dashboard.auth_provider`) |
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` | `dashboard_basic_auth_configured` (presence only) |
| `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | `dashboard_basic_auth_configured` (presence only) |

Two further environment variables are read at the CLI layer, not by the collector,
and select the home itself rather than a source inside it: `HERMES_HOME`
(`__main__.py:105`) and `HERMES_PROFILE` (`__main__.py:115`).

## Sources hermesd does not read

Recorded so the next reader does not have to re-derive the scope from scratch.

| path | upstream scope | note |
| --- | --- | --- |
| `~/.hermes/active_profile` | ROOT | `hermes_constants.py:80`. hermesd deliberately does **not** follow it — pinned by `test_default_collector_ignores_active_profile_file`. |
| `~/.hermes/profiles/<name>/profile.yaml` | PROFILE | `hermes_cli/profile_describer.py:191`; read for `ui_meta` by `tools/bot_mode_probe.py:103,131`. |
| `~/.hermes/.env` (and per-profile `.env`) | PROFILE | `hermes_constants.py:1142-1145` (`get_env_path()`). Secrets; hermesd has no business reading it. |
| `~/.hermes/skins/` | PROFILE | `hermes_cli/skin_engine.py:365`. hermesd reads the skin *name* from root `config.yaml` and resolves colours itself. |
