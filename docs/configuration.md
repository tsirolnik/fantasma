# Profile configuration reference

Start with the [README setup](../README.md), then choose a shipped profile or
copy one to a directory you control. A profile composes strategies and policies;
it is not a script executed by the agents. Python extensions implement behavior.

```bash
fantasma profiles inspect bugbounty
fantasma full --profile bugbounty --targets YOUR_AUTHORIZED_DOMAIN \
  --director-llm claude --worker-llm claude --budget 1
```

The first command inspects a trusted profile without starting a run. The second
starts a real run. TOML can import Python extensions, including during inspection.

## Sections and defaults

| Section | Keys and defaults | What consumes it |
| --- | --- | --- |
| `profile` | `name` defaults to file stem; `description = ""` | Display/identification |
| `methodology` | `type = "pentest"`; other keys go to constructor | Swarm worker instructions; built-ins honor `extra_instructions`; rebuilt briefings honor `turn_template` |
| `target` | `provider = "auto"`; other keys go to constructor | Lab/swarm target selection; `auto` uses direct targets when supplied, otherwise discovery; `direct` requires `--targets` |
| `topology` | `mode = "swarm"`, `agents_per_team = 4`, `num_teams = 1` | Engine selection and swarm size; multiple teams use swarm mode |
| `interaction` | `mode = "batch"`; pentester-only `max_iterations = 20`, `max_tool_calls = 50` | Interface selection independent of topology; see [interaction](interaction.md) |
| `workflow` | `stages`, `worker_mode`, `max_scan_targets`; see [lab](lab.md) | Sequential lab workflow only |
| `mechanisms` | `enabled = []`; optional `[[mechanisms.items]]` entries | Additive swarm incentive hooks; see [mechanisms](mechanisms.md) |
| `scoring` | `default_accepted = 10`, `rejected = -10`, `duplicate = -5`, `termination_threshold = -50` | Shared-memory base scoring and worker termination |
| `scoring.accepted` | critical 50, high 30, medium 15, low 5 | Severity rewards; a supplied table replaces the whole map, omitted severities use `default_accepted` |
| `coordination` | `claim_mode = "exclusive"`, `director_review_interval_s = 30`, `cross_team_sharing = true` | Swarm claims/review; cross-team sharing matters only with multiple teams |
| `llm.director` | `provider = "claude"`, `model = ""` | Director provider/model |
| `llm.default_worker` | `provider = "ollama"`, `model = ""` | Default worker provider/model |
| `llm.overrides.<agent_name>` | Each table has `provider` and `model` | Role override by lookup key, not necessarily runtime agent ID; a missing provider defaults to Ollama |

An empty model string selects the provider's configured default. Provider
credentials, endpoint URLs and pricing come from settings/environment; see
[`.env.example`](../.env.example). The global `LLM_PROVIDER` setting is not a
replacement for the Director and worker role configuration.

Methodologies and target providers accept `module.path:ClassName`; mechanisms
also accept `./file.py:ClassName` relative to the profile. Extra methodology and
target keys are passed through to constructors, but have no effect unless that
implementation reads them. Loading a profile does not prove that all its
arbitrary keys affect execution.

## Precedence and engine differences

- Explicit `--mode`, `--swarm-agents`, and `--num-teams` override profile topology.
- `--mechanisms` replaces the entire profile mechanism list. It accepts built-in
  names only; use profile tables for parameters, conditions or custom classes.
  `--mechanisms ""` disables all mechanisms.
- An explicit profile's empty mechanism list also disables mechanisms. Only when
  neither a profile nor `--mechanisms` supplies a list does `MECHANISMS` apply.
- Explicit Director/worker role flags replace that role's profile configuration.
  Setting only `--director-model`, for example, uses the CLI role's default
  provider (Claude), not a different provider from the profile. Pass both provider
  and model flags when overriding a role. Named role overrides remain more
  specific than the default-worker configuration.
- Omitting both `--profile` and `--mode` loads `profiles/interactive.toml`.
  `--mode lab` loads `lab`; `--mode swarm` loads `bugbounty` (four workers).
  `--profile bugbounty` explicitly selects swarm.
- In the current `full` command, lab consumes target-provider, workflow and role LLM configuration. Swarm
  methodology, scoring, mechanisms and coordination do not drive its sequential
  workers. See the [lab workflow reference](lab.md).

`fantasma profiles inspect PATH` shows resolved profile values, implemented
active hooks and filters. It does not combine a future `full` invocation's CLI
flags, credentials or provider defaults, and it does not validate provider access.
It warns about lab mode's unsupported swarm settings.

## Examples and troubleshooting

- [Basic custom mechanism](../examples/mechanisms/profile.toml): adjacent Python
  file with a tunable constructor parameter.
- [Conditional mechanism](../examples/mechanisms/conditional.toml): only accepted
  high/critical submissions for a named target; another mechanism is disabled.
- [Bug-bounty profile](../profiles/bugbounty.toml): single-team baseline.
- [Red-team profile](../profiles/redteam.toml): direct targets and different instructions.

If an incentive seems inactive, inspect its `active_hooks` and `when` conditions,
check that you selected swarm, and check for `--mechanisms` overriding the list.
Acceptance is shared-memory admission, not independent verification. Exact
`agent_ids` must match runtime IDs; target filters match `finding.target`, not
necessarily the hostname of the finding URL. Conditional hooks do not grant or
expand authorized target scope.

Test custom logic without a model using the [guide's small Python test](mechanisms.md#start-with-a-working-example).
For component flow and timing, read the [architecture](architecture.md).

## Role lookup keys

Swarm pentesters (including replacements) request the `scanner` role; their
runtime IDs are not used to choose an LLM. Both the single-team validator and
multi-team auditor request `validator`. Configure `[llm.overrides.scanner]` or
`[llm.overrides.validator]` for those roles. The per-agent Ollama-server path
constructs its provider separately. See [team spawning](../agents/team.py),
[validator spawning](../agents/swarm_pipeline.py), and [auditor spawning](../agents/group.py).

`director_review_interval_s` is the team monitor tick interval. The Director
review occurs every second tick, not every tick. See [runtime timing](reference/runtime.md).
For all CLI options and settings declarations, use the [CLI](reference/cli.md)
and [environment](reference/settings.md) references.

The default interactive session uses the `scanner` role and waits for user prompts.
It consumes methodology, mechanisms and interaction limits, and takes scope from
`--targets` or `/scope`; it does not invoke the target provider. See [interactive](interactive.md).

## Name compatibility

Fantasma retains the `swarmhound` command as an alias. Existing profiles and
local data paths are unchanged. Prefer `FANTASMA_DB_PATH`,
`FANTASMA_TOOL_NETWORK`, and `FANTASMA_TOOL_HOST_REWRITE`; the corresponding
`SWARMHOUND_*` variables still work when the new names are unset.
