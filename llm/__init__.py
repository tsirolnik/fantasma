"""LLM provider factory."""

from llm.base import LLMProvider
from llm.claude_provider import ClaudeProvider
from llm.gemini_provider import GeminiProvider
from llm.ollama_provider import OllamaProvider
from llm.openai_provider import OpenAICompatibleProvider
from llm.vertex_provider import VertexProvider


def get_provider(name: str | None = None) -> LLMProvider:
    """Factory function to get the configured LLM provider."""
    if name is None:
        from config import get_settings
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
