# MCP tool plugins

Profiles can connect external Model Context Protocol servers, including an IDA MCP server. Their tools become callable tools in the agent's normal model/tool loop. No Python extension to Fantasma is necessary.

This is separate from the existing `mcp_list_tools` and `mcp_call_tool` research tools, which inspect a target's MCP endpoint on demand.

## Try the included local server

From the repository root:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e .
fantasma profiles inspect examples/mcp/profile.toml
fantasma full --profile examples/mcp/profile.toml --no-browser
```

Configure your model provider as described in [Getting started](getting-started.md). At the prompt, ask: `Use the demo MCP add tool to add 2 and 3.` The demo tool only adds numbers. Profile inspection is offline; starting the session launches the configured MCP process. `--dry-run` skips MCP connections and model calls.

The example uses `python` from your activated environment. Use an absolute interpreter path when running without activation. Stdio commands are executed directly, without a shell. `cwd` resolves relative to the profile file; command arguments are passed literally.

## Connect IDA or another existing server

Add a server table to your interactive profile. Use the transport and endpoint printed by your installed server; the URL below is illustrative, not a promised IDA default:

```toml
[[mcp.servers]]
name = "ida"
transport = "streamable_http"
url = "http://127.0.0.1:13337/mcp"
timeout = 60
# tools = ["exact_name_from_your_server"]
```

Start IDA and its MCP plugin according to that plugin's instructions, open the intended database, then start Fantasma with the profile. Ask the agent to use the IDA tools to inspect the loaded database. Fantasma does not install IDA, select a database, or start an HTTP server for you. For a legacy SSE endpoint, select `transport = "sse"` and supply its actual SSE URL.

For a local stdio server:

```toml
[[mcp.servers]]
name = "local"
transport = "stdio"
command = "/absolute/path/to/server-python"
args = ["/absolute/path/to/server.py"]
# cwd = "."
[mcp.servers.env]
SERVER_API_KEY = "MY_SERVER_API_KEY"
```

For authenticated HTTP, map header names to environment variable names:

```toml
[[mcp.servers]]
name = "remote"
transport = "streamable_http"
url = "https://your-server.example/mcp"
[mcp.servers.headers]
Authorization = "MY_MCP_AUTHORIZATION"
```

Set `MY_MCP_AUTHORIZATION` outside TOML to the complete header value, including `Bearer ` if required. Missing referenced variables fail startup. Profile inspection displays variable names, never their resolved values. Avoid secrets in URLs, command arguments, and profiles. Stdio receives the SDK's minimal default environment plus explicitly mapped variables, rather than the complete parent environment.

## Configuration reference

Each `[[mcp.servers]]` accepts:

| Field | Meaning |
| --- | --- |
| `name` | Required unique identifier; 1–24 letters, digits, `_` or `-` |
| `transport` | `stdio` (default), `streamable_http`, or `sse` |
| `command`, `args`, `cwd` | Stdio executable, string arguments, optional working directory |
| `url` | Required HTTP/SSE endpoint, without embedded user/password |
| `env` | Stdio child variable → parent environment variable name |
| `headers` | HTTP header → environment variable name |
| `tools` | Exact remote tool allowlist; omitted exposes all; `[]` exposes none |
| `timeout` | Positive request/read timeout in seconds; default 60 |

Unknown settings, duplicate server names, and unknown allowlisted tools fail instead of silently disappearing. All servers are required. Tool discovery follows pagination once at startup; restart to refresh changed tool catalogs. Names are exported as `mcp_<server>_<sanitized-tool>_<hash>` to prevent collisions and meet provider name limits. Descriptions retain the original names. Input JSON schemas are preserved and arguments bypass Fantasma's built-in parameter-name normalization.

## Runtime behavior and boundaries

The `full` command owns connections for its lifetime, closes them on exit or startup failure, and shares sessions across tool agents. This includes the interactive pentester, swarm workers and replacements, validators, and tool-based lab workers. Lab profiles must explicitly set `workflow.worker_mode = "tool"`. Deterministic workers and Directors do not gain a tool-use loop from this setting. Programmatic callers can use `async with MCPRuntime() as runtime`, await `runtime.connect(profile.mcp)`, and create/run agents inside that context. Enter and exit on the same task.

Tools consume the existing agent tool-call budget and follow existing tracing and output truncation. Text, text resources, and structured results are forwarded; binary/image/audio blocks are represented by omission notices. Server tool errors become agent tool errors. There is no automatic reconnect or retry of side-effecting calls. Concurrent agents can call the same server, so a stateful IDA workflow is best started with the single interactive pentester.

Only MCP tool discovery and invocation are supported here. Standalone resources, prompts, sampling, elicitation, OAuth login, and live tool-list updates are not implemented. Credentials use environment-backed headers. The SDK dependency is constrained to the tested v1 API.

Configure trusted servers: stdio launches local code, and MCP tools may modify files or remote state. Tool allowlists limit exposure; tool annotations are not an approval system. Existing host/argument scope checks still apply where recognized, but cannot sandbox arbitrary MCP operations or interpret every custom schema. Server-side permissions determine what an IDA or other plugin can access.

## Source reference

- [Profile schema](../profiles/mcp.py) and [loader](../profiles/loader.py)
- [Session lifecycle, discovery and result adapter](../agents/mcp_runtime.py)
- [Agent registry integration](../agents/tool_agent.py) and [command ownership](../commands/full.py)
- [Example profile](../examples/mcp/profile.toml), [demo server](../examples/mcp/server.py), [integration tests](../tests/test_mcp.py)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.28.0)
