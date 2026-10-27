"""Google Gemini LLM provider.

Supports both direct Gemini API (API key) and Vertex AI (project_id + ADC).
Uses the google-genai SDK (``google.genai``).

Install: pip install google-genai  (or comes with google-cloud-aiplatform)
"""

import asyncio
import logging
from uuid import uuid4

from google import genai
from google.genai import types

from config import get_settings
from llm.base import LLMProvider

logger = logging.getLogger(__name__)

GEMINI_PRICING: dict[str, dict[str, float]] = {
    "gemini-3.1-pro-preview": {"input": 2.0, "output": 12.0},
    "gemini-2.5-pro-preview-06-05": {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash-preview-05-20": {"input": 0.15, "output": 0.60},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.0-flash-lite": {"input": 0.025, "output": 0.10},
}
DEFAULT_PRICING = {"input": 0.15, "output": 0.60}


class GeminiProvider(LLMProvider):
    """Google Gemini via the google-genai SDK."""

    def __init__(self):
        settings = get_settings()
        self.model = settings.gemini_model or "gemini-2.5-flash-preview-05-20"

        # Three auth modes:
        # 1. Vertex AI with explicit project
        # 2. Vertex AI via gcloud ADC (auto-detects project)
        # 3. Direct API key
        if settings.vertex_project_id:
            self.client = genai.Client(
                vertexai=True,
                project=settings.vertex_project_id,
                location=settings.vertex_region or "us-central1",
            )
        elif settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)
        else:
            # Try Vertex AI with ADC (gcloud auth application-default login)
            self.client = genai.Client(vertexai=True)

        # Pricing
        self._input_price_override = settings.gemini_input_price
        self._output_price_override = settings.gemini_output_price
        self._pricing = self._resolve_pricing()

        self.total_input_tokens = 0
        self.total_output_tokens = 0

        # Maps tool_use_id → function name for tool_result routing
        self._pending_tool_names: dict[str, str] = {}
        # Maps tool_use_id → thought_signature (Gemini 3.x thinking models)
        self._pending_thought_sigs: dict[str, str] = {}

    def _resolve_pricing(self) -> dict[str, float]:
        if self._input_price_override > 0 and self._output_price_override > 0:
            return {
                "input": self._input_price_override,
                "output": self._output_price_override,
            }
        return GEMINI_PRICING.get(self.model, DEFAULT_PRICING).copy()

    # ── public interface ────────────────────────────────────────────

    async def _call_with_retry(self, **kwargs):
        """Call generate_content with exponential backoff on 429s and a hard timeout."""
        from google.genai.errors import ClientError
        for attempt in range(5):
            try:
                return await asyncio.wait_for(
                    self.client.aio.models.generate_content(**kwargs),
                    timeout=300,  # 5-minute hard cap per API call
                )
            except asyncio.TimeoutError:
                if attempt < 4:
                    logger.warning("Gemini API timeout (attempt %d/5), retrying...", attempt + 1)
                    continue
                logger.error("Gemini API timed out on all attempts")
                raise
            except ClientError as e:
                if e.code == 429 and attempt < 4:
                    wait = 2 ** attempt + 1
                    logger.warning("Gemini 429 rate limit, retrying in %ds (attempt %d/5)", wait, attempt + 1)
                    await asyncio.sleep(wait)
                else:
                    raise

    async def analyze(self, prompt: str, context: str = "") -> str:
        content = f"Context:\n{context}\n\n{prompt}" if context else prompt
        response = await self._call_with_retry(
            model=self.model,
            contents=content,
        )
        self._track_usage(response)
        return response.text or ""

    async def generate_report(self, prompt: str) -> str:
        return await self.analyze(prompt)

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> float:
        cost = (input_tokens / 1_000_000) * self._pricing["input"]
        cost += (output_tokens / 1_000_000) * self._pricing["output"]
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
        contents = self._to_gemini_contents(messages)
        gemini_tools = self._to_gemini_tools(tools)

        config = types.GenerateContentConfig(
            tools=gemini_tools,
            max_output_tokens=max_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_budget=2048,
                include_thoughts=True,
            ),
        )

        try:
            response = await self._call_with_retry(
                model=self.model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            logger.error("Gemini chat_with_tools error: %s", e)
            return {
                "content": [{"type": "text", "text": f"[API ERROR] {type(e).__name__}: {e}"}],
                "thinking": "",
                "stop_reason": "end_turn",
                "usage": {"input": 0, "output": 0},
            }

        self._track_usage(response)
        return self._parse_response(response)

    # ── message normalization ───────────────────────────────────────

    def _to_gemini_contents(self, messages: list[dict]) -> list[types.Content]:
        """Convert Anthropic-style messages to Gemini Content objects.

        Preserves thought_signature on function_call parts (required by
        Gemini 3.x thinking models).
        """
        contents: list[types.Content] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            payload = msg.get("_provider_history")
            if role == "assistant" and payload and payload["provider"] == "GeminiProvider":
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_use":
                        self._pending_tool_names[block["id"]] = block["name"]
                contents.append(types.Content.model_validate(payload["data"]))
                continue

            # Plain text message
            if isinstance(content, str):
                gemini_role = "model" if role == "assistant" else "user"
                contents.append(types.Content(
                    role=gemini_role,
                    parts=[types.Part(text=content)],
                ))
                continue

            # List of content blocks
            if isinstance(content, list):
                parts: list[types.Part] = []
                is_tool_result = False

                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                    else:
                        btype = getattr(block, "type", None)

                    if btype == "text":
                        text = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                        if text:
                            parts.append(types.Part(text=text))

                    elif btype == "tool_use":
                        # Assistant requesting a tool call
                        if isinstance(block, dict):
                            name = block.get("name", "")
                            args = block.get("input", {})
                            tool_id = block.get("id", "")
                        else:
                            name = getattr(block, "name", "")
                            args = getattr(block, "input", {})
                            tool_id = getattr(block, "id", "")

                        self._pending_tool_names[tool_id] = name

                        part = types.Part(
                            function_call=types.FunctionCall(
                                name=name,
                                args=args,
                            )
                        )

                        # Restore thought_signature if we saved one
                        sig = self._pending_thought_sigs.get(tool_id)
                        if sig:
                            part.thought_signature = sig

                        parts.append(part)

                    elif btype == "tool_result":
                        is_tool_result = True
                        if isinstance(block, dict):
                            tool_use_id = block.get("tool_use_id", "")
                            result_content = block.get("content", "")
                        else:
                            tool_use_id = getattr(block, "tool_use_id", "")
                            result_content = getattr(block, "content", "")

                        fn_name = self._pending_tool_names.get(tool_use_id, "unknown")

                        # Multimodal tool result (e.g. screenshot): extract image + text
                        if isinstance(result_content, list):
                            text_parts_inner = []
                            for sub in result_content:
                                if isinstance(sub, dict) and sub.get("type") == "image":
                                    import base64 as _b64
                                    src = sub.get("source", {})
                                    img_bytes = _b64.b64decode(src.get("data", ""))
                                    parts.append(types.Part(
                                        inline_data=types.Blob(
                                            mime_type=src.get("media_type", "image/png"),
                                            data=img_bytes,
                                        )
                                    ))
                                elif isinstance(sub, dict) and sub.get("type") == "text":
                                    text_parts_inner.append(sub.get("text", ""))
                                else:
                                    text_parts_inner.append(str(sub))
                            result_dict = {"result": "\n".join(text_parts_inner) if text_parts_inner else "image attached"}
                        elif isinstance(result_content, str):
                            result_dict = {"result": result_content}
                        elif isinstance(result_content, dict):
                            result_dict = result_content
                        else:
                            result_dict = {"result": str(result_content)}

                        parts.append(types.Part(
                            function_response=types.FunctionResponse(
                                name=fn_name,
                                response=result_dict,
                            )
                        ))

                if parts:
                    gemini_role = "user" if (role == "user" or is_tool_result) else "model"
                    contents.append(types.Content(role=gemini_role, parts=parts))
                continue

            # Fallback
            gemini_role = "model" if role == "assistant" else "user"
            contents.append(types.Content(
                role=gemini_role,
                parts=[types.Part(text=str(content) if content else "")],
            ))

        return contents

    @staticmethod
    def _to_gemini_tools(tools: list[dict]) -> list[types.Tool]:
        """Convert Anthropic-style tool schemas to Gemini FunctionDeclarations."""
        declarations = []
        for tool in tools:
            schema = tool.get("input_schema", {})
            # Clean the schema: remove 'additionalProperties' which Gemini doesn't support
            cleaned = _clean_schema(schema)
            declarations.append(types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters=cleaned if cleaned.get("properties") else None,
            ))
        return [types.Tool(function_declarations=declarations)]

    # ── response parsing ────────────────────────────────────────────

    def _parse_response(self, response) -> dict:
        """Convert Gemini response back to Anthropic-style dict.

        Captures thought_signature from function_call parts so they can be
        replayed in the next request (required by Gemini 3.x thinking models).
        """
        if not response.candidates:
            reason = getattr(response, "prompt_feedback", None)
            logger.warning("Gemini returned no candidates (blocked? feedback=%s)", reason)
            return {
                "content": [{"type": "text", "text": "[API ERROR] Gemini returned no candidates (possibly blocked by safety filters)"}],
                "thinking": "",
                "stop_reason": "end_turn",
                "usage": {"input": 0, "output": 0},
            }

        candidate = response.candidates[0]
        content_blocks = []
        has_tool_calls = False

        thinking = ""

        parts = getattr(candidate.content, "parts", None) if candidate.content else None
        finish = getattr(candidate, "finish_reason", "unknown")
        if not parts:
            # MALFORMED_FUNCTION_CALL: model tried a tool call but produced
            # invalid JSON.  Return a soft retry hint instead of [API ERROR]
            # so ToolAgent doesn't nuke context with emergency compression.
            if "MALFORMED" in str(finish):
                logger.warning("Gemini malformed function call — asking model to retry")
                return {
                    "content": [{"type": "text", "text": (
                        "[MALFORMED_TOOL_CALL] Your previous tool call was malformed. "
                        "Please retry with simpler arguments. Make sure all JSON values "
                        "are properly quoted and the parameter names match the tool schema exactly."
                    )}],
                    "thinking": "",
                    "stop_reason": "end_turn",
                    "usage": {"input": 0, "output": 0},
                }
            logger.warning("Gemini candidate has no content parts (finish_reason=%s)", finish)
            return {
                "content": [{"type": "text", "text": f"[API ERROR] Gemini returned empty response (finish_reason={finish})"}],
                "thinking": "",
                "stop_reason": "end_turn",
                "usage": {"input": 0, "output": 0},
            }

        for part in parts:
            if part.thought:
                thinking = (thinking + "\n" + part.text).strip() if thinking else part.text
            elif part.text:
                content_blocks.append({"type": "text", "text": part.text})

            if part.function_call:
                has_tool_calls = True
                tool_id = uuid4().hex[:12]
                fn_name = part.function_call.name
                fn_args = dict(part.function_call.args) if part.function_call.args else {}

                self._pending_tool_names[tool_id] = fn_name

                # Capture thought_signature for this function call
                sig = getattr(part, "thought_signature", None)
                if sig:
                    self._pending_thought_sigs[tool_id] = sig

                content_blocks.append({
                    "type": "tool_use",
                    "id": tool_id,
                    "name": fn_name,
                    "input": fn_args,
                })

        usage = response.usage_metadata
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0

        result = {
            "content": content_blocks,
            "stop_reason": "tool_use" if has_tool_calls else "end_turn",
            "usage": {
                "input": input_tokens,
                "output": output_tokens,
            },
        }
        result["history"] = candidate.content.model_dump(mode="json", exclude_none=True)
        if thinking:
            result["thinking"] = thinking
        return result

    # ── helpers ─────────────────────────────────────────────────────

    def _track_usage(self, response) -> None:
        usage = response.usage_metadata
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        logger.debug("Gemini tokens: in=%d, out=%d", input_tokens, output_tokens)


def _clean_schema(schema: dict) -> dict:
    """Remove keys that Gemini's API doesn't accept (e.g. additionalProperties)."""
    if not isinstance(schema, dict):
        return schema
    cleaned = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {k: _clean_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            cleaned[key] = _clean_schema(value)
        else:
            cleaned[key] = value
    return cleaned
