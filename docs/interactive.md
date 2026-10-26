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

## Inspecting context

Ctrl+G opens/closes the read-only context panel. Escape closes either inspector;
Ctrl+O switches to tool activity. Ctrl+G focuses the panel at the top. PgUp/PgDn or Ctrl+U/D scroll it; Left/Right or F5/F6 cycle views. Tab returns to the input without closing the panel.

| Command | View |
| --- | --- |
| `/context` | Model, profile, scope, spend, message character counts, and tool count |
| `/context messages` | Roles and previews of messages from the last assembled request |
| `/context N` | Message N, numbered from 1 |
| `/context tools` | Tool schemas from the last assembled request |
| `/context prompt` | Current agent system instructions |
| `/context reasoning` | Provider-returned reasoning retained in the last assembled request or saved history |
| `/context plan` | Agent-recorded decision journal: questions, evidence, conclusions, next steps |
| `/context memory` | Shared findings and recent notes; not necessarily all sent to the model |
| `/context off` | Close inspection |

Before a model call, the conversation view shows saved history. After a call is
assembled, it shows a captured preview of those messages and tool schemas before
provider-specific normalization. `/context reasoning` shows only reasoning text
returned by a supported provider; it cannot inspect undisclosed model internals
or a live server-side context window. The inspector does not read configured
provider credentials. Conversation content may still be sensitive. Character counts refer to
message content, not exact tokens. Previews use the same redaction and size bounds
as tool inspection and do not cause model calls. Capture previews are in memory;
a resumed session starts with its saved conversation until the next request.

## Keeping an investigation coherent

For multi-step work, the interactive methodology asks the agent to maintain a
`record_investigation` decision journal: question, observed evidence, conclusion,
next step, and status (`active`, `ruled_out`, `blocked`, or `complete`). Reusing a
question updates its entry. The journal is bounded, included in each step's
progress briefing, saved with the session, and visible through `/context plan`.

The interactive briefing uses the current request and decision journal. The
default interactive profile uses shared claims, so a scoped target does not need
a separate exclusive claim.
It distinguishes an attempted tool call from a completed test and prompts the
agent to summarize or ask for missing inputs when no supported next step remains.
Simple questions need neither a journal nor tools. Journal entries are written
by the agent; review the underlying tool results when checking a conclusion.

## Saved sessions

Interactive CLI sessions are automatically saved under `data/sessions/` in the
working directory. The session ID is printed at startup; `/session` shows it again.
To use another directory, export `FANTASMA_SESSIONS_DIR` before launching Fantasma.

```bash
fantasma                              # Start a new, autosaved session
fantasma sessions                     # List saved sessions; no model calls
fantasma full --resume-session        # Restore the most recently updated session
fantasma full --resume-session ID     # Restore a specific session
```

Inside a session:

- `/save` writes a snapshot immediately.
- `/stop` cancels work and saves the interrupted request.
- `/continue` restarts the interrupted request using saved context and recent tool
  observations. It may make new model/tool calls; it does not replay a suspended
  process at its exact instruction.
- `/quit` saves and closes. On resume, no investigation starts until you send an
  instruction or use `/continue`.

Snapshots retain completed conversation history, scope, shared-memory notes and
findings, the decision journal, recent bounded tool previews, the pending request, budget and recorded
spend. They are saved after scope changes, request/model/tool progress, and clean
shutdown. An abrupt crash can lose changes since the latest snapshot. Browser
cookies/processes, live tool processes, and provider-side requests are not restored.

The saved profile reference is loaded again when resuming. Its current contents
and current `.env` settings apply; credentials are not copied from configuration
into snapshots. Explicit `--profile`, `--targets`, and `--budget` can override the
saved defaults. The restored budget includes previously recorded spend.

Session files may contain sensitive conversation and tool evidence. Files are
created with owner-only permissions and stored outside Git's tracked files.
Only one Fantasma process may open a given session for writing at a time. These
interactive sessions are separate from swarm/lab checkpoint resume.

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
cancel active work. Ctrl+D closes the session when the draft is empty. Piped input uses plain text with
no animation. The playful phrases are waiting indicators, not claims about
specific actions or access to the model's internal reasoning.

Inspect activity without enabling debug logs:

- `/peek`: current phase, all running tool calls, and the five most recent completed calls.
- `/peek 3`: inputs, result/error, status, and timing for call #3.
- `Ctrl+O`: open/close the live inspector panel, preserving your input draft.
  Escape or `/peek off` also closes it. The panel opens at the tail and follows
  new output until its pane is focused. Tab focuses the pane for scrolling;
  F5/F6 select previous/next retained calls, including a latest-activity view.
  Long lines wrap to the panel width.
- `/peek on` or `/peek off`: explicitly enable or disable live details.

In a terminal, `/peek` and `/peek ID` open the same collapsible panel. Details
are rendered in place rather than appended to scrollback. Plain/piped sessions
print snapshots; closing inspection stops expanded output without reprinting it.

The inspector retains recent calls in memory (up to 100 completed calls plus
active calls). Each input/result preview is capped at 4,000 characters; oversized previews
retain their beginning and tail with an omitted-middle marker. Common
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
| `/steer TEXT` | Queues guidance while busy; starts a request if idle |
| `/interrupt TEXT` | Cancels current work, waits for cleanup, then starts the replacement |
| `/context [VIEW]`, Ctrl+G | Inspect conversation, model input, instructions, tools and shared memory |
| `/peek [ID]` | Opens the tool inspector; selects a call by ID |
| `Ctrl+O`, `Escape`, `/peek off` | Toggle inspection / close it / close it |
| `/peek on`, PgUp/PgDn | Enable inspection / scroll terminal details |
| `/save`, `/session` | Save immediately / show the session ID and resume command |
| `/continue` | Restart an interrupted request with saved context; waits if work is running |
| `/stop` | Cancels active work, clears queued redirects and returns to the session |
| `/quit` or `/exit` | Cancels work and closes the browser/session |
| `/help` | Prints commands |
| EOF | Closes the session and cancels active work |

The agent may answer directly without tools, and can finish its current request
without a Director's permission. Successful turns retain conversation and tool
results for follow-up questions; existing context compression may summarize older
material. A cancelled/failed turn is not committed to conversation history, but
shared-memory changes and completed external actions are not rolled back.

The terminal uses prompt-toolkit on macOS/Linux, with asyncio file-descriptor
input for plain/piped sessions. Token streaming and multi-pentester chat routing
are not implemented. Tools are
announced by name, an inspector shows their details, and replies render Markdown.
Local session snapshots support conversation resume.

Browser tools are available unless `--no-browser` is selected. Shared memory,
conversation and shared findings are saved in session snapshots; logs use
`data/logs/`. Custom mechanism instance state is not serialized.
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

## Full-screen console

On an interactive terminal, Fantasma opens a full-screen workspace with a
scrollable conversation and a separate tool/context inspector. The multiline
input sits directly beneath the latest message in the same conversation column,
growing with your draft (up to six visible lines). Short conversations leave unused
space below the input; long conversations scroll above it. Inspectors appear beside the conversation on wide terminals
and below it on narrow terminals. Replies render Markdown. The screen restores
when the session closes. After the screen is restored, the CLI prints the exact
`fantasma full --resume-session ID` command for the saved session. This also
appears after plain-text exits; plain piped input and `TERM=dumb` keep text output.

| Control | Action |
| --- | --- |
| Enter | Send; while busy, steer the current request |
| Alt+Enter or Ctrl+J | Insert a newline |
| Ctrl+X | Send the draft as an interrupt-and-replace request |
| Ctrl+C | Stop work, keeping the draft and session |
| Ctrl+D in composer with empty draft | Close the session |
| Tab / Shift+Tab | Move focus between conversation, inspector and composer |
| Tab on a slash command | Show command completions |
| F2 / Ctrl+O | Toggle tool inspector |
| F3 / Ctrl+G | Toggle context inspector |
| F5 / F6 or Left / Right in inspector | Select previous/next tool call or context view |
| PageUp / PageDown | Scroll focused inspector, or conversation otherwise |
| Ctrl+U / Ctrl+D in conversation or inspector | Scroll up / down without Page keys |
| Ctrl+End or Ctrl+L | Follow latest conversation output and return to composer |
| Escape | Close inspector and return to composer |

On macOS, hints use shortcuts that do not require function keys or configuring
Option as Meta: **Ctrl+O** opens/focuses tools, **Ctrl+G** opens/focuses context,
**Left/Right** selects inspector entries, **Ctrl+U/D** scrolls the focused reading
pane, **Ctrl+L** returns to the latest conversation and composer, and **Ctrl+J**
inserts a newline. These alternatives also work on Linux. Ctrl means Control (⌃),
not Command (⌘). Command shortcuts such as copy/paste remain managed by the
terminal application; Fantasma does not change your terminal's key mappings.
Option+Enter depends on the terminal sending an Escape-prefixed Enter; Ctrl+J
is the alternative when it does not. Opening an inspector focuses it; Tab returns
to the composer without closing it, and Escape closes it. Ctrl+D scrolls while
reading a pane and only exits from an empty composer.

Console text inherits your terminal’s foreground/background palette in both light
and dark themes. Headers and selections use reverse video; Markdown keeps
formatting without forcing a dark color palette.

The console leaves mouse events to your terminal: drag to select text and use
Command+C on macOS (or your terminal’s normal Copy shortcut). No Shift-drag
workaround is required. Use Tab and the scrolling shortcuts above to navigate
inside the app; mouse scrolling is handled by the terminal. The conversation view retains the latest
500 display entries; saved session history is managed separately. Opening or
closing a pane preserves your draft. Use `/help` for session commands.

## Reasoning continuity

For the OpenAI-compatible provider (including Novita), native `reasoning_content`
returned by the endpoint is now retained with its assistant message and sent back
on subsequent tool calls and conversation turns. It is also saved in session
history and restored on resume. No additional model call is needed. Endpoints
that do not return this field keep their existing behavior; inline `<think>` text
is not converted into a native API field. Other provider adapters do not receive
this OpenAI-specific field when a session switches providers.

Use `/context reasoning` to inspect redacted, bounded previews from the last
assembled request (or saved conversation before the first request). This is
provider-returned text, not verified evidence. It does not appear in normal chat
output. The request view updates at the next model call.

Recent reasoning stays intact alongside its tool calls and results. When context
compression runs, older reasoning is supplied to the summarizer along with the
results; fallback compression keeps a labeled excerpt of up to 2,000 characters
per old turn. Retaining reasoning can increase context size, cost and local
session storage. Review tool evidence alongside reasoning when assessing a conclusion.

Restart the CLI to load this change. Existing sessions can be resumed, but
reasoning discarded by older versions cannot be recovered. This currently
applies to accumulated conversation history, including interactive mode; swarm
agents that rebuild their briefings each step do not retain full turn history.

## Browser-based web search

`web_search` uses headless Playwright Chromium to read DuckDuckGo's HTML search
page. No search API or API key is required. Browser support is enabled by default;
set `ENABLE_BROWSER=true` if you previously disabled it, and install Chromium:

```bash
python -m playwright install chromium
```

Example tool arguments:

```json
{"query": "Python asyncio TaskGroup official documentation", "max_results": 5}
```

Queries must be nonempty and at most 500 characters. `max_results` accepts 1–10
(default 5). Results contain titles, URLs and short snippets, with duplicates
removed. Search uses a temporary tab and closes it afterward, leaving the agent's
current page intact. Agents without an existing browser session create and close
a temporary browser pool. Disabling browser support makes search unavailable.

Search performs one navigation with a 20-second navigation timeout and a
25-second overall operation timeout. Challenges/rate limits return `blocked`;
unrecognized markup returns `unrecognized_page`, not a misleading empty result.
There are no automatic retries or CAPTCHA bypasses. Search-engine markup can
change, and public engines can block automated browsing.

Search sends the query to a public engine: do not include credentials or private
information. Results are untrusted references, not proof of a vulnerability.
Public search does not require target scope; following URLs with other tools
still uses those tools' existing scope checks. Nuclei remains available through
Docker and the scanner toolset, but agent prompts and Docker examples no longer
recommend or prescribe it.

## Investigation objective

The built-in interactive, pentest (full and compact), and red-team methodologies
prioritize the highest-impact plausible vulnerability within the user's requested
work and authorized scope, in the shortest practical time with the fewest
necessary tool calls. Impact, likelihood, and expected useful evidence guide the
next action. Scanning, fuzzing and enumeration are optional techniques rather
than a mandatory sequence. Efficiency never replaces verification: findings need
reproducible evidence and demonstrated impact, and severity follows that evidence.

Budgets are ceilings, not spending targets. Pentesters may conclude when the
objective is met, useful leads are exhausted, or missing prerequisites block
progress. They report uncertainty rather than claiming exhaustive coverage or
that the most severe possible vulnerability has been found. Swarm turn context
includes the selected methodology on each rebuilt turn. Custom methodologies and
profile extra instructions remain user-controlled.

Interactive mode still answers ordinary questions without starting an
investigation. The objective governs investigations the user actually requests.
The same objective applies to full and compact pentester prompts. Custom profiles
can extend the methodology with their own instructions.
