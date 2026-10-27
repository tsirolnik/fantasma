"""Per-agent LLM configuration.

Allows the Director and each worker to use different LLM providers/models.
Example: Director on Opus, analyzer on Sonnet, other workers on Ollama.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config import get_settings, override_settings
from llm import get_provider
from llm.base import LLMProvider

logger = logging.getLogger(__name__)


@dataclass
class AgentLLMConfig:
    """LLM configuration for a single agent."""

    provider: str = "ollama"  # "claude", "ollama", or "openai"
    model: str = ""  # empty = use default for that provider


@dataclass
class LLMConfigMap:
    """Maps agent roles to their LLM configurations."""

    director: AgentLLMConfig = field(
        default_factory=lambda: AgentLLMConfig(provider="claude")
    )
    default_worker: AgentLLMConfig = field(
        default_factory=lambda: AgentLLMConfig(provider="ollama")
    )
    retain_thinking: bool = True
    overrides: dict[str, AgentLLMConfig] = field(default_factory=dict)

    def get_config(self, agent_name: str) -> AgentLLMConfig:
        if agent_name == "director":
            return self.director
        return self.overrides.get(agent_name, self.default_worker)


def configured_llm_map() -> LLMConfigMap:
    """Resolve role settings, falling back to the globally configured provider."""
    settings = get_settings()
    return LLMConfigMap(
        director=AgentLLMConfig(
            provider=settings.director_llm_provider or settings.llm_provider,
            model=settings.director_llm_model,
        ),
        default_worker=AgentLLMConfig(
            provider=settings.worker_llm_provider or settings.llm_provider,
            model=settings.worker_llm_model,
        ),
    )


def apply_cli_llm_overrides(config: LLMConfigMap, args) -> LLMConfigMap:
    """Apply explicit flags after profile resolution, preserving unspecified fields."""
    for role, attr in (("director", "director"), ("worker", "default_worker"),
                       ("analyzer", None)):
        provider = getattr(args, f"{role}_llm", None)
        model = getattr(args, f"{role}_model", None)
        if provider is None and model is None:
            continue
        base = getattr(config, attr) if attr else config.get_config("analyzer")
        resolved = AgentLLMConfig(
            provider=provider or base.provider,
            model=model if model is not None else ("" if provider and provider != base.provider else base.model),
        )
        if attr:
            setattr(config, attr, resolved)
        else:
            config.overrides["analyzer"] = resolved
    return config


def create_llm_for_agent(
    agent_name: str,
    config_map: LLMConfigMap,
) -> LLMProvider | None:
    """Create an LLM provider instance for a specific agent.

    Returns None if the provider cannot be created (missing API key, etc.)
    so agents can fall back to no-LLM mode.
    """
    cfg = config_map.get_config(agent_name)
    settings = get_settings()

    # Temporarily override model settings if a custom model is specified
    overrides: dict = {}
    if cfg.model:
        if cfg.provider == "claude":
            overrides["claude_model"] = cfg.model
        elif cfg.provider == "gemini":
            overrides["gemini_model"] = cfg.model
        elif cfg.provider == "ollama":
            overrides["ollama_model"] = cfg.model
        elif cfg.provider == "openai":
            overrides["openai_model"] = cfg.model
        elif cfg.provider == "vertex":
            overrides["vertex_model"] = cfg.model

    if overrides:
        override_settings(**overrides)

    try:
        provider = get_provider(cfg.provider)
        provider.retain_thinking = config_map.retain_thinking
        logger.info(
            "Created %s provider for agent '%s' (model: %s)",
            cfg.provider,
            agent_name,
            cfg.model or "default",
        )
        return provider
    except Exception:
        # Fall back to default worker provider (e.g. Ollama) if primary fails
        fallback_cfg = config_map.default_worker
        if cfg.provider != fallback_cfg.provider:
            try:
                provider = get_provider(fallback_cfg.provider)
                provider.retain_thinking = config_map.retain_thinking
                logger.warning(
                    "Agent '%s': %s unavailable, falling back to %s",
                    agent_name, cfg.provider, fallback_cfg.provider,
                )
                return provider
            except Exception:
                pass
        logger.warning(
            "Could not create LLM for agent '%s' (provider=%s). "
            "Agent will run without LLM reflection.",
            agent_name,
            cfg.provider,
            exc_info=True,
        )
        return None
    finally:
        # Restore original settings
        if overrides:
            restore = {}
            if "claude_model" in overrides:
                restore["claude_model"] = settings.claude_model
            if "gemini_model" in overrides:
                restore["gemini_model"] = settings.gemini_model
            if "ollama_model" in overrides:
                restore["ollama_model"] = settings.ollama_model
            if "openai_model" in overrides:
                restore["openai_model"] = settings.openai_model
            if "vertex_model" in overrides:
                restore["vertex_model"] = settings.vertex_model
            if restore:
                override_settings(**restore)
