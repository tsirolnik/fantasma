"""Ollama local LLM provider."""

import json
import logging
import re
from uuid import uuid4

import httpx
import ollama

from config import get_settings
from llm.base import LLMProvider

logger = logging.getLogger(__name__)
wire = logging.getLogger("llm.wire")


class OllamaProvider(LLMProvider):
    """Local Ollama LLM provider."""

    def __init__(self, base_url: str | None = None):
        settings = get_settings()
        timeout_sec = settings.ollama_timeout or 300
        self.client = ollama.AsyncClient(
            host=base_url or settings.ollama_base_url,
            timeout=httpx.Timeout(timeout_sec, connect=30.0),
        )
        self.model = settings.ollama_model
        self.base_url = base_url or settings.ollama_base_url
        self._num_ctx = settings.ollama_num_ctx

    def _build_options(self, num_predict: int = 4096) -> dict:
        """Build Ollama options dict with num_predict and optional num_ctx."""
        opts: dict = {"num_predict": num_predict}
        if self._num_ctx > 0:
            opts["num_ctx"] = self._num_ctx
        return opts

    async def analyze(self, prompt: str, context: str = "") -> str:
        full_prompt = f"Context:\n{context}\n\n{prompt}" if context else prompt
        response = await self.client.chat(
            model=self.model,
            messages=[{"role": "user", "content": full_prompt}],
            options=self._build_options(4096),
            think=True,
        )
        return response["message"]["content"]

    async def generate_report(self, prompt: str) -> str:
        return await self.analyze(prompt)

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> float:
        return 0.0  # Local inference is free

    @property
    def session_cost(self) -> float:
        return 0.0

    @property
    def supports_tool_use(self) -> bool:
        return True

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 4096,
    ) -> dict:
        # Convert tool schemas to Ollama format
        ollama_tools = [self._convert_tool_schema(t) for t in tools]
        # Normalize messages: Ollama expects string content + tool_calls,
        # but the agentic loop stores Anthropic-style content block lists.
        normalized = self._normalize_messages_for_ollama(messages)

        # Wire debug: log outgoing request
        self._wire_log_request(normalized, ollama_tools)

        # Progressive retry strategy on timeout:
        #   1) think=True,  full num_predict  — best quality
        #   2) think=False, halved num_predict — much faster
        #   3) give up, return error to agent loop
        attempts = [
            {"think": True,  "num_predict": max_tokens},
            {"think": False, "num_predict": max_tokens // 2},
        ]

        last_err = None
        for i, attempt in enumerate(attempts):
            try:
                response = await self.client.chat(
                    model=self.model,
                    messages=normalized,
                    tools=ollama_tools,
                    options=self._build_options(attempt["num_predict"]),
                    think=attempt["think"],
                )
                result = self._parse_chat_response(response)
                self._wire_log_response(result)
                return result
            except httpx.TimeoutException as e:
                last_err = e
                if i < len(attempts) - 1:
                    next_a = attempts[i + 1]
                    logger.warning(
                        "Ollama timeout (attempt %d/%d, think=%s) — retrying with think=%s, num_predict=%d",
                        i + 1, len(attempts), attempt["think"],
                        next_a["think"], next_a["num_predict"],
                    )
                continue
            except ollama.ResponseError as e:
                last_err = e
                if i < len(attempts) - 1:
                    next_a = attempts[i + 1]
                    logger.warning(
                        "Ollama ResponseError (attempt %d/%d): %s — retrying with think=%s",
                        i + 1, len(attempts), e, next_a["think"],
                    )
                    continue
                logger.warning("Ollama ResponseError (all attempts failed): %s", e)
                return {
                    "content": [{"type": "text", "text": f"[Tool call failed: {e}. Call a tool now.]"}],
                    "thinking": "",
                    "stop_reason": "end_turn",
                    "usage": {"input": 0, "output": 0},
                }

        # All attempts timed out
        logger.error("Ollama timed out on all %d attempts: %s", len(attempts), last_err)
        return {
            "content": [{"type": "text", "text": "[TIMEOUT] LLM timed out even without thinking. Context is too large — compress and retry."}],
            "thinking": "",
            "stop_reason": "end_turn",
            "usage": {"input": 0, "output": 0},
        }

    def _parse_chat_response(self, response: dict) -> dict:
        """Parse an Ollama chat response into the standard agent format."""
        msg = response["message"]
        thinking = msg.get("thinking", "") or ""
        content = []
        raw_text = msg.get("content", "") or ""
        # Strip raw <tool_call> XML that some models echo alongside real tool_calls
        if msg.get("tool_calls") and raw_text:
            raw_text = re.sub(
                r"<tool_call>.*?</tool_call>", "", raw_text, flags=re.DOTALL
            ).strip()
        if raw_text:
            content.append({"type": "text", "text": raw_text})
        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            content.append({
                "type": "tool_use",
                "id": uuid4().hex[:12],
                "name": tc["function"]["name"],
                "input": tc["function"]["arguments"],
            })
        return {
            "content": content,
            "thinking": thinking,
            "stop_reason": "tool_use" if tool_calls else "end_turn",
            "usage": {"input": 0, "output": 0},
        }

    def _wire_log_request(self, messages: list[dict], tools: list[dict]) -> None:
        """Log the full outgoing request to the wire debug log."""
        lines = [f">>> REQUEST to {self.model} | {len(messages)} msgs | {len(tools)} tools"]
        for i, m in enumerate(messages):
            role = m.get("role", "?")
            content = m.get("content", "")
            tcs = m.get("tool_calls")
            tc_info = f" + {len(tcs)} tool_calls" if tcs else ""
            preview = (str(content) or "")[:300]
            if len(str(content or "")) > 300:
                preview += "..."
            lines.append(f"  [{i}] {role}{tc_info}: {preview}")
            if tcs:
                for tc in tcs:
                    fn = tc.get("function", {})
                    lines.append(f"       -> {fn.get('name', '?')}({json.dumps(fn.get('arguments', ''))[:100]})")
        wire.debug("\n".join(lines))

    def _wire_log_response(self, result: dict) -> None:
        """Log the full incoming response to the wire debug log."""
        lines = [f"<<< RESPONSE from {self.model} | stop={result.get('stop_reason')} | usage={result.get('usage')}"]
        for block in result.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")[:500]
                if len(block.get("text", "")) > 500:
                    text += "..."
                lines.append(f"  text: {text}")
            elif block.get("type") == "tool_use":
                args_str = json.dumps(block.get("input", {}))[:200]
                lines.append(f"  tool_use: {block.get('name', '?')}({args_str})")
        thinking = result.get("thinking", "")
        if thinking:
            lines.append(f"  thinking: {thinking[:300]}{'...' if len(thinking) > 300 else ''}")
        wire.debug("\n".join(lines))

    @staticmethod
    def _normalize_messages_for_ollama(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-style messages to Ollama's expected format.

        Ollama expects:
          - assistant messages: content=str, tool_calls=[{function:{name,arguments}}]
          - user messages with tool results: role="tool", content=str
        The agentic loop stores:
          - assistant content as a list of {type:text/tool_use} blocks
          - tool results as user messages with list of {type:tool_result} dicts
        """
        normalized = []
        for msg in messages:
            content = msg.get("content")

            # Already a plain string — pass through
            if isinstance(content, str):
                normalized.append(msg)
                continue

            # List of content blocks (Anthropic format)
            if isinstance(content, list) and len(content) > 0:
                first = content[0]

                # Check if these are tool_result blocks (user role)
                is_tool_result = False
                if isinstance(first, dict) and first.get("type") == "tool_result":
                    is_tool_result = True
                elif hasattr(first, "type") and getattr(first, "type") == "tool_result":
                    is_tool_result = True

                if is_tool_result:
                    # Convert each tool_result into a separate "tool" message
                    for block in content:
                        if isinstance(block, dict):
                            normalized.append({
                                "role": "tool",
                                "content": str(block.get("content", "")),
                            })
                        else:
                            normalized.append({
                                "role": "tool",
                                "content": str(getattr(block, "content", "")),
                            })
                    continue

                # Assistant message with text + tool_use blocks
                text_parts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "function": {
                                    "name": block["name"],
                                    "arguments": block.get("input", {}),
                                },
                            })
                    else:
                        btype = getattr(block, "type", None)
                        if btype == "text":
                            text_parts.append(getattr(block, "text", ""))
                        elif btype == "tool_use":
                            tool_calls.append({
                                "function": {
                                    "name": block.name,
                                    "arguments": getattr(block, "input", {}),
                                },
                            })

                out = {
                    "role": msg.get("role", "assistant"),
                    "content": "\n".join(text_parts),
                }
                if tool_calls:
                    out["tool_calls"] = tool_calls
                normalized.append(out)
                continue

            # Fallback — pass through
            normalized.append(msg)

        return normalized

    @staticmethod
    def _convert_tool_schema(tool: dict) -> dict:
        """Convert Anthropic-style tool schema to Ollama format."""
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        }
