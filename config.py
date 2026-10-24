"""Configuration loaded from environment variables and .env file."""

from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # LLM
    llm_provider: str = Field(default="claude", description="claude or ollama")
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    claude_model: str = Field(default="claude-sonnet-4-6")
    ollama_model: str = Field(default="llama3")
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_timeout: int = Field(default=120, description="Timeout in seconds for Ollama API calls (default 2 min)")
    ollama_num_ctx: int = Field(default=0, description="Context window size for Ollama (0 = model default)")

    # OpenAI-compatible provider (Novita AI, vLLM, remote Ollama, etc.)
    openai_base_url: str = Field(default="", description="Base URL for OpenAI-compatible API")
    openai_api_key: str = Field(default="", description="API key for OpenAI-compatible provider")
    openai_model: str = Field(default="", description="Model name (e.g. qwen/qwen3-8b-fp8)")
    openai_input_price: float | None = Field(default=None, ge=0, allow_inf_nan=False, description="Cost per 1M input tokens in USD")
    openai_output_price: float | None = Field(default=None, ge=0, allow_inf_nan=False, description="Cost per 1M output tokens in USD")
    openai_free_endpoint: bool = Field(default=False, description="Explicitly allow zero-cost accounting for a free endpoint")
    openai_enable_thinking: bool = Field(default=True, description="Enable thinking/reasoning mode for models that support it (Qwen3.5, DeepSeek)")

    # Google Gemini
    gemini_api_key: str = Field(default="", description="Google Gemini API key (not needed if using Vertex AI)")
    gemini_model: str = Field(default="gemini-2.5-flash-preview-05-20", description="Gemini model ID")
    gemini_input_price: float = Field(default=0.0, description="Override: cost per 1M input tokens (0 = built-in)")
    gemini_output_price: float = Field(default=0.0, description="Override: cost per 1M output tokens (0 = built-in)")

    # GCP Vertex AI
    vertex_project_id: str = Field(default="", description="GCP project ID for Vertex AI")
    vertex_region: str = Field(default="global", description="GCP region for Vertex AI (default: global)")
    vertex_model: str = Field(default="claude-sonnet-4-6", description="Vertex AI model ID")
    vertex_input_price: float = Field(default=0.0, description="Override: cost per 1M input tokens in USD (0 = use built-in pricing)")
    vertex_output_price: float = Field(default=0.0, description="Override: cost per 1M output tokens in USD (0 = use built-in pricing)")

    # Platform keys
    hackerone_api_token: str = Field(default="")
    hackerone_api_username: str = Field(default="")
    bugcrowd_api_token: str = Field(default="")
    bugcrowd_email: str = Field(default="", description="Bugcrowd login email for scope enrichment")
    bugcrowd_password: str = Field(default="", description="Bugcrowd login password for scope enrichment")
    intigriti_api_token: str = Field(default="")

    # Scanning
    rate_limit_rps: float = Field(default=2.0)
    max_concurrency: int = Field(default=10)
    scan_timeout: int = Field(default=30)

    # Cost
    hourly_rate: float = Field(default=50.0)
    infra_hourly_cost: float = Field(default=0.10)
    session_budget: float = Field(default=100.0)

    # Director/Worker agent system
    director_llm_provider: str = Field(default="claude", description="LLM provider for the Director agent")
    director_llm_model: str = Field(default="", description="Model override for the Director (empty = provider default)")
    worker_llm_provider: str = Field(default="ollama", description="Default LLM provider for worker agents")
    worker_llm_model: str = Field(default="", description="Model override for workers (empty = provider default)")
    auto_mode: bool = Field(default=False, description="Run fully unattended (no human checkpoints)")
    pause_points: str = Field(default="", description="Comma-separated pipeline phases to pause at (e.g. discover,analyze)")

    # Tool calling optimization
    cli_tool_mode: bool = Field(default=False, description="Use CLI-style single 'run' tool to reduce context token usage")
    max_response_tokens: int = Field(default=4096, description="Max tokens the LLM may generate per call (reasoning + text + tool args)")
    tool_cache_ttl: int = Field(default=90, description="Seconds an idempotent read-tool result (extract_/read_/get_, GET fetch) is served from cache on an identical repeat call — free, no re-execution. State-changing/testing tools are never cached. 0 disables.")

    # Pipeline mode
    pipeline_mode: str = Field(default="", description="Pipeline mode: 'swarm' or 'lab' — required for 'full' command")
    swarm_agents: int = Field(default=3, description="Number of concurrent pen-tester agents per team")
    swarm_timeout_minutes: int = Field(default=180, description="Max swarm runtime in minutes")
    num_teams: int = Field(default=2, description="Number of competing teams (group mode only)")
    ollama_per_agent: bool = Field(default=False, description="Spawn a separate Ollama instance per swarm agent for true parallel inference")

    # Optional pluggable game-theory mechanisms, additive on top of the base
    # scoring (see memory/mechanisms.py). Empty = unmodified baseline, unchanged.
    mechanisms: str = Field(default="", description="Comma-separated mechanism names, e.g. 'first_finder_bonus,duplicate_escalation'")

    # Agent context management
    keep_recent: int = Field(default=10, description="Number of recent iteration pairs to keep uncompressed (increase for large-context models like Qwen3.5-397B)")
    agent_context_window: int = Field(default=32000, description="Model context window in tokens. Drives how aggressively agent history/tool-results are compressed — set high (e.g. 200000) for large-context models like GLM/Qwen3.5-397B to stop shredding evidence.")
    generous_context: bool = Field(default=False, description=(
        "Explicit opt-in: hold several times more verbatim agent history/tool-result "
        "text before compressing, on top of whatever agent_context_window's tier already "
        "allows. Safe ONLY for a REMOTE API provider (Novita/OpenAI-compatible, Claude, "
        "Gemini, Vertex) — a LOCAL Ollama model shares the host's RAM/VRAM with the agent "
        "process, and the current conservative tiers exist specifically to avoid OOM-ing "
        "a local machine (empirically: 2-3 local agents with large verbatim contexts "
        "tipped a 32GB M4 into swap and got OOM-killed). Never auto-inferred from the "
        "provider choice — must be set explicitly (--generous-context), so a run never "
        "silently changes its memory footprint based on which provider happens to be "
        "configured."
    ))

    # Feature flags
    domain_triage: bool = Field(default=False, description="Enable smart domain triage — Director fingerprints and ranks domains before spawning agents")
    auth_testing: bool = Field(default=True, description="Authenticated testing — agents attempt account creation, login, and test auth surfaces (disable with --no-auth-testing)")
    enable_browser: bool = Field(default=True, description="Start the Playwright browser pool. Disable with --no-browser to cut ~0.5-1GB of the run's memory footprint on RAM-constrained machines (agents lose browser_* tools, keep HTTP/docker/code).")
    compact_prompt: bool = Field(default=False, description="Use compact system prompt (~60% smaller) to save tokens on rate-limited or expensive models")

    # Graph memory (FalkorDB)
    falkordb_enabled: bool = Field(default=True, description="Enable FalkorDB graph memory (requires Docker)")
    falkordb_host: str = Field(default="localhost", description="FalkorDB host")
    falkordb_port: int = Field(default=6380, description="FalkorDB port (6380 to avoid Redis conflict)")

    # Web dashboard
    web_auth_token: str = Field(default="", description="Dashboard access token; generated at startup if empty")
    web_host: str = Field(default="127.0.0.1")
    web_port: int = Field(default=8080)

    # Output
    output_dir: Path = Field(default=Path("./output"))
    verbose: bool = Field(default=False)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def override_settings(**kwargs) -> Settings:
    """Override settings with CLI arguments."""
    global _settings
    current = get_settings()
    updated = current.model_copy(update={k: v for k, v in kwargs.items() if v is not None})
    _settings = updated
    return _settings
