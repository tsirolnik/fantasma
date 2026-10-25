# Changes on main after v0.1.0

These changes are available from the current source checkout. The original
v0.1.0 tag and release archives have not been rebuilt with them.

## Interactive workspace

- Quiet console output with persistent diagnostic logs; enable console diagnostics
  using `full --diagnostics` or `[interaction] diagnostics = true`.
- Markdown replies, animated activity status, and concise tool progress.
- A context inspector for assembled model input, instructions, tools and shared memory.
  Ctrl+G toggles it; `/context` selects detailed views.
- A collapsible tool inspector: Ctrl+O toggles, Escape closes, PgUp/PgDn scroll.
  `/peek ID` selects a specific call; `/peek off` closes the panel.
- `/steer` queues guidance, `/interrupt` cancels and replaces a request, and
  `/stop` cancels while keeping the session open.
- Autosaved local sessions: `fantasma sessions`, `full --resume-session [ID]`,
  `/save`, `/session`, and `/continue`.
- Refreshed browser dashboards and README screenshots with labeled example data.

## Investigation continuity

Interactive agents can record questions, evidence, decisions, and next steps in a
session-persisted journal (`/context plan`). The per-step briefing uses this
journal instead of swarm score reminders and encourages closing unsupported or
blocked leads. This is model guidance, not independent evidence validation.

## Configuration and reliability

- Every shipped profile explicitly inherits `.env`/environment model settings.
- Role settings, profile overrides, and CLI overrides resolve in a documented order.
- Model-only CLI overrides retain the resolved provider; changing a provider drops
  an inherited model override from the previous provider.
- Permanent OpenAI-compatible API failures stop the request. Context-overflow
  recovery is bounded instead of retrying until the agent's iteration limit.

## Using these changes

From your existing checkout with its virtual environment active:

```bash
git pull
python -m pip install -e .
fantasma
```

Restart an older running process to load new features. Processes started before
session persistence was implemented cannot retroactively save their in-memory
conversation through the new commands.

See the [interactive guide](../interactive.md), [setup](../getting-started.md),
and [configuration reference](../configuration.md). This remains experimental:
resume restores saved context rather than live tools, terminal execution targets
macOS/Linux, and interactive findings do not receive independent validation.
