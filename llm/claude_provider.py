"""Anthropic Claude LLM provider."""

import asyncio
import logging

import anthropic

from config import get_settings
from llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Approximate pricing per 1M tokens (USD)
CLAUDE_PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0}

MAX_RETRIES = 5
INITIAL_BACKOFF = 30  # seconds — 30k tokens/min limit means waiting ~60s is typical


async def _retry_on_rate_limit(coro_fn, max_retries=MAX_RETRIES):
    """Call coro_fn() with exponential backoff on rate limit errors."""
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except anthropic.RateLimitError as e:
            if attempt == max_retries:
                raise
            wait = INITIAL_BACKOFF * (2 ** attempt)
            logger.warning("Rate limited (attempt %d/%d), waiting %ds: %s", attempt + 1, max_retries, wait, e)
            await asyncio.sleep(wait)


class ClaudeProvider(LLMProvider):
    """Claude LLM provider using the Anthropic SDK."""

    def __init__(self):
        settings = get_settings()
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = settings.claude_model
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    async def analyze(self, prompt: str, context: str = "") -> str:
        messages = []
        if context:
            messages.append({"role": "user", "content": f"Context:\n{context}\n\n{prompt}"})
        else:
            messages.append({"role": "user", "content": prompt})

        response = await _retry_on_rate_limit(
            lambda: self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=messages,
            )
        )
        self.total_input_tokens += response.usage.input_tokens
        self.total_output_tokens += response.usage.output_tokens
        logger.debug(
            f"Claude tokens: in={response.usage.input_tokens}, out={response.usage.output_tokens}"
        )
        return response.content[0].text

    async def generate_report(self, prompt: str) -> str:
        return await self.analyze(prompt)

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> float:
        # cached_input_tokens accepted for interface parity; Claude cache
        # pricing isn't modeled here yet.
        pricing = CLAUDE_PRICING.get(self.model, DEFAULT_PRICING)
        cost = (input_tokens / 1_000_000) * pricing["input"]
        cost += (output_tokens / 1_000_000) * pricing["output"]
        return cost

    @property
    def session_cost(self) -> float:
        return self.estimate_cost(self.total_input_tokens, self.total_output_tokens)

    @property
    def supports_tool_use(self) -> bool:
        return True

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 4096,
    ) -> dict:
        response = await _retry_on_rate_limit(
            lambda: self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=messages,
                tools=tools,
            )
        )
        self.total_input_tokens += response.usage.input_tokens
        self.total_output_tokens += response.usage.output_tokens
        logger.debug(
            "Claude tool-call tokens: in=%d, out=%d",
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return {
            "content": response.content,
            "stop_reason": response.stop_reason,
            "usage": {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        }
