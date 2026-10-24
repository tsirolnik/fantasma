# Dashboard API reference

The dashboard is an aiohttp service created by [web/server.py](../../web/server.py)
and connected to the run by [PipelineBridge](../../web/bridge.py). This is an
internal, unversioned API; response fields can evolve with the frontend.
Return to the [documentation index](../README.md).

## Start and authenticate

Add `--web` to `fantasma full ...`. The default bind address is
`127.0.0.1:8080`; `--web-port` changes the port. Set `WEB_AUTH_TOKEN` for a reusable
token, otherwise a fresh token is printed at startup. Login through the browser
form, or supply `Authorization: Bearer TOKEN` to API calls.

```bash
curl -H "Authorization: Bearer $WEB_AUTH_TOKEN" http://127.0.0.1:8080/api/status
```

This example assumes the shell variable contains the same token as the server;
a value stored only in `.env` is not automatically exported to your shell.
`POST /login` accepts form field `token` and sets an HttpOnly, SameSite=Strict
session cookie (Secure when served over HTTPS). API calls also accept that cookie.

Unexpected Host values and mismatched Origin values are rejected. WebSocket
clients must send an Origin exactly matching the server scheme and host, in
addition to authentication. `/api/ingest` specifically requires the bearer token.
Keep the service local unless you supply appropriate deployment controls.

## Conversational pentester

The default pentester uses [InteractiveWebUI](../../web/interactive.py), sharing
the same authentication middleware. Its companion page supports `GET /api/status`
(scope, running state, cost, findings count, recent output), `GET /api/profile`,
and `POST /api/message` with JSON `{"text": "..."}`. Messages use the same
session handler as terminal input. `/quit` stays terminal-only; `/stop` works
from either interface. The browser remains available while the terminal session
is open. The pipeline routes below apply to swarm and lab.

## Routes

| Method/path | Input | Result / behavior |
| --- | --- | --- |
| `GET /` | Login cookie or bearer token | Dashboard, or login form when unauthenticated |
| `POST /login` | Form `token` | Set cookie and redirect; invalid token gives 401 |
| `GET /api/status` | None | Bridge status snapshot |
| `GET /api/profile` | None | Bridge profile view; not an offline inspector or complete CLI-resolution report |
| `GET /api/messages` | None | MessageBus conversation log |
| `GET /api/costs` | None | Current total, breakdown, per-target spend, budget and history |
| `GET /api/findings` | None | `raw` and `analyzed` finding arrays, with shared-memory fallback |
| `GET /api/memory` | None | Learning summary, recent scans and best vulnerability types |
| `GET /api/graph` | None | Connectivity and operation counts |
| `GET /api/history/findings` | Optional `target`, `vuln_type`, `limit` (100, capped 500) | Stored findings and program filter options |
| `GET /api/history/scans` | Optional `target`, `limit` (50, capped 200) | Stored scans |
| `GET /api/graph/overview` | None | Graph overview, with SQLite fallback |
| `GET /api/graph/domain` | Required `name` | Domain context, with SQLite fallback |
| `GET /api/graph/techniques` | Required comma-separated `techs` | Technique recommendations from graph |
| `GET /api/llm-wire` | See handler in server source | LLM wire log view |
| `GET /ws` | Authenticated WebSocket upgrade, matching Origin | Live events and control messages |
| `POST /api/ingest` | JSON event, bearer token | External event ingestion/broadcast |

Query validation is limited; callers should send valid positive integer limits.
The live findings fallback uses the pipeline's shared-memory reference, which
points to the first team in multi-team mode. It is not always an aggregate view.
Full payload construction is defined by the linked route handlers and bridge.

## WebSocket control messages

Send JSON text messages, for example:

```json
{"type": "control", "action": "pause"}
```

Control actions are `pause`, `resume`, `stop`, `checkpoint`, and `set_budget`.
Budget changes include a numeric `value`:

```json
{"type": "control", "action": "set_budget", "value": 5}
```

These invoke pipeline controls; they do not roll back target-side actions. Pause
is cooperative. Checkpoint limitations are documented in [runtime](runtime.md).
The bridge also handles `inject`, `checkpoint_response`, `help_response` and
`browser_watch`; consult `PipelineBridge.handle_client_message` and the
[frontend](../../web/frontend.html) for their current payloads and live event
consumers. Those interaction payloads are not a stable public integration API.

## Using the workspace

The full dashboard keeps status and controls above the activity stream and agent
panels. **Filter activity** searches the displayed event text, including agent
names. Checkpoint and help requests remain visible even when a filter is active.
On smaller screens, the panels stack and the tab strip scrolls horizontally.

The conversational interface shares the same visual language, with scope, status,
and stop controls beside the session output. Use **Ctrl+Enter** (or **⌘+Enter**)
to submit a request; Enter alone adds a newline. Setting scope inserts `/scope`
into the composer for you to complete.

For local browser checks without model calls, run `python scripts/check_ui.py`
after installing Playwright Chromium. The README screenshot is generated from
the actual full dashboard with explicitly synthetic data using
`python scripts/capture_dashboard.py`.
