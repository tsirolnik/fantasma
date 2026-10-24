# Lab workflow profile

Lab is the sequential workflow shipped in [profiles/lab.toml](../profiles/lab.toml).
Select it like any other profile:

```bash
python -m profiles inspect lab
python main.py full --profile lab --targets YOUR_AUTHORIZED_DOMAIN \
  --director-llm claude --worker-llm claude --analyzer-llm claude --budget 1
```

The second command starts a real run. `full --mode lab` without a profile
loads this same file. Lab is opt-in; the default is the interactive pentester. The legacy mode
flag remains supported. Explicit CLI flags still override profile values.

## Configure the workflow

```toml
[profile]
name = "scan-only"

[topology]
mode = "lab"

[target]
provider = "direct"

[workflow]
stages = ["scan"]
worker_mode = "auto"
max_scan_targets = 10

[llm.director]
provider = "claude"

[llm.default_worker]
provider = "claude"
```

Save as `my-lab.toml`, inspect it, then pass `--profile my-lab.toml`. Profiles do
not inherit from one another; omitted fields use loader defaults.

| Setting | Default | Contract |
| --- | --- | --- |
| `stages` | `["recon", "scan", "analyze", "report"]` | Ordered subset of these stages; `scan` is required; duplicates and reordered stages fail validation |
| `worker_mode` | `auto` | `auto` chooses tool workers if the recon provider supports tools; `tool` requires that support; `deterministic` selects the fixed worker implementations |
| `max_scan_targets` | `50` | Positive integer limiting targets passed to the scan worker per program |

Discovery is handled by `[target]`, before the per-program stages. Use `direct`
to require explicit targets or `auto` for direct targets with discovery fallback.
Custom target provider classes work here too. LLM overrides use the lab roles
`discover`, `recon`, `scanner`, `analyzer`, `reporter` and `director`.

`deterministic` describes the worker implementation, not a no-cost run: Director
and reflection/analysis calls may still use models. `--dry-run` selects the
existing no-model worker path even with `worker_mode = "tool"`; it is not a
promise that no tools or network requests execute.

## Interact with the lab

Add `[interaction] mode = "terminal"` to this profile to wait for `/start`, send
Director guidance, use pause/resume/stop, and respond to checkpoints without a
competing blocking input prompt. `mode = "web"` selects the dashboard.
See [interaction](interaction.md) and the [terminal lab example](../examples/profiles/terminal-lab.toml).

## Stage data flow

- Recon receives domains and scope context. Its output feeds scan, session tags
  and optional graph updates. Without recon, scan receives the original domains
  and an empty recon-data dictionary.
- Scan receives capped targets and scope/context. Director skip/abort or no
  findings ends the program before later stages.
- Analyze receives scanner findings. Without analyze, raw findings pass directly
  to persistence and any report stage. This does not make them verified. The
  session's legacy `analyzed_findings` collection also holds these passthrough results.
- Report receives the retained findings and cost estimates. Without report, no
  report worker or pre-report human checkpoint executes; findings are still saved.

Stages retain their existing Python implementations and data contracts. This is
not an arbitrary DAG engine: dependency order is validated, custom stage classes
and arbitrary reorderings are not supported. The [Python extension reference](reference/extensions.md)
describes the available extension points.

Swarm mechanism hooks, swarm coordination/scoring and the pentester methodology
are not applied to these sequential workers. Use a swarm profile for those
features. Lab supports target-provider selection, workflow settings and role LLMs.
The inspection output and dashboard profile view include the lab workflow.

Source: [profile loader](../profiles/loader.py), [workflow schema](../profiles/__init__.py),
[command assembly](../commands/full.py), [stage execution](../agents/pipeline.py).
