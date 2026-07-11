# Hermes Agent feature visibility analysis for hermesd

Date: 2026-07-11

## Scope

This report compares current Hermes Agent release behavior against hermesd's monitoring surface and identifies read-only additions that would improve operator visibility.

Inputs verified:

- Official GitHub releases for `NousResearch/hermes-agent`.
- Official Hermes Agent update documentation.
- Local Hermes Agent checkout at `/Users/mudrii/src/hermes/hermes-agent`, after fetching tags from `origin`.
- Local hermesd source, README, and changelog at `/Users/mudrii/src/hermes/hermesd`.

Important hermesd constraints:

- hermesd must stay read-only against `~/.hermes/`.
- hermesd must not import Hermes Agent code.
- SQLite reads must preserve last good state on errors.
- SQLite NULL handling should use explicit `or 0` / `or ""` style.
- New feature work should be TDD-first and surgically scoped.

## Release verification

The latest official Hermes Agent release found during verification is:

- `v2026.7.7.2` / Hermes Agent `v0.18.2`
- Published: 2026-07-08 UTC
- Release page: https://github.com/NousResearch/hermes-agent/releases/tag/v2026.7.7.2

That release is a same-window patch. The main change is dependency/install reliability for WhatsApp Baileys in tagged Docker builds. It does not materially change the hermesd monitoring roadmap by itself.

The high-signal feature window for hermesd alignment is:

- `v2026.7.1` / Hermes Agent `v0.18.0`, "Judgment Release"
  - Release page: https://github.com/NousResearch/hermes-agent/releases/tag/v2026.7.1
- `v2026.6.19` / Hermes Agent `v0.17.0`, "Reach Release"
  - Release page: https://github.com/NousResearch/hermes-agent/releases/tag/v2026.6.19

The official update docs also matter because `hermes update` tracks the latest code from `main`, runs config migration, and restarts gateways. That means hermesd should prefer tolerant readers over strict tag assumptions.

- Update docs: https://hermes-agent.nousresearch.com/docs/getting-started/updating

## Current hermesd coverage

hermesd already has broad coverage across 13 panels:

- Gateway: PID, version, platform state, platform errors, active-agent count, restart marker.
- Sessions: session summaries, parent lineage, handoff metadata.
- Tokens/Cost: provider/model cost summaries and endpoint status.
- Tools: background process and checkpoint visibility.
- Config: gateway, routing, memory, dashboard auth, kanban, tool search, code execution, hooks, and related settings.
- Cron: jobs, config, tick/output metadata.
- Skills/Integrations: skills, provider auth, credential pools, hooks, plugins, MCP, BOOT files.
- Logs: agent, gateway, errors, cron, desktop, dashboard, GUI, update, gateway error, TUI crash, workspace, audit, MCP stderr.
- Profiles: discovered profile/runtime view.
- Memory: memory files and summaries.
- Kanban: default `kanban.db` task/run/event/comment counts, active/problem tasks, recent runs, task links, attachments, metadata.
- Operations: dashboard processes, Desktop build stamp, response store, model cache, PR monitor files.
- Curator: latest memory-curation run, counts, model/provider, tool calls, transitions, summary/error.

This means the right roadmap is not "add every release-note item." The best roadmap is to add the new persisted surfaces that are currently invisible or only partially visible.

## Highest-value implementation candidates

### 1. Verification and goal evidence

Status: missing or only indirectly visible.

Hermes Agent v0.18.0 added stronger `/goal` completion contracts and a coding verification evidence ledger. The local source shows a dedicated `verification_evidence.db` with `verification_events` and `verification_state` tables.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/agent/verification_evidence.py`
- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/goals.py`

Candidate hermesd implementation:

- Add a Verification section or panel.
- Read `~/.hermes/verification_evidence.db` read-only.
- Show latest checks by session/root, command kind, scope, status, exit code, and age.
- Show `verification_state` rows with changed paths and last edit time.
- Read `/goal` state from `state.db.state_meta` keys if present, using schema-tolerant queries.
- In detail view, group by project root and session.
- In snapshot JSON, expose structured fields so release automation can inspect verification health.

Value:

- Directly aligns with the v0.18 "done means proven" feature.
- Makes Hermes Agent's most important new coding reliability surface visible from hermesd.

Risk:

- Medium. Goal metadata may evolve, so readers must tolerate missing tables, missing keys, and JSON decode failures.

Suggested phase:

- Phase 1 / P0.

### 2. Mixture-of-Agents visibility

Status: partially visible through normal session/token provider fields, but MoA-specific configuration and traces are invisible.

Hermes Agent v0.18.0 made MoA a selectable virtual provider and added optional JSONL trace persistence through `moa.save_traces`.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/moa_config.py`
- `/Users/mudrii/src/hermes/hermes-agent/agent/moa_trace.py`
- Default trace path: `~/.hermes/moa-traces/<session_id>.jsonl`

Candidate hermesd implementation:

- Add MoA summary rows to Config:
  - enabled/available by config shape,
  - default preset,
  - preset count,
  - reference model count,
  - aggregator model/provider,
  - `moa.save_traces` state.
- Add MoA trace inventory to Operations:
  - trace file count,
  - total bytes,
  - newest trace mtime,
  - newest session id,
  - bounded latest-record summary.
- Avoid rendering full prompts, reference outputs, or aggregator outputs by default.

Value:

- MoA is a headline v0.18 feature.
- Operators can tell whether MoA is configured, being used, and generating debug artifacts.

Risk:

- Medium. Trace files can contain sensitive prompts and model outputs. Initial implementation should be counts and metadata only.

Suggested phase:

- Phase 1 / P1 for config and inventory.
- Phase 2 for optional privacy-safe detail summaries.

### 3. First-class Projects visibility

Status: missing.

Hermes Agent Desktop added per-profile Projects backed by `projects.db`. The source shows explicit project, folder, metadata, and discovered repo tables.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/projects_db.py`

Candidate hermesd implementation:

- Add Projects summary to Operations or a new Projects panel.
- Read `~/.hermes/projects.db` read-only.
- Show:
  - project count,
  - archived count,
  - folder count,
  - discovered repo count,
  - project to `board_slug` mappings,
  - projects missing primary paths,
  - newest discovered repos.

Value:

- Aligns hermesd with the Desktop coding cockpit direction.
- Helps explain why sessions, kanban tasks, and repos are grouped the way they are.

Risk:

- Low to medium. The database schema is straightforward, but profile-scoped homes must be handled consistently.

Suggested phase:

- Phase 1 / P1.

### 4. Gateway lifecycle, drain, and scale-to-zero state

Status: partially visible.

hermesd currently shows `gateway_state`, `active_agents`, platform errors, and restart markers. Hermes Agent now has explicit drain coordination and scale-to-zero behavior. The local source shows `.drain_request.json`, busy/drainable derivation, and scale-to-zero helpers.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/gateway/drain_control.py`
- `/Users/mudrii/src/hermes/hermes-agent/gateway/status.py`
- `/Users/mudrii/src/hermes/hermes-agent/gateway/scale_to_zero.py`

Candidate hermesd implementation:

- Read `~/.hermes/.drain_request.json` safely.
- Show whether external drain is active, requested age, principal, and suppress-notification flag if present.
- Derive and render gateway busy/drainable states from `gateway_state` and `active_agents`.
- Show `served_profiles` from `gateway_state.json` when present.
- Surface scale-to-zero config from `config.yaml`, especially idle timeout and relay-only intent.

Value:

- High for hosted/team operators.
- Makes restarts, dormancy, and maintenance windows explainable.

Risk:

- Medium. Avoid overclaiming why a gateway is draining unless the marker/config makes it clear.

Suggested phase:

- Phase 1 / P1.

### 5. Multi-board Kanban and typed blockers

Status: partially visible.

hermesd reads the root `~/.hermes/kanban.db` and already surfaces tasks, runs, links, attachments, workers, and metadata. Hermes Agent has expanded multi-board behavior under `kanban/boards/<slug>/kanban.db`, current-board resolution, and typed block reasons.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/kanban_db.py`
- Current hermesd reader: `/Users/mudrii/src/hermes/hermesd/hermesd/collector.py`
- Current hermesd models: `/Users/mudrii/src/hermes/hermesd/hermesd/models.py`

Candidate hermesd implementation:

- Discover `~/.hermes/kanban/boards/*/kanban.db` without following symlinks.
- Read `~/.hermes/kanban/current` when present.
- Show board count and current board.
- Aggregate task counts per board.
- Surface `block_kind` counts when the column exists.
- Show stale claims using `last_heartbeat_at`, `claim_expires`, and configured TTLs when available.
- Keep default board behavior exactly as-is for homes without multi-board state.

Value:

- High for project-based Hermes use.
- Connects Desktop Projects, kanban boards, and background workers.

Risk:

- Medium. Multiple databases increase read cost and need bounded iteration.

Suggested phase:

- Phase 1 / P1.

### 6. Journey and learning graph summary

Status: partially visible through Memory and Skills panels, but not as a learning graph/timeline.

Hermes Agent v0.18.0 added `/journey` and a memory graph. The source builds this from skill metadata, usage, and memory files.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/journey.py`
- `/Users/mudrii/src/hermes/hermes-agent/agent/learning_graph.py`

Candidate hermesd implementation:

- Add a Learning subsection to Memory or Skills.
- Read only persisted files:
  - `skills/.usage.json`,
  - learned/profile skill metadata,
  - `memories/MEMORY.md`,
  - `memories/USER.md`.
- Show:
  - learned skill count,
  - pinned skill count,
  - agent-created skill count,
  - usage-bearing skill count,
  - memory card count,
  - isolated vs linked node counts if derivable cheaply.

Value:

- Useful for visibility into the self-improvement loop.
- Complements the existing Curator panel.

Risk:

- Medium. Do not import `agent.learning_graph`; mirror only the minimum stable parsing hermesd needs.

Suggested phase:

- Phase 2 / P2.

### 7. Cron provider, Chronos, and automation blueprint visibility

Status: partially visible.

hermesd already has a Cron panel for jobs and recent runtime metadata. Hermes Agent added provider-based cron scheduling, Chronos managed cron for scale-to-zero deployments, and Automation Blueprints.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/cron/scheduler_provider.py`
- `/Users/mudrii/src/hermes/hermes-agent/docs/chronos-managed-cron-contract.md`
- `/Users/mudrii/src/hermes/hermes-agent/cron/blueprint_catalog.py`
- `/Users/mudrii/src/hermes/hermes-agent/cron/suggestions.py`

Candidate hermesd implementation:

- Extend Config/Cron detail with:
  - `cron.provider`,
  - Chronos portal URL presence,
  - callback URL presence,
  - expected audience presence,
  - JWKS URL presence,
  - builtin fallback suspicion from logs if provider load fails.
- Show cron suggestion counts if persisted suggestion files exist.
- Do not import the blueprint catalog from Hermes Agent.
- Do not duplicate the blueprint UI; hermesd should monitor configured jobs and provider health, not become a blueprint authoring surface.

Value:

- Important for hosted scale-to-zero cron reliability.
- Good fit for hermesd's operational posture.

Risk:

- Low for config presence.
- Medium if trying to infer provider health from logs.

Suggested phase:

- Phase 2 / P2.

### 8. Channel directory and new messaging adapter detail

Status: partially visible.

Hermes Agent v0.17/v0.18 expanded platform coverage and media support, including WhatsApp Cloud/Baileys changes, Teams media, iMessage Photon, Raft, and platform aliasing. hermesd already shows platform states from `gateway_state.json` and directory entries.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/gateway/channel_directory.py`
- `/Users/mudrii/src/hermes/hermes-agent/gateway/platforms/whatsapp_cloud.py`

Candidate hermesd implementation:

- Read `channel_aliases.json` if present.
- Show alias count and stale alias warnings.
- Add labels for known platform families:
  - WhatsApp Cloud,
  - WhatsApp Baileys,
  - Teams,
  - Photon/iMessage,
  - Raft,
  - Slack, Discord, Telegram, Matrix, etc.
- Flag platforms present in gateway state but absent from channel directory.

Value:

- Helps explain multi-platform gateway behavior without needing the Hermes dashboard.

Risk:

- Low if limited to persisted state.

Suggested phase:

- Phase 2 / P2.

### 9. Curator scheduler state

Status: mostly visible, with one useful gap.

hermesd already has a strong Curator panel for the newest run report. Hermes Agent also has curator scheduler state and consolidation config.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/agent/curator.py`
- Current hermesd reader: `/Users/mudrii/src/hermes/hermesd/hermesd/collector.py`

Candidate hermesd implementation:

- Read `skills/.curator_state` safely if present.
- Show:
  - paused state,
  - last run id/path,
  - run count,
  - last report path,
  - consolidation enabled/disabled from config.

Value:

- Completes the existing Curator panel rather than creating a new surface.

Risk:

- Low.

Suggested phase:

- Phase 2 / P2.

## Lower-priority or not recommended

### Do not import Hermes Agent code

Some upstream features expose convenient Python helpers, especially for MoA config, `/journey`, cron blueprints, and projects. hermesd should not import those helpers. It should read persisted JSON/YAML/SQLite/file state directly.

### Do not render full MoA traces by default

`moa-traces/*.jsonl` can include prompts and full model outputs. The first hermesd implementation should display inventory and bounded metadata only. A later detail view can include opt-in redacted summaries if there is a clear user need.

### Do not replicate Hermes Desktop

Projects, profile builder, Skills Hub, memory graph, and cron blueprints are rich interactive surfaces in Hermes Agent. hermesd should monitor their runtime artifacts, not become a parallel editor.

### Do not overfit to release-note text

Hermes Agent's update path follows `main` and includes config migration. hermesd readers should key off persisted files and tolerate missing/extra columns rather than assuming a single tag schema.

## Proposed roadmap

### Phase 1: high-value operational gaps

1. Verification and goals
   - Add fixture DBs for `verification_evidence.db` and `state.db.state_meta`.
   - Add models and collector readers.
   - Add panel/detail rendering or Operations subsection.
   - Verify snapshot JSON output and read-only DB behavior.

2. MoA summary
   - Add config summary for `moa`.
   - Add trace inventory under Operations.
   - Keep trace reads bounded and content-safe.

3. Projects summary
   - Add read-only `projects.db` collector.
   - Show project/folder/repo/board mapping counts.

4. Gateway lifecycle
   - Add drain marker, busy/drainable, scale-to-zero config, and served profiles.

5. Kanban multi-board
   - Discover boards safely.
   - Add current board and per-board count summaries.
   - Add typed block reason counts when available.

### Phase 2: richer context once Phase 1 is stable

1. Learning graph summary from memory and skill persisted files.
2. Cron provider and Chronos config health.
3. Channel aliases and new adapter labels.
4. Curator scheduler state and consolidation config.

### Phase 3: deeper correlation

1. Connect Projects to Kanban board summaries.
2. Connect verification evidence to project roots and recent sessions.
3. Correlate MoA trace inventory with provider/model cost summaries.
4. Add dashboard/snapshot views that answer "what changed after the latest Hermes update?"

## Test plan

Use TDD for each implementation slice.

Recommended tests:

- Fixture `verification_evidence.db` with pass/fail/stale events and missing optional tables.
- Fixture `state.db` with and without goal metadata.
- Fixture `projects.db` with active and archived projects.
- Fixture `moa-traces/` with multiple JSONL files, malformed final lines, and large files.
- Fixture multi-board kanban directory with safe board DBs and a symlink that must be ignored.
- Fixture `.drain_request.json` with valid, missing, and malformed bodies.
- Fixture `gateway_state.json` with `served_profiles`, active agents, restart marker, and platform errors.
- Snapshot JSON assertions for all new fields.
- Panel rendering tests for long text, missing values, and malformed data.
- Read-only invariants: no writes to `~/.hermes/`, no Hermes Agent imports, and preservation of last good state after SQLite errors.

## Final priority ranking

| Rank | Candidate | Priority | Why |
|---:|---|---|---|
| 1 | Verification and goals | P0 | Directly monitors the v0.18 reliability headline: completed work backed by evidence. |
| 2 | MoA config and trace inventory | P1 | Directly monitors the v0.18 model headline without exposing sensitive trace content. |
| 3 | Projects summary | P1 | Aligns hermesd with Desktop's project-based coding workflow. |
| 4 | Gateway drain / scale-to-zero | P1 | High operator value for hosted or long-running gateways. |
| 5 | Multi-board Kanban | P1 | Completes the project/board/worker visibility story. |
| 6 | Learning graph summary | P2 | Useful self-improvement visibility, but less urgent than verification. |
| 7 | Cron provider / Chronos | P2 | Important for hosted cron reliability; config-first implementation is safe. |
| 8 | Channel aliases and adapter detail | P2 | Useful gateway diagnostics, especially with WhatsApp/Teams/Raft expansion. |
| 9 | Curator scheduler state | P2 | Small enhancement to an already strong panel. |

## Bottom line

hermesd is already aligned with many Hermes Agent runtime surfaces, especially gateway state, logs, cron, profiles, memory, kanban, operations, and curator runs. The strongest next step is to make Hermes Agent's new evidence-based coding workflow visible: verification events, goal contracts, project roots, and board state.

The next most valuable additions are MoA-safe observability, Projects, gateway lifecycle/drain state, and multi-board Kanban. Those changes fit hermesd's read-only architecture and give users better monitoring without duplicating Hermes Desktop or importing Hermes Agent internals.
