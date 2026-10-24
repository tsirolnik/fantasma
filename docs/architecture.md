# swarmhound — A configurable engine with runtime feedback

The framework is **assembled from a profile**, then **adapts while it runs**.
Bug-bounty research is one configuration of the swarm engine. The diagrams below
show both kinds of dynamism: component selection and feedback during execution.

## 1. Compose the run

Purple components support built-in names or custom Python classes. Gold nodes
configure behavior and topology. Dotted arrows describe configuration applied
when constructing a run—not live hot-swapping.

```mermaid
flowchart TB
    PROFILE["TOML profile + explicit CLI overrides"] --> LOAD["Resolve components and parameters"]
    LOAD -. "Select / instantiate" .-> TARGET["Target provider<br/>Built-in or module:ClassName + kwargs"]
    LOAD -. "Select / instantiate" .-> METHOD["Methodology<br/>Built-in or module:ClassName + kwargs"]
    LOAD -. "Compose / tune" .-> MECH["Mechanisms<br/>Names or custom classes + parameters"]
    LOAD -. "Configure" .-> POLICY["Scoring + coordination policies<br/>Rewards, thresholds, claims, review cadence"]
    LOAD -. "Configure" .-> TOPO["Topology + per-role LLMs<br/>Team count, worker count, provider / model"]
    TARGET -->|"Resolve scope"| ENGINE["Swarm orchestration engine"]
    METHOD -->|"System prompt + optional turn template"| ENGINE
    MECH -->|"Event hooks"| ENGINE
    POLICY -->|"Rules for scoring and coordination"| ENGINE
    TOPO -->|"Construct teams and agents"| ENGINE
    ENGINE --> TEAM["AgentTeam × configured team count<br/>One shared Director, separate team memories"]
    classDef extension fill:#eef2ff,stroke:#6366f1,color:#20204a;
    class TARGET,METHOD,MECH extension;
    classDef config fill:#fff4df,stroke:#d18b17,color:#523600;
    class PROFILE,LOAD,POLICY,TOPO config;
```

| Change in the profile | What changes in the running system |
|---|---|
| `[target] provider` and extra fields | How scope is obtained; custom provider classes receive extra fields as constructor arguments |
| `[methodology] type` and extra fields | How workers are instructed to reason; built-ins support additional instructions and a turn-template override |
| `[mechanisms] enabled` / `[[mechanisms.items]]` | Which event-driven incentives run and their parameters; custom mechanism classes are supported |
| `[scoring]` | Accepted/rejected/duplicate scores and the worker termination threshold |
| `[coordination]` | Exclusive/shared claims, monitor cadence, cross-team intelligence sharing |
| `[topology]` | Team count and workers per team; multi-team runs use AgentGroup on the same swarm engine |
| `[llm.director]`, `[llm.default_worker]`, `[llm.overrides.*]` | Provider and model selection by role or agent |

For example, `profiles/competitive.toml` selects three teams of three workers,
shared claims, tuned mechanism bonuses, and distinct Director/worker models.
`profiles/bugbounty.toml` selects one team of four workers without additive
mechanisms. Those are data choices, not separate orchestration implementations.

## 2. Adapt during execution

Green nodes form feedback loops. The worker does not follow a fixed scanner
sequence: it chooses the next tool from the latest state. Monitoring and
validation run concurrently with that loop.

```mermaid
flowchart TB
    CONTEXT["Rebuild worker context<br/>Scope, budget, facts, leads, last results"] --> LLM["Worker LLM chooses next action"]
    LLM --> TOOL["Tool registry + execution checks"]
    TOOL --> EXEC["HTTP / browser / Docker / code<br/>Interact with target systems"]
    EXEC -->|"Result / error / evidence"| STATE["Agent-local observations and distilled state"]
    STATE -->|"Next turn"| CONTEXT

    TOOL <-->|"Memory tools: claim, read, post, submit"| MEMORY[("Per-team SharedMemory<br/>Feed, claims, findings, scores")]
    MEMORY -->|"Submission / claim events"| HOOK["Configured mechanism hooks<br/>Additional rewards, penalties, feedback"]
    HOOK -->|"Score effects / messages"| MEMORY

    MEMORY -->|"Progress and scores"| MON["Team monitor"]
    STATE -->|"Recent tool activity"| MON
    MON -->|"Periodic review"| DIR["Shared Director"]
    DIR -->|"Redirects + score changes"| MON
    MON -->|"Directive queue → worker notes"| CONTEXT
    MON -->|"Apply score changes"| MEMORY
    MON -->|"Score below configured threshold"| REPLACE["Cancel worker, release claims<br/>Spawn replacement within slot limit"]
    REPLACE -->|"New worker uses prior shared work"| CONTEXT

    MEMORY -->|"First finding launches verification"| VAL["Validator / cross-team auditor"]
    VAL -->|"Re-test with tools"| EXEC
    EXEC -->|"Verification evidence"| VAL
    VAL -->|"Update finding verdict in memory"| MEMORY

    MEMORY -->|"Accepted submission"| STORE[("SQLite + optional graph<br/>Persist before verification")]
    MEMORY -->|"Run ends"| END["Run-end mechanism hooks<br/>Collect findings, synthesize chains, debrief"]
    END --> REPORT["ReportWorker → Markdown reports"]
    classDef feedback fill:#e6f5ee,stroke:#338466,color:#133c2b;
    class CONTEXT,STATE,MON,DIR,REPLACE feedback;
    classDef extension fill:#eef2ff,stroke:#6366f1,color:#20204a;
    class HOOK extension;
```

**What changes live:**

- Tool outcomes update observations, repeated-endpoint tracking and distilled
  facts/leads. The next worker prompt is rebuilt from that state.
- Director reviews return redirects and score adjustments. Team monitors apply
  them; directives enter worker notes for context distillation.
- Accepted/rejected findings and claims trigger the configured mechanism hooks.
  Scores can influence subsequent behavior and worker lifecycle.
- Low-scoring workers can be terminated and replaced. Claims are released and a
  replacement is instructed to consult prior shared work; respawns have a limit.
- The first finding starts a validator or cross-team auditor. Its verdict updates
  the same in-memory finding objects while workers continue.
- The dashboard can pause/resume/stop the run, change its effective budget and
  inject directives through PipelineBridge. Group monitoring checks the shared
  budget and optionally broadcasts cross-team intelligence.

**What the diagram does not imply:** profiles are not hot-reloaded mid-run;
workers do not rewrite the profile or automatically choose a new model. A
verification verdict does not itself trigger the submission persistence callback.
Report handoff is not restricted to verified findings. The group monitor does
not dynamically reallocate budget between teams.

## 3. Extension hooks and implementation boundaries

| Event / extension | Actual connection |
|---|---|
| Submission | `SharedMemory.submit_finding` → quality gates / dedup / base scoring → persistence on acceptance → `on_submit` effects |
| Claim | Shared-memory claim handling → `on_claim` effects |
| Turn | `turn_lines()` → `on_turn`; currently called by the history-based digest, **not the rebuilt-context pentester path** |
| Director review | `director_lines()` → `on_director_review` for additional review context |
| Run completion | `apply_run_end()` → configured end-of-run bonuses |
| Tool extension | `ToolRegistry.register()` / `merge()` allow tool composition in code; there is no general TOML tool-plugin loader shown here |

The optional graph supplies context through graph tools and receives findings.
An optional recorder also writes shared-memory observations to the graph; the
current multi-team recorder watches the first team only. SQLite, the graph,
MessageBus, CostTracker, Director and browser pool are shared run services.
SharedMemory, claims, scores and workers belong to individual teams. Browser
sessions and local observations belong to individual agents.

The separate `lab` engine uses discovery → recon → scan → analysis → reporting.
The composition and feedback diagrams above describe the swarm implementation;
not every profile feature is consumed by the lab path.

## Code behind the diagram

- `profiles/loader.py`: class resolution, constructor kwargs, policy and model configuration
- `commands/full.py`: CLI precedence and assembly of the pipeline
- `agents/methodology.py`, `agents/target_provider.py`: replaceable strategies
- `agents/tool_agent.py`, `agents/turn_context.py`: tool loop and state rebuilding
- `agents/team.py`: reviews, redirects, termination and replacement
- `agents/group.py`: team orchestration, auditor and cross-team intelligence
- `memory/shared.py`, `memory/mechanisms.py`: hook dispatch and scoring effects
- `agents/swarm_pipeline.py`: persistence, finalization and report handoff
- `web/bridge.py`: live human controls

Checked against the repository on **24 October 2026**.
