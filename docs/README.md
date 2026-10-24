# swarmhound documentation

swarmhound composes an authorized investigation from target, methodology, model,
coordination and incentive components. This documentation describes the current
repository implementation. Experimental features and gaps are called out where
relevant; it is not a promise that every profile field applies to every engine.

## Learn and run

Start with the [interactive pentester](interactive.md), the default user-led terminal
session. Batch swarm and lab workflows are explicit profile choices.

1. [Setup and first custom mechanism](getting-started.md) — install dependencies,
   copy an extension outside the repo, test without model calls, then run it.
2. [Profile configuration](configuration.md) — choose components, understand
   defaults and CLI precedence, inspect a profile before running it.
3. [Writing mechanisms](mechanisms.md) — examples, all hook contracts, conditions,
   state lifetime and constructor parameters.
4. [Local benchmark](../benchmark/README.md) — managed local targets and result interpretation.

## Reference

| Reference | Contents |
| --- | --- |
| [Architecture](architecture.md) / [browser diagrams](architecture.html) | Component composition, execution feedback and source traceability |
| [Runtime and persistence](reference/runtime.md) | Ownership, monitor timing, replacement, finding lifecycle, storage and checkpoint limits |
| [Interaction](interaction.md) | Terminal, web and batch interfaces across execution topologies |
| [Lab workflow](lab.md) | Sequential stage selection, worker mode, target selection and compatibility |
| [CLI](reference/cli.md) | Every registered command and its arguments, generated from argparse |
| [Environment/settings](reference/settings.md) | Every declared Settings field, default, description and constraints |
| [Python extension APIs](reference/extensions.md) | Mechanism, Methodology, TargetProvider, Tool/ToolRegistry and turn templates |
| [Finding model](reference/findings.md) / [JSON schema](reference/finding.schema.json) | Fields, defaults, required values, enums and nested data |
| [Dashboard API](reference/dashboard.md) | Authentication, routes and control messages |

## Examples

| Example | Purpose |
| --- | --- |
| [coverage.py](../examples/mechanisms/coverage.py) + [profile.toml](../examples/mechanisms/profile.toml) | Stateful custom Python mechanism configured from TOML |
| [conditional.toml](../examples/mechanisms/conditional.toml) | Filter hook calls by outcome, target and severity; disable an entry |
| [bugbounty.toml](../profiles/bugbounty.toml) | Single-team baseline |
| [competitive.toml](../profiles/competitive.toml) | Multiple teams and incentives |
| [redteam.toml](../profiles/redteam.toml) | Direct targets and a different system methodology |
| [Methodology and tool examples](reference/extensions.md) | Implement other Python extension points |

## Maintaining the references

From the repo root with the virtual environment active:

```bash
python scripts/generate_reference.py
python scripts/generate_reference.py --check
python -m unittest discover -s tests
```

The generator reads class and parser declarations, never live settings or
credentials. It updates CLI, settings and finding schema references. The `--check`
mode exits nonzero when those outputs differ from their declarations. Narrative
runtime and architecture pages still require source review after behavior changes.
Architecture Markdown is the source for the embedded text in `architecture.html`;
keep both synchronized. Diagram rendering requires the HTML page's CDN assets.

MCP plugins: see [setup, IDA connection examples, and configuration reference](mcp.md).
