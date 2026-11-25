"""LLM provider factory."""

from fantasma.llm.base import LLMProvider
from fantasma.llm.claude_provider import ClaudeProvider
from fantasma.llm.gemini_provider import GeminiProvider
from fantasma.llm.ollama_provider import OllamaProvider
from fantasma.llm.openai_provider import OpenAICompatibleProvider
from fantasma.llm.vertex_provider import VertexProvider


def get_provider(name: str | None = None) -> LLMProvider:
    """Factory function to get the configured LLM provider."""
    if name is None:
        from fantasma.config import get_settings
        name = get_settings().llm_provider

    providers = {
        "claude": ClaudeProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
        "openai": OpenAICompatibleProvider,
        "vertex": VertexProvider,
    }
    if name not in providers:
        raise ValueError(f"Unknown LLM provider: {name}. Choose from: {list(providers.keys())}")
    return providers[name]()
