# Interactive pentester (default profile)

Running `fantasma` or `fantasma full` opens the terminal session
configured by [profiles/interactive.toml](../profiles/interactive.toml). It waits
for a request; startup does not launch discovery, scanning, a Director, or a lab
pipeline. Lab is opt-in with `--profile lab`.

Complete [installation](getting-started.md#1-install-the-python-environment),
then configure your provider. Like every shipped profile, it explicitly declares `[llm] source = "env"`
and inherits `LLM_PROVIDER` from
your environment or `.env`, with `WORKER_LLM_PROVIDER` taking precedence when
set. For Novita, use `LLM_PROVIDER=openai`, configure `OPENAI_BASE_URL`,
`OPENAI_API_KEY`, `OPENAI_MODEL`, and pricing, and leave role providers empty
or set them to `openai`. Leave `WORKER_LLM_MODEL` empty to use `OPENAI_MODEL`;
an old role-model override (for example, an Ollama model name) otherwise wins.
Likewise, leave `DIRECTOR_LLM_MODEL` empty to use the Director provider's default.
Restart the CLI after changing `.env`.

Invalid-model, authentication, billing, and other non-context API failures stop
the request and return control to the session. OpenAI-compatible context overflow
can trigger compression, with at most two recovery attempts.
You can also override the provider explicitly:

```bash
fantasma full --profile interactive --worker-llm openai --budget 1 --no-browser
```

The profile uses `topology.mode = "pentester"` with `interaction.mode = "terminal"`.
Other topologies can also be interactive; see [profile interaction](interaction.md).
There is one pentester in this conversational implementation. It uses the
`scanner` model role, the profile's methodology, shared memory and mechanisms.
`[interaction] max_iterations = 20` and `max_tool_calls = 50` bound each request;
the cost budget applies across the session. No model or browser is created until
your first plain-text request. Controls such as `/status` do not call a model.

## Steering and interruption

- `/steer TEXT` queues guidance for the next model step. An active tool continues.
  Plain text while busy behaves the same way, including during agent startup.
- `/interrupt TEXT` cancels the current request, waits for its cleanup, then starts
  the replacement instruction. Previously completed conversation turns remain.
- `/stop` or Ctrl+C cancels work without starting another request. The session
  stays open. An empty `/interrupt` prints usage and leaves work running.
- When idle, `/steer TEXT` starts a new request.

Cancellation does not undo external actions already performed. In-flight remote
requests may still complete at their provider; interrupted-turn history is not
committed to the conversation. Shared findings already recorded remain available.

## Console output

Model replies render Markdown headings, lists, tables, emphasis, and highlighted
code blocks in the terminal. Piped output preserves the Markdown source.

Interactive sessions hide diagnostic console logs by default, while retaining
logs under `data/logs/`. Responses and actionable session errors remain visible.
In a terminal, an animated ghost-themed status bar rotates playful phrases while
showing elapsed time. During tool execution it displays the actual tool name.
Tool activity is visible even with diagnostics disabled:

```text
  ↳ #1 http_fetch · example.test
  ✓ #1 http_fetch · done · 0.3s
```

Failures use `✗`. Previews omit raw arguments and output, including URL credentials
and query strings. Tool results remain available to the agent and diagnostic logs.
The input prompt stays editable while activity updates; use `/stop` or Ctrl+C to
cancel active work. Ctrl+D closes the session. Piped input uses plain text with
no animation. The playful phrases are waiting indicators, not claims about
specific actions or access to the model's internal reasoning.

Inspect activity without enabling debug logs:

- `/peek`: current phase, all running tool calls, and the five most recent completed calls.
- `/peek 3`: inputs, result/error, status, and timing for call #3.
- `Ctrl+O`: open/close the live inspector panel, preserving your input draft.
  Escape or `/peek off` also closes it. PgUp/PgDn scroll the panel.
- `/peek on` or `/peek off`: explicitly enable or disable live details.

In a terminal, `/peek` and `/peek ID` open the same collapsible panel. Details
are rendered in place rather than appended to scrollback. Plain/piped sessions
print snapshots; closing inspection stops expanded output without reprinting it.

The inspector retains recent calls in memory (up to 100 completed calls plus
active calls). Each input/result preview is capped at 4,000 characters. Common
credential fields, bearer tokens, and URL credentials are redacted; arbitrary
free-form tool output may still contain sensitive material. Inspect locally
before sharing. Calls interrupted by stopping the request are marked accordingly.
The model-wait phase reports the model and request size, not private reasoning.

Enable console diagnostics for a session:

```bash
fantasma full --diagnostics
# Global verbose flag is also supported (before the subcommand):
fantasma --verbose full
```

Or enable them in a custom profile:

```toml
[interaction]
mode = "terminal"
diagnostics = true
```

## Example conversation

```text
/scope localhost
Explain your approach to reviewing the local application at http://localhost:3000.
Check its login flow and show me the evidence for anything you find.
Focus on authentication; don't investigate unrelated endpoints.
/stop
Summarize what we have established so far.
/status
/quit
```

The localhost example assumes you started an authorized local target separately.
The session does not provision one. Scope accepts comma-separated hostnames,
not URLs; include the URL and port in your request. Scope enforcement operates
on hosts, not per-port permissions, and retains the framework's existing
[network-boundary limitations](reference/runtime.md#execution-boundaries).
Recognized network-capable tools are blocked until scope is configured.

## Controls and lifecycle

| Input | Behavior |
| --- | --- |
| Plain text while idle | Starts a request using the pentester's tools when needed |
| Plain text while working | Queues a redirect for a subsequent model call; does not interrupt the current tool |
| `/scope HOST[,HOST]` | Changes scope while idle; `/scope` alone clears it |
| `/status` | Shows scope, estimated spend and finding count |
| `/stop` | Cancels active work, clears queued redirects and returns to the session |
| `/quit` or `/exit` | Cancels work and closes the browser/session |
| `/help` | Prints commands |
| EOF | Closes the session and cancels active work |

The agent may answer directly without tools, and can finish its current request
without a Director's permission. Successful turns retain conversation and tool
results for follow-up questions; existing context compression may summarize older
material. A cancelled/failed turn is not committed to conversation history, but
shared-memory changes and completed external actions are not rolled back.

The current reader uses asyncio terminal file-descriptor support on macOS/Linux.
This is a basic text console, not feature parity with Claude Code or Codex: no
full-screen UI, token streaming, multi-pentester chat routing, or persistent chat
resume. Tools are announced by name and the final answer is printed when ready.

Browser tools are available unless `--no-browser` is selected. Shared memory,
mechanism state and conversation live for this session; logs use `data/logs/`.
There is no automatic SQLite finding persistence, graph integration, independent
validator, Director review or report generation in this path. Claim, submit,
turn and run-end mechanism hooks are supported; Director-review hooks have no
caller here. Findings therefore require review.

`--auto`, multiple workers/teams and checkpoint resume are rejected in
interactive mode; select an explicit batch profile for those features.
`--dry-run` permits session controls but rejects model requests. It is useful for
checking startup and exit without provider calls.

## Customize the default

Copy `profiles/interactive.toml` and pass `--profile PATH`. Configure methodology,
mechanism instances, worker LLM, and per-request limits there. The terminal uses
`--targets` or `/scope` for scope; it does not invoke profile target discovery.
Lab workflow stages and swarm monitors do not apply. Do not change
`topology.agents_per_team` or `num_teams` from one for this engine.

Implementation: [interactive session](../agents/interactive.py),
[tool loop](../agents/tool_agent.py), [command dispatch](../commands/full.py).

## Web companion

Run `fantasma full --web` to use an authenticated browser view alongside
the terminal. Both operate on the same session: requests, redirects, `/scope`,
`/status`, and `/stop` work from either surface. The browser displays responses,
tool-start messages, spend and findings count. Close the session with `/quit`
or EOF in the terminal; the browser does not keep it alive afterward.
The pentester view does not expose swarm-specific widgets or browser-watch.
