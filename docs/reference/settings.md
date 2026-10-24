# Environment/settings reference

Generated from [Settings](../../config.py) declarations without reading `.env`.
Regenerate with `python scripts/generate_reference.py`.

Variables use uppercase field names. Process environment values override `.env`
in the working directory. These are declared defaults, not effective run values.
See [configuration](../configuration.md) for role models and CLI/profile precedence.
Not every engine consumes every setting. Default model IDs may no longer be available.

| Variable | Python type | Declared default | Description / constraints |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | `<class 'str'>` | `'claude'` | claude or ollama |
| `ANTHROPIC_API_KEY` | `<class 'str'>` | `''` | Anthropic API key |
| `CLAUDE_MODEL` | `<class 'str'>` | `'claude-sonnet-4-6'` | See source declaration and consuming components. |
| `OLLAMA_MODEL` | `<class 'str'>` | `'llama3'` | See source declaration and consuming components. |
| `OLLAMA_BASE_URL` | `<class 'str'>` | `'http://localhost:11434'` | See source declaration and consuming components. |
| `OLLAMA_TIMEOUT` | `<class 'int'>` | `120` | Timeout in seconds for Ollama API calls (default 2 min) |
| `OLLAMA_NUM_CTX` | `<class 'int'>` | `0` | Context window size for Ollama (0 = model default) |
| `OPENAI_BASE_URL` | `<class 'str'>` | `''` | Base URL for OpenAI-compatible API |
| `OPENAI_API_KEY` | `<class 'str'>` | `''` | API key for OpenAI-compatible provider |
| `OPENAI_MODEL` | `<class 'str'>` | `''` | Model name (e.g. qwen/qwen3-8b-fp8) |
| `OPENAI_INPUT_PRICE` | `float \| None` | `None` | Cost per 1M input tokens in USD Ge(ge=0), allow_inf_nan=False |
| `OPENAI_OUTPUT_PRICE` | `float \| None` | `None` | Cost per 1M output tokens in USD Ge(ge=0), allow_inf_nan=False |
| `OPENAI_FREE_ENDPOINT` | `<class 'bool'>` | `False` | Explicitly allow zero-cost accounting for a free endpoint |
| `OPENAI_ENABLE_THINKING` | `<class 'bool'>` | `True` | Enable thinking/reasoning mode for models that support it (Qwen3.5, DeepSeek) |
| `GEMINI_API_KEY` | `<class 'str'>` | `''` | Google Gemini API key (not needed if using Vertex AI) |
| `GEMINI_MODEL` | `<class 'str'>` | `'gemini-2.5-flash-preview-05-20'` | Gemini model ID |
| `GEMINI_INPUT_PRICE` | `<class 'float'>` | `0.0` | Override: cost per 1M input tokens (0 = built-in) |
| `GEMINI_OUTPUT_PRICE` | `<class 'float'>` | `0.0` | Override: cost per 1M output tokens (0 = built-in) |
| `VERTEX_PROJECT_ID` | `<class 'str'>` | `''` | GCP project ID for Vertex AI |
| `VERTEX_REGION` | `<class 'str'>` | `'global'` | GCP region for Vertex AI (default: global) |
| `VERTEX_MODEL` | `<class 'str'>` | `'claude-sonnet-4-6'` | Vertex AI model ID |
| `VERTEX_INPUT_PRICE` | `<class 'float'>` | `0.0` | Override: cost per 1M input tokens in USD (0 = use built-in pricing) |
| `VERTEX_OUTPUT_PRICE` | `<class 'float'>` | `0.0` | Override: cost per 1M output tokens in USD (0 = use built-in pricing) |
| `HACKERONE_API_TOKEN` | `<class 'str'>` | `''` | See source declaration and consuming components. |
| `HACKERONE_API_USERNAME` | `<class 'str'>` | `''` | See source declaration and consuming components. |
| `BUGCROWD_API_TOKEN` | `<class 'str'>` | `''` | See source declaration and consuming components. |
| `BUGCROWD_EMAIL` | `<class 'str'>` | `''` | Bugcrowd login email for scope enrichment |
| `BUGCROWD_PASSWORD` | `<class 'str'>` | `''` | Bugcrowd login password for scope enrichment |
| `INTIGRITI_API_TOKEN` | `<class 'str'>` | `''` | See source declaration and consuming components. |
| `RATE_LIMIT_RPS` | `<class 'float'>` | `2.0` | See source declaration and consuming components. |
| `MAX_CONCURRENCY` | `<class 'int'>` | `10` | See source declaration and consuming components. |
| `SCAN_TIMEOUT` | `<class 'int'>` | `30` | See source declaration and consuming components. |
| `HOURLY_RATE` | `<class 'float'>` | `50.0` | See source declaration and consuming components. |
| `INFRA_HOURLY_COST` | `<class 'float'>` | `0.1` | See source declaration and consuming components. |
| `SESSION_BUDGET` | `<class 'float'>` | `100.0` | See source declaration and consuming components. |
| `DIRECTOR_LLM_PROVIDER` | `<class 'str'>` | `'claude'` | LLM provider for the Director agent |
| `DIRECTOR_LLM_MODEL` | `<class 'str'>` | `''` | Model override for the Director (empty = provider default) |
| `WORKER_LLM_PROVIDER` | `<class 'str'>` | `'ollama'` | Default LLM provider for worker agents |
| `WORKER_LLM_MODEL` | `<class 'str'>` | `''` | Model override for workers (empty = provider default) |
| `AUTO_MODE` | `<class 'bool'>` | `False` | Run fully unattended (no human checkpoints) |
| `PAUSE_POINTS` | `<class 'str'>` | `''` | Comma-separated pipeline phases to pause at (e.g. discover,analyze) |
| `CLI_TOOL_MODE` | `<class 'bool'>` | `False` | Use CLI-style single 'run' tool to reduce context token usage |
| `MAX_RESPONSE_TOKENS` | `<class 'int'>` | `4096` | Max tokens the LLM may generate per call (reasoning + text + tool args) |
| `TOOL_CACHE_TTL` | `<class 'int'>` | `90` | Seconds an idempotent read-tool result (extract_/read_/get_, GET fetch) is served from cache on an identical repeat call — free, no re-execution. State-changing/testing tools are never cached. 0 disables. |
| `PIPELINE_MODE` | `<class 'str'>` | `''` | Pipeline mode: 'swarm' or 'lab' — required for 'full' command |
| `SWARM_AGENTS` | `<class 'int'>` | `3` | Number of concurrent pen-tester agents per team |
| `SWARM_TIMEOUT_MINUTES` | `<class 'int'>` | `180` | Max swarm runtime in minutes |
| `NUM_TEAMS` | `<class 'int'>` | `2` | Number of competing teams (group mode only) |
| `OLLAMA_PER_AGENT` | `<class 'bool'>` | `False` | Spawn a separate Ollama instance per swarm agent for true parallel inference |
| `MECHANISMS` | `<class 'str'>` | `''` | Comma-separated mechanism names, e.g. 'first_finder_bonus,duplicate_escalation' |
| `KEEP_RECENT` | `<class 'int'>` | `10` | Number of recent iteration pairs to keep uncompressed (increase for large-context models like Qwen3.5-397B) |
| `AGENT_CONTEXT_WINDOW` | `<class 'int'>` | `32000` | Model context window in tokens. Drives how aggressively agent history/tool-results are compressed — set high (e.g. 200000) for large-context models like GLM/Qwen3.5-397B to stop shredding evidence. |
| `GENEROUS_CONTEXT` | `<class 'bool'>` | `False` | Explicit opt-in: hold several times more verbatim agent history/tool-result text before compressing, on top of whatever agent_context_window's tier already allows. Safe ONLY for a REMOTE API provider (Novita/OpenAI-compatible, Claude, Gemini, Vertex) — a LOCAL Ollama model shares the host's RAM/VRAM with the agent process, and the current conservative tiers exist specifically to avoid OOM-ing a local machine (empirically: 2-3 local agents with large verbatim contexts tipped a 32GB M4 into swap and got OOM-killed). Never auto-inferred from the provider choice — must be set explicitly (--generous-context), so a run never silently changes its memory footprint based on which provider happens to be configured. |
| `DOMAIN_TRIAGE` | `<class 'bool'>` | `False` | Enable smart domain triage — Director fingerprints and ranks domains before spawning agents |
| `AUTH_TESTING` | `<class 'bool'>` | `True` | Authenticated testing — agents attempt account creation, login, and test auth surfaces (disable with --no-auth-testing) |
| `ENABLE_BROWSER` | `<class 'bool'>` | `True` | Start the Playwright browser pool. Disable with --no-browser to cut ~0.5-1GB of the run's memory footprint on RAM-constrained machines (agents lose browser_* tools, keep HTTP/docker/code). |
| `COMPACT_PROMPT` | `<class 'bool'>` | `False` | Use compact system prompt (~60% smaller) to save tokens on rate-limited or expensive models |
| `FALKORDB_ENABLED` | `<class 'bool'>` | `True` | Enable FalkorDB graph memory (requires Docker) |
| `FALKORDB_HOST` | `<class 'str'>` | `'localhost'` | FalkorDB host |
| `FALKORDB_PORT` | `<class 'int'>` | `6380` | FalkorDB port (6380 to avoid Redis conflict) |
| `WEB_AUTH_TOKEN` | `<class 'str'>` | `''` | Dashboard access token; generated at startup if empty |
| `WEB_HOST` | `<class 'str'>` | `'127.0.0.1'` | See source declaration and consuming components. |
| `WEB_PORT` | `<class 'int'>` | `8080` | See source declaration and consuming components. |
| `OUTPUT_DIR` | `<class 'pathlib._local.Path'>` | `PosixPath('output')` | See source declaration and consuming components. |
| `VERBOSE` | `<class 'bool'>` | `False` | See source declaration and consuming components. |

Keep populated credentials outside Git. See [.env.example](../../.env.example).
