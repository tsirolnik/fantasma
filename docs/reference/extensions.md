# Python extension API reference

Use this page for contracts; use [getting started](../getting-started.md) for a
walkthrough. These are current Python interfaces, not a versioned compatibility
promise. Return to the [documentation index](../README.md).

## Choosing an extension point

| Desired change | Interface | Configuration |
| --- | --- | --- |
| Incentives, event feedback, stateful scoring rules | `Mechanism` | `[[mechanisms.items]]` |
| Worker system instructions | `Methodology` | `[methodology]` |
| Target discovery/selection | `TargetProvider` | `[target]` |
| Available tool implementation | `Tool` / `ToolRegistry` | Python assembly; no profile tool-plugin loader |
| Base numerical rewards | `ScoringPolicy` | `[scoring]` |
| Claim restrictions / review cadence | `CoordinationPolicy` | `[coordination]` |

## Mechanism

Source: [memory/mechanisms.py](../../memory/mechanisms.py). Subclass `Mechanism`
and define a unique `name`. The [mechanism guide](../mechanisms.md) specifies all
six hooks, return values, ordering, conditions, constructor parameters and state
lifetime. `MechanismEffect(score_delta=0, reason="", message=None)` is applied by
SharedMemory for submit/claim/turn hooks. Hooks are synchronous. Return an empty
effect to do nothing; do not return a coroutine.

Both installed `package.module:ClassName` and standalone `./file.py:ClassName`
forms work for mechanisms. Relative file paths resolve beside the profile.
`enabled=false` skips import/construction; `when` gates dispatch before the
inner hook runs. Reserved keys are `name`, `class`, `enabled`, `when`; all other
keys become constructor kwargs. See [example source](../../examples/mechanisms/coverage.py).

## Methodology

Source: [agents/methodology.py](../../agents/methodology.py).

```python
class Methodology:
    def system_prompt(self, *, auth: bool = False, compact: bool = False) -> str:
        ...
```

The interface is a structural Protocol. Custom implementations must accept the
keyword arguments above; they need not subclass a built-in. Constructor kwargs
are all `[methodology]` keys except `type`. A `name` attribute identifies the
methodology in profile summaries. Built-ins retain kwargs in `config` and append
`extra_instructions`; custom implementations must implement equivalent behavior
if desired.

For example, create an importable `my_extensions` package with `__init__.py` and
`methodology.py` (install the package or add its parent to `PYTHONPATH`):

```python
class ReviewMethodology:
    name = "review"

    def __init__(self, objective="Review the authorized application", **config):
        self.objective = objective
        self.config = config

    def system_prompt(self, *, auth=False, compact=False):
        return f"{self.objective}. Record reproducible evidence and stay within scope."
```

```toml
[methodology]
type = "my_extensions.methodology:ReviewMethodology"
objective = "Investigate authorization boundaries"
turn_template = """
{scope_lines}
{scope_limit_line}
{budget_line}
Established: {established}
Findings: {findings}
Next leads: {leads}
Avoid: {dead_ends}
Latest results: {recent_results}
Choose the next evidence-gathering step within the authorized scope.
"""
```

The rebuilt-context path reads `methodology.config["turn_template"]`; keep that
attribute if supplying a custom template. A methodology does not register tools
or change scope enforcement. Built-in methodology names are `interactive`, `pentest` and `redteam`.

### Turn template placeholders

Source: [agents/turn_context.py](../../agents/turn_context.py), `build_turn_context`.

| Placeholder | Value |
| --- | --- |
| `background`, `goal` | Built-in bug-bounty background and goal text |
| `budget_line` | Spent/total budget and percentage remaining |
| `scope_lines`, `scope_limit_line` | Scope list and scope guidance |
| `dead_ends` | Repeated/rate-limited endpoint guidance |
| `excluded_classes` | Built-in low-value class text |
| `established` | Distilled facts |
| `findings` | Finding summaries |
| `leads` | Ranked next leads |
| `recent_results` | Last tool results |

Templates use Python string formatting: escape literal braces as `{{` and `}}`.
Unknown named/indexed placeholders log a warning and fall back to the
default template. Malformed braces or format specifications can raise an error. There is no template-time Python expression evaluation.
The progress digest is appended after the rendered template.

## TargetProvider

Source: [agents/target_provider.py](../../agents/target_provider.py).

```python
async def get_targets(self, pipeline, top_n: int, min_bounty: float):
    # Return list[BountyProgram], or None to abort target selection.
    ...
```

Custom providers use `package.module:ClassName`. All `[target]` keys except
`provider` become constructor kwargs. Built-ins are `auto`, `direct`, `bugbounty`.
The pipeline argument exposes session, persistence, discovery workers and graph;
this is a coupled Python interface, not a remote service API. Use
[DirectTargetProvider](../../agents/target_provider.py) as the reference for
building scope from explicit targets and recording selected programs.
[BountyProgram](../../models/program.py) is the returned scope model, despite
its bounty-specific name. Returning None aborts; an empty list supplies no targets.

## Tool and ToolRegistry

Source: [agents/tools.py](../../agents/tools.py).

```python
import asyncio
from agents.tools import Tool, ToolRegistry

async def summarize_text(params):
    return {"characters": len(params["text"])}

registry = ToolRegistry()
registry.register(Tool(
    name="summarize_text",
    description="Count characters in supplied text",
    parameters={"type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"]},
    handler=summarize_text,
))
print(asyncio.run(registry.get("summarize_text").handler({"text": "hello"})))
```

A Tool has `name`, `description`, JSON-schema `parameters`, async `handler`,
`category="general"` and `risk_level="low"`. The handler receives a parameter
dictionary. Registering an existing name overwrites it; `merge(other)` registers
all of the other registry's tools with the same overwrite rule. `get` returns a
Tool or None; `unregister` returns whether it existed; `list_tools(category=None)`
and `names()` expose discovery. Schema exporters support the model adapters.

The example exercises a standalone registry. To expose it to an investigator,
merge it into the registry supplied when constructing that agent, at the relevant
Python assembly point. A TOML methodology declaration alone will not do this.
`risk_level` is metadata, not a sandbox or an approval policy. Custom tools must
handle their own input semantics and resource cleanup.

## External MCP tools

Use profile-configured [MCP servers](../mcp.md) to add tools without a Python extension. Connections are shared for the full-command lifetime; tools are discovered before agents are constructed.
