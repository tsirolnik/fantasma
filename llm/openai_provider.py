"""OpenAI-compatible LLM provider.

Supports any endpoint that implements the OpenAI chat completions API:
Novita AI, vLLM, remote Ollama (/v1), Together, Groq, Fireworks, etc.
"""

import json
import logging
from uuid import uuid4

from openai import AsyncOpenAI

from config import get_settings
from llm.base import LLMProvider

logger = logging.getLogger(__name__)
wire = logging.getLogger("llm.wire")


class OpenAICompatibleProvider(LLMProvider):
    """Generic OpenAI-compatible provider using the openai Python SDK."""

    def __init__(self):
        settings = get_settings()
        prices = (settings.openai_input_price, settings.openai_output_price)
        if not settings.openai_free_endpoint:
            if any(price is None for price in prices) or not any(prices):
                raise ValueError(
                    "Set OPENAI_INPUT_PRICE and OPENAI_OUTPUT_PRICE (USD per million tokens), "
                    "or explicitly set OPENAI_FREE_ENDPOINT=true for a free endpoint."
                )
        self.input_price = settings.openai_input_price or 0.0
        self.output_price = settings.openai_output_price or 0.0
        base_url = settings.openai_base_url or "https://api.novita.ai/openai"
        api_key = settings.openai_api_key or "no-key"
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = settings.openai_model or "qwen/qwen3-8b"
        self.enable_thinking = settings.openai_enable_thinking
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_input_tokens = 0
        # Fraction of the full input price charged for cache-hit tokens.
        # Novita/Moonshot bill cache reads at ~10% of the input rate.
        self.cache_discount = 0.10

    supports_reasoning_history = True

    async def analyze(self, prompt: str, context: str = "") -> str:
        content = f"Context:\n{context}\n\n{prompt}" if context else prompt
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            max_tokens=4096,
        )
        usage = response.usage
        if usage:
            self.total_input_tokens += usage.prompt_tokens or 0
            self.total_output_tokens += usage.completion_tokens or 0
            self.total_cached_input_tokens += self._cached_input_tokens(usage)
        return response.choices[0].message.content or ""

    async def generate_report(self, prompt: str) -> str:
        return await self.analyze(prompt)

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> float:
        # Cache-hit input tokens are billed at a discount (cache_discount × rate).
        cached = max(0, min(cached_input_tokens, input_tokens))
        uncached = input_tokens - cached
        cost = (uncached / 1_000_000) * self.input_price
        cost += (cached / 1_000_000) * self.input_price * self.cache_discount
        cost += (output_tokens / 1_000_000) * self.output_price
        return cost

    @staticmethod
    def _cached_input_tokens(usage) -> int:
        """Extract cache-hit input tokens from an OpenAI-compatible usage object."""
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None:
            return 0
        # May be a pydantic object or a plain dict depending on the SDK/provider.
        if isinstance(details, dict):
            return int(details.get("cached_tokens") or 0)
        return int(getattr(details, "cached_tokens", 0) or 0)

    @property
    def session_cost(self) -> float:
        return self.estimate_cost(
            self.total_input_tokens,
            self.total_output_tokens,
            self.total_cached_input_tokens,
        )

    @property
    def supports_tool_use(self) -> bool:
        return True

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 4096,
    ) -> dict:
        openai_tools = [self._convert_tool_schema(t) for t in tools]
        openai_messages = self._normalize_messages(messages)

        # Wire debug: log outgoing request
        self._wire_log_request(openai_messages, openai_tools)

        try:
            kwargs: dict = {
                "model": self.model,
                "messages": openai_messages,
                "tools": openai_tools,
                "max_tokens": max_tokens,
            }
            if self.enable_thinking:
                kwargs["extra_body"] = {"enable_thinking": True}
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            # Dump the full request to a file for debugging
            import os
            dump_path = os.path.join("data", "logs", "last_api_error.json")
            try:
                os.makedirs(os.path.dirname(dump_path), exist_ok=True)
                with open(dump_path, "w") as f:
                    json.dump({"model": self.model, "messages": openai_messages, "tools": openai_tools}, f, indent=2, default=str)
                logger.error("OpenAI-compat API error: %s — request dumped to %s", e, dump_path)
            except Exception:
                logger.error("OpenAI-compat API error: %s (could not dump request)", e)
            # Only context overflow can be repaired by compressing history.
            # The SDK already retries transient failures. Surface all other errors
            # (including invalid models, credentials and billing) to the session.
            body = str(getattr(e, "body", "")).lower()
            context_overflow = getattr(e, "status_code", None) in (400, 413) and any(
                marker in body for marker in (
                    "context_length_exceeded", "maximum context length",
                    "context window exceeded",
                )
            )
            if not context_overflow:
                raise
            return {
                "content": [{"type": "text", "text": f"[API ERROR] {type(e).__name__}: {e}"}],
                "thinking": "",
                "stop_reason": "end_turn",
                "usage": {"input": 0, "output": 0},
            }

        usage = response.usage
        cached_in = 0
        if usage:
            cached_in = self._cached_input_tokens(usage)
            self.total_input_tokens += usage.prompt_tokens or 0
            self.total_output_tokens += usage.completion_tokens or 0
            self.total_cached_input_tokens += cached_in
            logger.debug(
                "OpenAI-compat tokens: in=%d (cached=%d), out=%d",
                usage.prompt_tokens or 0,
                cached_in,
                usage.completion_tokens or 0,
            )

        msg = response.choices[0].message

        # Extract reasoning/thinking if present (Qwen3, DeepSeek)
        thinking = ""
        if hasattr(msg, "reasoning_content") and msg.reasoning_content:
            thinking = msg.reasoning_content

        # Build Anthropic-style content blocks
        content = []
        text = msg.content or ""

        # Strip <think>...</think> wrapper from Qwen3 inline thinking
        if text.startswith("<think>"):
            import re
            m = re.match(r"<think>(.*?)</think>\s*(.*)", text, re.DOTALL)
            if m:
                thinking = thinking or m.group(1).strip()
                text = m.group(2).strip()

        if text:
            content.append({"type": "text", "text": text})

        tool_calls = msg.tool_calls or []
        for tc in tool_calls:
            # Skip malformed tool calls (model returned null name)
            if not tc.function or not tc.function.name:
                logger.warning("Skipping tool call with null function name: %s", tc)
                content.append({"type": "text", "text": "[MALFORMED_TOOL_CALL] Tool call had null name — ignored"})
                continue
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append({
                "type": "tool_use",
                "id": tc.id or uuid4().hex[:12],
                "name": tc.function.name,
                "input": args,
            })

        result = {
            "content": content,
            "thinking": thinking,
            "stop_reason": "tool_use" if tool_calls else "end_turn",
            "usage": {
                "input": (usage.prompt_tokens or 0) if usage else 0,
                "output": (usage.completion_tokens or 0) if usage else 0,
                "cached_input": cached_in,
            },
        }

        # Only replay a native API field, never synthesize it from inline <think> text.
        if isinstance(getattr(msg, "reasoning_content", None), str):
            result["reasoning_content"] = msg.reasoning_content

        # Wire debug: log incoming response
        self._wire_log_response(result)

        return result

    def _wire_log_request(self, messages: list[dict], tools: list[dict]) -> None:
        """Log the full outgoing request to the wire debug log."""
        lines = [f">>> REQUEST to {self.model} | {len(messages)} msgs | {len(tools)} tools"]
        for i, m in enumerate(messages):
            role = m.get("role", "?")
            content = m.get("content", "")
            tcs = m.get("tool_calls")
            tc_info = f" + {len(tcs)} tool_calls" if tcs else ""
            # Truncate content for readability
            preview = (str(content) or "")[:300]
            if len(str(content or "")) > 300:
                preview += "..."
            lines.append(f"  [{i}] {role}{tc_info}: {preview}")
            if tcs:
                for tc in tcs:
                    fn = tc.get("function", {})
                    lines.append(f"       -> {fn.get('name', '?')}({fn.get('arguments', '')[:100]})")
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
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-style messages to OpenAI format.

        Anthropic stores:
          - assistant: content = [{type:text, text:...}, {type:tool_use, id:..., name:..., input:{}}]
          - user tool results: content = [{type:tool_result, tool_use_id:..., content:...}]

        OpenAI expects:
          - assistant: content=str, tool_calls=[{id:..., type:"function", function:{name:..., arguments:str}}]
          - tool results: role="tool", tool_call_id=..., content=str
        """
        normalized = []
        for msg in messages:
            content = msg.get("content")
            reasoning = ({"reasoning_content": msg["reasoning_content"]}
                         if msg.get("role") == "assistant" and isinstance(msg.get("reasoning_content"), str) else {})

            # Plain string — pass through
            if isinstance(content, str):
                normalized.append({"role": msg["role"], "content": content, **reasoning})
                continue

            # List of content blocks
            if isinstance(content, list) and len(content) > 0:
                first = content[0]

                # Detect tool_result blocks (from user role)
                is_tool_result = (
                    (isinstance(first, dict) and first.get("type") == "tool_result")
                    or (hasattr(first, "type") and getattr(first, "type") == "tool_result")
                )

                if is_tool_result:
                    for block in content:
                        if isinstance(block, dict):
                            tool_call_id = block.get("tool_use_id", "")
                            result_content = block.get("content", "")
                        else:
                            tool_call_id = getattr(block, "tool_use_id", "")
                            result_content = getattr(block, "content", "")
                        # Multimodal tool result (e.g. screenshot with image + text)
                        if isinstance(result_content, list):
                            openai_parts = []
                            for part in result_content:
                                if isinstance(part, dict) and part.get("type") == "image":
                                    src = part.get("source", {})
                                    openai_parts.append({
                                        "type": "image_url",
                                        "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"},
                                    })
                                elif isinstance(part, dict) and part.get("type") == "text":
                                    openai_parts.append({"type": "text", "text": part.get("text", "")})
                                else:
                                    openai_parts.append({"type": "text", "text": str(part)})
                            normalized.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": openai_parts,
                            })
                        else:
                            normalized.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": str(result_content),
                            })
                    continue

                # Assistant message with text + tool_use blocks
                text_parts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            name = block.get("name")
                            if not name:
                                text_parts.append("[MALFORMED_TOOL_CALL] Tool call had null name")
                                continue
                            tool_calls.append({
                                "id": block.get("id", uuid4().hex[:12]),
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
                    else:
                        btype = getattr(block, "type", None)
                        if btype == "text":
                            text_parts.append(getattr(block, "text", ""))
                        elif btype == "tool_use":
                            name = getattr(block, "name", None)
                            if not name:
                                text_parts.append("[MALFORMED_TOOL_CALL] Tool call had null name")
                                continue
                            tool_calls.append({
                                "id": getattr(block, "id", uuid4().hex[:12]),
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(
                                        getattr(block, "input", {})
                                    ),
                                },
                            })

                out: dict = {
                    "role": msg.get("role", "assistant"),
                    "content": "\n".join(text_parts) or "",
                }
                out.update(reasoning)
                if tool_calls:
                    out["tool_calls"] = tool_calls
                normalized.append(out)
                continue

            # Fallback
            normalized.append({"role": msg.get("role", "user"), "content": str(content) if content else "", **reasoning})

        return OpenAICompatibleProvider._sanitize_messages(normalized)

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Fix message sequences that OpenAI-compatible APIs reject.

        Problems this fixes:
        1. Assistant tool_calls without matching tool results (orphaned by
           context compression or director redirects breaking pair alignment)
        2. Orphaned tool results without matching assistant tool_calls
        3. Last message being an assistant with tool_calls (needs tool results)
        4. Empty tool_call_id on tool messages
        """
        # Collect all tool_call IDs from assistant messages
        call_ids: set[str] = set()
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tid = tc.get("id", "")
                    if tid:
                        call_ids.add(tid)

        # Collect all tool_call IDs that have matching tool results
        result_ids: set[str] = set()
        for m in messages:
            if m.get("role") == "tool":
                tid = m.get("tool_call_id", "")
                if tid:
                    result_ids.add(tid)

        sanitized = []
        for m in messages:
            # Strip orphaned tool_calls from assistant messages
            if m.get("role") == "assistant" and m.get("tool_calls"):
                tcs = m["tool_calls"]
                matched = [tc for tc in tcs if tc.get("id", "") in result_ids]
                if matched:
                    sanitized.append({**m, "tool_calls": matched})
                else:
                    names = [tc.get("function", {}).get("name", "?") for tc in tcs]
                    text = m.get("content") or f"Called {', '.join(names)}"
                    sanitized.append({k: v for k, v in {**m, "content": text}.items() if k != "tool_calls"})
            # Strip orphaned tool results (no matching assistant tool_call)
            elif m.get("role") == "tool":
                tid = m.get("tool_call_id", "")
                if tid and tid in call_ids:
                    sanitized.append(m)
                else:
                    # Convert to user message so context isn't lost
                    sanitized.append({"role": "user", "content": f"[Tool result]: {m.get('content', '')[:200]}"})
            else:
                sanitized.append(m)

        # If last message is assistant with tool_calls, strip them
        if sanitized and sanitized[-1].get("role") == "assistant" and sanitized[-1].get("tool_calls"):
            last = sanitized[-1]
            names = [tc.get("function", {}).get("name", "?") for tc in last["tool_calls"]]
            sanitized[-1] = {k: v for k, v in last.items() if k != "tool_calls"}
            sanitized[-1]["content"] = last.get("content") or f"Called {', '.join(names)}"

        return sanitized

    @staticmethod
    def _convert_tool_schema(tool: dict) -> dict:
        """Convert Anthropic-style tool schema to OpenAI format."""
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        }
