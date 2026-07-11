# Hermes Agent feature visibility analysis for hermesd

Date: 2026-07-11

## Scope

This report compares current Hermes Agent release behavior against hermesd's monitoring surface and identifies read-only additions that would improve operator visibility.

## Implementation status

This report started as an implementation roadmap. On branch `feature/hermes-agent-visibility`, the roadmap items below have now been implemented, tested, and documented in hermesd:

- Verification evidence and `/goal` state are visible in Operations.
- MoA config and bounded trace inventory are visible in Config and Operations.
- Projects are summarized in Operations, including repo/board correlation data.
- Gateway lifecycle now shows served profiles, drain state, busy/drainable state, scale-to-zero config, and relay-only intent.
- Kanban discovers multi-board state, current board, stale claims, and typed blocker counts.
- Memory shows a lightweight learning summary from persisted skills and memory files.
- Cron shows scheduler provider, Chronos config presence, and persisted suggestion counts.
- Gateway shows channel aliases, stale aliases, platform families, and missing channel-directory entries.
- Curator shows scheduler state and consolidation config.

The remaining roadmap is correlation depth, not basic visibility: richer cross-panel links between Projects, Kanban, verification evidence, MoA traces, cost summaries, and post-update change summaries.

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

- Gateway: PID, version, platform state, platform errors, active-agent count, restart marker, served profiles, drain/busy/drainable state, scale-to-zero config, relay-only intent, channel aliases, stale aliases, platform families, and missing channel-directory entries.
- Sessions: session summaries, parent lineage, handoff metadata.
- Tokens/Cost: provider/model cost summaries and endpoint status.
- Tools: background process and checkpoint visibility.
- Config: gateway, routing, memory, dashboard auth, kanban, tool search, code execution, hooks, MoA preset/reference/aggregator/trace settings, and related settings.
- Cron: jobs, config, tick/output metadata, scheduler provider, Chronos config presence, and persisted suggestion counts.
- Skills/Integrations: skills, provider auth, credential pools, credential freshness, hooks, plugins, MCP, BOOT files.
- Logs: agent, gateway, errors, cron, desktop, dashboard, GUI, update, gateway error, TUI crash, workspace, audit, MCP stderr.
- Profiles: discovered profile/runtime view.
- Memory: memory files, summaries, and lightweight learning summary.
- Kanban: default and multi-board `kanban.db` task/run/event/comment counts, current board, stale claims, typed blockers, active/problem tasks, recent runs, task links, attachments, metadata.
- Operations: dashboard processes, Desktop build stamp, response store, verification evidence, `/goal` state, MoA trace metadata, Projects summaries/correlations, model cache, PR monitor files.
- Curator: scheduler state, consolidation config, latest memory-curation run, counts, model/provider, tool calls, transitions, summary/error.

This means the original roadmap is now mostly complete. Future work should focus on deeper correlations and post-update summaries, not on adding every release-note item as a new panel.

## Highest-value implementation candidates

### 1. Verification and goal evidence

Status: implemented in Operations.

Hermes Agent v0.18.0 added stronger `/goal` completion contracts and a coding verification evidence ledger. The local source shows a dedicated `verification_evidence.db` with `verification_events` and `verification_state` tables.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/agent/verification_evidence.py`
- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/goals.py`

Implemented hermesd behavior:

- Reads `~/.hermes/verification_evidence.db` read-only.
- Shows verification totals, failed-check counts, latest evidence rows, pending changed-path counts, and `/goal` state from `state.db`.
- Preserves last-good data across transient SQLite/read failures.
- Exposes the new fields through the structured dashboard state and snapshot JSON.

Value:

- Directly aligns with the v0.18 "done means proven" feature.
- Makes Hermes Agent's most important new coding reliability surface visible from hermesd.

Risk:

- Medium. Goal metadata may evolve, so readers must tolerate missing tables, missing keys, and JSON decode failures.

Original roadmap phase:

- Phase 1 / P0.

### 2. Mixture-of-Agents visibility

Status: implemented across Config and Operations.

Hermes Agent v0.18.0 made MoA a selectable virtual provider and added optional JSONL trace persistence through `moa.save_traces`.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/moa_config.py`
- `/Users/mudrii/src/hermes/hermes-agent/agent/moa_trace.py`
- Default trace path: `~/.hermes/moa-traces/<session_id>.jsonl`

Implemented hermesd behavior:

- Config shows MoA preset/reference/aggregator/trace settings.
- Operations inventories `moa-traces/*.jsonl` with bounded latest-record metadata.
- Full prompts, reference outputs, and aggregator outputs remain out of the default view.

Value:

- MoA is a headline v0.18 feature.
- Operators can tell whether MoA is configured, being used, and generating debug artifacts.

Risk:

- Medium. Trace files can contain sensitive prompts and model outputs. Initial implementation should be counts and metadata only.

Original roadmap phase:

- Phase 1 / P1 for config and inventory.
- Phase 2 for optional privacy-safe detail summaries.

### 3. First-class Projects visibility

Status: implemented in Operations.

Hermes Agent Desktop added per-profile Projects backed by `projects.db`. The source shows explicit project, folder, metadata, and discovered repo tables.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/projects_db.py`

Implemented hermesd behavior:

- Reads `~/.hermes/projects.db` read-only and shows:
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

Original roadmap phase:

- Phase 1 / P1.

### 4. Gateway lifecycle, drain, and scale-to-zero state

Status: implemented in Gateway.

hermesd currently shows `gateway_state`, `active_agents`, platform errors, and restart markers. Hermes Agent now has explicit drain coordination and scale-to-zero behavior. The local source shows `.drain_request.json`, busy/drainable derivation, and scale-to-zero helpers.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/gateway/drain_control.py`
- `/Users/mudrii/src/hermes/hermes-agent/gateway/status.py`
- `/Users/mudrii/src/hermes/hermes-agent/gateway/scale_to_zero.py`

Implemented hermesd behavior:

- Reads `~/.hermes/.drain_request.json` safely.
- Shows served profiles, external drain marker, busy/drainable state, scale-to-zero idle timeout, and relay-only intent.

Value:

- High for hosted/team operators.
- Makes restarts, dormancy, and maintenance windows explainable.

Risk:

- Medium. Avoid overclaiming why a gateway is draining unless the marker/config makes it clear.

Original roadmap phase:

- Phase 1 / P1.

### 5. Multi-board Kanban and typed blockers

Status: implemented in Kanban.

hermesd reads the root `~/.hermes/kanban.db` and already surfaces tasks, runs, links, attachments, workers, and metadata. Hermes Agent has expanded multi-board behavior under `kanban/boards/<slug>/kanban.db`, current-board resolution, and typed block reasons.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/kanban_db.py`
- Current hermesd reader: `/Users/mudrii/src/hermes/hermesd/hermesd/collector.py`
- Current hermesd models: `/Users/mudrii/src/hermes/hermesd/hermesd/models.py`

Implemented hermesd behavior:

- Discovers `~/.hermes/kanban/boards/*/kanban.db` without following symlinks.
- Reads current board state when present.
- Shows per-board task/run/problem counts, stale claims, and typed blocker counts while preserving default-board behavior.

Value:

- High for project-based Hermes use.
- Connects Desktop Projects, kanban boards, and background workers.

Risk:

- Medium. Multiple databases increase read cost and need bounded iteration.

Original roadmap phase:

- Phase 1 / P1.

### 6. Journey and learning graph summary

Status: implemented as a lightweight Memory learning summary.

Hermes Agent v0.18.0 added `/journey` and a memory graph. The source builds this from skill metadata, usage, and memory files.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/hermes_cli/journey.py`
- `/Users/mudrii/src/hermes/hermes-agent/agent/learning_graph.py`

Implemented hermesd behavior:

- Reads only persisted files:
  - `skills/.usage.json`,
  - learned/profile skill metadata,
  - `memories/MEMORY.md`,
  - `memories/USER.md`.
- Shows:
  - learned skill count,
  - pinned skill count,
  - agent-created skill count,
  - usage-bearing skill count,
  - memory card count.

Value:

- Useful for visibility into the self-improvement loop.
- Complements the existing Curator panel.

Risk:

- Medium. Do not import `agent.learning_graph`; mirror only the minimum stable parsing hermesd needs.

Original roadmap phase:

- Phase 2 / P2.

### 7. Cron provider, Chronos, and automation blueprint visibility

Status: implemented in Cron and Config.

hermesd already has a Cron panel for jobs and recent runtime metadata. Hermes Agent added provider-based cron scheduling, Chronos managed cron for scale-to-zero deployments, and Automation Blueprints.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/cron/scheduler_provider.py`
- `/Users/mudrii/src/hermes/hermes-agent/docs/chronos-managed-cron-contract.md`
- `/Users/mudrii/src/hermes/hermes-agent/cron/blueprint_catalog.py`
- `/Users/mudrii/src/hermes/hermes-agent/cron/suggestions.py`

Implemented hermesd behavior:

- Shows active scheduler provider, Chronos managed-cron config presence, and persisted cron suggestion counts.
- Keeps blueprint authoring out of hermesd.

Value:

- Important for hosted scale-to-zero cron reliability.
- Good fit for hermesd's operational posture.

Risk:

- Low for config presence.
- Medium if trying to infer provider health from logs.

Original roadmap phase:

- Phase 2 / P2.

### 8. Channel directory and new messaging adapter detail

Status: implemented in Gateway.

Hermes Agent v0.17/v0.18 expanded platform coverage and media support, including WhatsApp Cloud/Baileys changes, Teams media, iMessage Photon, Raft, and platform aliasing. hermesd already shows platform states from `gateway_state.json` and directory entries.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/gateway/channel_directory.py`
- `/Users/mudrii/src/hermes/hermes-agent/gateway/platforms/whatsapp_cloud.py`

Implemented hermesd behavior:

- Reads `channel_aliases.json` if present.
- Shows alias count and stale alias warnings.
- Adds labels for known platform families:
  - WhatsApp Cloud,
  - WhatsApp Baileys,
  - Teams,
  - Photon/iMessage,
  - Raft,
  - Slack, Discord, Telegram, Matrix, etc.
- Flags platforms present in gateway state but absent from channel directory.

Value:

- Helps explain multi-platform gateway behavior without needing the Hermes dashboard.

Risk:

- Low if limited to persisted state.

Original roadmap phase:

- Phase 2 / P2.

### 9. Curator scheduler state

Status: implemented in Curator.

hermesd already has a strong Curator panel for the newest run report. Hermes Agent also has curator scheduler state and consolidation config.

Relevant local source:

- `/Users/mudrii/src/hermes/hermes-agent/agent/curator.py`
- Current hermesd reader: `/Users/mudrii/src/hermes/hermesd/hermesd/collector.py`

Implemented hermesd behavior:

- Reads `skills/.curator_state` safely if present and shows:
  - paused state,
  - last run timestamp,
  - run count,
  - last report path,
  - consolidation enabled/disabled from config.

Value:

- Completes the existing Curator panel rather than creating a new surface.

Risk:

- Low.

Original roadmap phase:

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

## Implementation completion and future roadmap

### Completed in this branch

- Phase 1 visibility: verification/goals, MoA summary, Projects summary, gateway lifecycle, and multi-board Kanban.
- Phase 2 visibility: learning summary, Cron/Chronos config health, channel aliases/adapter labels, and Curator scheduler state.
- Resilience and safety: schema-tolerant SQLite reads, symlink/path safety for new file readers, malformed JSON tolerance, cache preservation, markup escaping, and snapshot coverage.
- Release hardening: locked CI installs, pinned Actions/uv versions, wheel and sdist smoke installs, release tag/changelog/artifact guards, and Dependabot tracking.

### Remaining future work

1. Deepen Projects/Kanban/verification links into a dedicated correlation view.
2. Correlate MoA trace inventory with provider/model cost summaries without exposing trace contents.
3. Add dashboard/snapshot views that answer "what changed after the latest Hermes update?"

## Test plan

The completed implementation used TDD for each slice. Keep the same test style for future correlation work.

Implemented or still-relevant test fixtures:

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

| Rank | Candidate | Priority | Status |
|---:|---|---|---|
| 1 | Verification and goals | P0 | Implemented |
| 2 | MoA config and trace inventory | P1 | Implemented |
| 3 | Projects summary | P1 | Implemented |
| 4 | Gateway drain / scale-to-zero | P1 | Implemented |
| 5 | Multi-board Kanban | P1 | Implemented |
| 6 | Learning graph summary | P2 | Implemented as lightweight persisted-file summary |
| 7 | Cron provider / Chronos | P2 | Implemented as config/suggestion visibility |
| 8 | Channel aliases and adapter detail | P2 | Implemented |
| 9 | Curator scheduler state | P2 | Implemented |

## Bottom line

hermesd is now aligned with the high-signal Hermes Agent v0.17/v0.18 runtime surfaces: verification events, goal contracts, MoA configuration/traces, Projects, gateway lifecycle, multi-board Kanban, learning summary, Cron/Chronos config, channel aliases, and Curator scheduler state.

The next most valuable additions are deeper cross-panel correlations and "what changed after update" summaries. Those should stay read-only, avoid Hermes Agent imports, and continue to monitor persisted runtime artifacts rather than duplicating Hermes Desktop authoring surfaces.
