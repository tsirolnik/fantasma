"""Abstract LLM provider interface."""

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Base class for LLM providers."""

    @abstractmethod
    async def analyze(self, prompt: str, context: str = "") -> str:
        """Analyze content with the LLM. Returns the LLM's text response."""
        ...

    @abstractmethod
    async def generate_report(self, prompt: str) -> str:
        """Generate a structured report. Returns markdown text."""
        ...

    @abstractmethod
    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> float:
        """Estimate the cost of a request in USD.

        `cached_input_tokens` is the subset of `input_tokens` served from the
        provider's prompt cache (billed at a discount by providers that support
        it). Providers without cache pricing ignore it.
        """
        ...

    @property
    def supports_tool_use(self) -> bool:
        """Whether this provider supports native tool calling."""
        return False

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 4096,
    ) -> dict:
        """Send messages with tool definitions, return response with potential tool_use blocks.

        Returns a dict with keys:
            content: list of blocks (text and/or tool_use)
            stop_reason: str ("end_turn" or "tool_use")
            usage: dict with "input" and "output" token counts

        Raises NotImplementedError if not supported.
        """
        raise NotImplementedError("This provider does not support tool calling")
