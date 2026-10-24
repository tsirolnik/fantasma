# Interaction is a profile capability

A profile selects **what runs** with `[topology]` and **how you interact with it**
with `[interaction]`. Dashboard availability is independent: add `--web` or
`[dashboard] enabled = true` to any profile, including terminal-controlled runs. A conversational pentester is a separate execution
style that returns control after answering each request.

```toml
[topology]
mode = "swarm"
agents_per_team = 2

[interaction]
mode = "terminal"
```

Use [terminal-swarm.toml](../examples/profiles/terminal-swarm.toml) or
[terminal-lab.toml](../examples/profiles/terminal-lab.toml) as complete examples:

```bash
python -m profiles inspect examples/profiles/terminal-swarm.toml
python main.py full --profile examples/profiles/terminal-swarm.toml --budget 1
```

In the console, set `/scope YOUR_AUTHORIZED_HOST`, then `/start`. The example uses
a direct target provider, so it needs explicit targets. No investigation starts
before `/start`, although pipeline services/providers are constructed at startup.
The default conversational pentester still constructs its agent lazily.

## Supported combinations

| Topology | `terminal` | `batch` | Optional dashboard (`--web`) |
| --- | --- | --- | --- |
| `pentester` | Conversational requests, retained context, redirects and cancellation | Not implemented; rejected | Shared conversation, scope, stop and status |
| `swarm` | Start/control a run, direct workers, Director messages, checkpoints | Existing run/checkpoint workflow | Existing dashboard controls and messages |
| `lab` | Start/control sequential stages, Director guidance, checkpoints | Existing sequential workflow | Existing dashboard |

`interaction.mode` defaults to `batch`. The default pentester profile selects
`terminal`. Legacy `topology.mode = "interactive"` resolves to pentester/terminal.
`--interaction terminal|batch` overrides the profile's interaction. The dashboard
is supported for all profiles but starts only when requested; it is not a separate
execution or interaction mode.

```toml
[interaction]
mode = "terminal"
[dashboard]
enabled = true
```

`--web` enables the dashboard without replacing terminal interaction. Legacy
`interaction.mode = "web"` or `--interaction web` maps to an enabled dashboard
with batch interaction for swarm/lab, or terminal interaction for pentester.
A profile can override the legacy implied dashboard setting explicitly.

`--auto` skips human checkpoints on swarm/lab but does not change the interface
or automatically start a terminal-controlled run. Pentester rejects `--auto`.
`interaction.max_iterations` and `max_tool_calls` are per-request pentester limits;
putting them in a swarm/lab profile is rejected rather than silently ignored.

## Swarm and lab terminal commands

| Input | Behavior |
| --- | --- |
| `/start` | Start one run with the current targets/configuration |
| `/scope HOST[,HOST]` | Set explicit target hosts before starting; empty restores provider-based target selection |
| Plain text | Queue a directive/question for the Director's next evaluation |
| `/tell AGENT MESSAGE` | Deliver a redirect to a registered tool agent, or to `director` |
| `/status` | Show lifecycle, target selection, spend and budget |
| `/pause`, `/resume` | Cooperative controls; lab pauses between stages, swarm workers check pause events |
| `/budget USD` | Set a finite positive effective budget; in-flight calls may overshoot |
| `/continue`, `/skip` | Resolve a pending human checkpoint |
| `/stop` | Stop/cancel the run and await cleanup |
| `/quit` or EOF | Stop work and close the console |

Scope is fixed after `/start`. A completed or stopped pipeline cannot be restarted
in that console; start another process for a new run. Completed target actions,
findings and stored data are not rolled back. `/stop` is not equivalent to
reversing executed tools.

Terminal-controlled pipelines retain their execution behavior. Sending a question
to the Director does not create an instant general-purpose chat response: it is
consumed at the next phase/team review, and swarm replies appear when the Director
returns a human response. For immediate conversational turns, use the default
[pentester session](interactive.md). Fixed lab workers do not support tool-loop
redirects; use Director guidance for them. An unavailable `/tell` recipient is
reported rather than silently discarded.

The existing dashboard remains the interface for its browser-watch and human-help
features; terminal control does not implement every dashboard widget. The reader
currently relies on macOS/Linux asyncio file-descriptor support.

Sources: [profile schema](../profiles/__init__.py), [loader](../profiles/loader.py),
[dispatch](../commands/full.py), [pipeline console](../agents/pipeline_console.py),
[conversational session](../agents/interactive.py), [dashboard](reference/dashboard.md).

When both surfaces are open, terminal-controlled swarm/lab checkpoints remain
in the terminal (`/continue`, `/skip`); dashboard monitoring, messages and controls
remain available. Terminal `/quit` or EOF closes the run and its dashboard.
