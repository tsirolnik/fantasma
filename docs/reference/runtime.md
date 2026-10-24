# Runtime, lifecycle and persistence reference

This page describes the current implementation, including behavior that differs
from class comments or the intended design. It is not a guarantee of a stable API.
Return to the [documentation index](../README.md).

## Interactive default

The default [terminal session](../interactive.md) constructs a pentester lazily
on the first user request. It has conversation history and in-session shared
memory, with no Director, validator or automatic scan. `/stop` cancels active
work; completed actions are not rolled back. The sections below describe the
explicit batch/swarm path rather than that interactive lifecycle.

## Assembly and ownership

| Component | Responsibility | Source |
| --- | --- | --- |
| `commands.full.run` | Load profile, apply explicit flags, choose lab or swarm | [commands/full.py](../../commands/full.py) |
| `SwarmPipeline` | Target selection, shared services, teams, persistence, finalization | [agents/swarm_pipeline.py](../../agents/swarm_pipeline.py) |
| `AgentTeam` | Spawn workers, monitor progress, apply Director decisions, replace workers | [agents/team.py](../../agents/team.py) |
| `AgentGroup` | Construct multiple teams, merged auditor view and cross-team broadcasts | [agents/group.py](../../agents/group.py) |
| `Director` | Review team summaries and return redirects / score adjustments | [agents/director.py](../../agents/director.py) |
| `PenTesterAgent` / `ToolAgent` | Build context, call model, select and execute tools | [pentester.py](../../agents/pentester.py), [tool_agent.py](../../agents/tool_agent.py) |
| `SharedMemory` | Team feed, claims, submission gates, finding objects, scores and hook effects | [memory/shared.py](../../memory/shared.py) |
| `ValidatorAgent` | Re-test findings and update verification fields | [agents/validator.py](../../agents/validator.py) |

The Director, MessageBus, CostTracker, persistence and browser pool are shared
services. Each team has its own SharedMemory and mechanism instances. Browser
sessions, raw observations, distilled facts and leads belong to individual agents.
There is no separate TeamLead LLM in this path: `AgentTeam` performs the monitoring
and calls the shared Director. Multi-team mode is `num_teams > 1` on swarm.

## Worker turns

`ToolAgent.execute` drives the loop. Pentesters enable rebuilt context: each turn
uses scope, cost/budget, dead ends, distilled facts, findings, ranked leads and
recent tool results. Director directives enter raw notes and subsequently the
distillation path. They are not guaranteed to appear verbatim immediately in the
next briefing. Distillation can itself call a model.

Both rebuilt context and history context call the progress digest, which invokes
`SharedMemory.turn_lines()` and applies mechanism turn effects. Workers also
consult shared work through tools; SharedMemory is not a shared model conversation.
Tool results update local state before the next model action.

A methodology changes the system prompt. The default turn briefing still contains
bug-bounty wording, goals and excluded classes from `agents/turn_context.py`.
Selecting a different methodology alone does not replace that briefing. Supply
`turn_template` when changing those instructions. This is a current boundary on
the claim that the engine is domain-independent.

## Monitor cadence and replacement

| Operation | Actual trigger |
| --- | --- |
| Team monitor tick | `director_review_interval_s` seconds (default 30), plus work performed in the tick |
| Director review | Every second team tick; nominally 60 seconds with defaults |
| Periodic team callback | Every fourth tick; nominally 120 seconds |
| Validator/auditor launch | A monitor tick sees findings and still has active tasks; not an immediate submission callback |
| Worker termination | Score is **less than or equal to** `termination_threshold` when checked by monitor |
| Replacement | At most two replacements per original slot; old claims released and browser session closed |
| Group monitor | Nominal 60-second ticks; budget/stop checks and periodic callbacks |
| Cross-team intelligence | Every third group tick, when sharing is enabled |

These are cooperative asyncio tasks, not hard scheduling deadlines. Stale-finding
checks log warnings after five minutes without progress; they do not alone prove
the worker is terminated. A finding arriving just as all tasks finish may never
receive a validator run. The group cancels a still-running auditor when teams end.
See `AgentTeam._monitor`, `AgentGroup._group_monitor` and `AgentGroup.run` in the
sources above.

## Findings and scoring order

1. `SharedMemory.submit_finding` applies quality/scope checks and duplicate logic.
2. Base scoring applies the acceptance, rejection or duplicate delta.
3. Accepted findings enter the team's in-memory collection. The persistence
   callback attempts SQLite and optional graph writes before returning.
4. `on_submit` mechanisms apply extra score deltas and feedback. Rejections and
   duplicates also trigger the hook; rejected duplicates have `reason=None`.
5. If launched, the validator updates the mutable finding's verification fields.

Acceptance is not independent verification. Persistence failures are logged and
can leave an accepted in-memory finding without a successful durable write. A
verification update does not automatically repeat the submission persistence
callback. Reports may contain unverified or inconclusive findings.

Within a team, deduplication uses target, vulnerability type and normalized URL:
query/fragment are removed and numeric/UUID path segments are collapsed. The
merged auditor view instead deduplicates by target/type/raw URL and returns a
representative object. Updating that object does not update every duplicate copy
held by other teams. See `SharedMemory.submit_finding` and
`AgentGroup._create_merged_shared_memory`.

Claim keys have one recorded owner in SharedMemory even with shared coordination.
`claim_mode = "shared"` changes whether another worker's claim blocks tool use;
it does not turn the claims dictionary into a multi-owner structure. Claim
mechanisms receive the actual success boolean from that dictionary operation.

At normal finalization, findings are collected, run-end mechanisms execute,
optional chain synthesis runs when eligible, and the Director debrief/report
stages follow. Stop/exit and insufficient findings can skip stages. Run-end hooks
execute once per mechanism name across team memories; order can affect scores.

## Storage and recovery

| Store | Contents | Location / lifetime |
| --- | --- | --- |
| SharedMemory | Feed, claims, finding objects, score events | In process, one per team |
| Mechanism instance | Custom counters/timers/sets | In process; not serialized by checkpoint capture |
| SQLite | Programs, findings, scans, costs, key/value state | `./data/memory.db`, or `SWARMHOUND_DB_PATH` |
| Checkpoints | Programs/domains, one shared-memory snapshot, session, spent cost and phase | SQLite key/value entries `checkpoint:<scan_id>` and `checkpoint:latest` |
| FalkorDB | Optional relationships and scan context | Configured graph service; can be disabled |
| Logs | Run, web and LLM wire output | `data/logs/` |
| Reports | Generated Markdown | `OUTPUT_DIR`, default `./output` |

`SWARMHOUND_DB_PATH` is read directly by [memory/store.py](../../memory/store.py),
so it is not a `Settings` field in the generated environment table. Export it in
the process environment when isolating runs.

[Checkpoint capture](../../memory/checkpoint.py) does not serialize arbitrary
Python mechanism state, browser sessions, in-flight calls, or a full multi-team
execution graph. `SwarmPipeline._build_checkpoint` captures its single
`shared_memory` reference; multi-team continuation is not a complete topology
restore. Reapply profile and provider settings when resuming. See the
[CLI reference](cli.md) for `--resume`, `--resume-scan`, and `--list-checkpoints`.

## Execution boundaries

Tool execution checks scope and claim ownership in the host process. Recognized
hosts can be checked, but arbitrary code, redirects and browser subrequests are
not comprehensively constrained by a network firewall. Docker tools use
resource-limited containers; HTTP/browser tools execute in the host process.
Custom Python extensions also execute in the host process.

Budgets are shared estimates based on reported usage and configured pricing.
Concurrent calls can overshoot. Dashboard pause is cooperative, not a rollback
of completed or in-flight external actions. See the [dashboard reference](dashboard.md)
and [README boundaries](../../README.md#boundaries).
