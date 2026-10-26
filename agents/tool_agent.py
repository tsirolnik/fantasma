"""ToolAgent — base class for LLM-driven agentic tool-use execution.

When a tool-capable LLM is available, the agent enters an autonomous loop:
the LLM decides which tools to call based on observations, iterates until
it reaches a conclusion, and returns structured results with a full trace.

Falls back to the deterministic ``run()`` path when the LLM doesn't
support tool calling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import uuid4
import time
from dataclasses import dataclass, field
from typing import Any

from typing import Callable

from agents.base import BaseAgent
from agents.cli_adapter import CLIToolAdapter
from agents.messaging import MessageBus, TaskAssignment, TaskResult
from agents.tools import ToolRegistry
from llm.base import LLMProvider

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """Record of a single tool invocation."""

    tool_name: str
    input: dict
    output: Any = None
    duration_ms: float = 0.0
    error: str | None = None


@dataclass
class AgentTrace:
    """Full trace of an agentic run for observability."""

    agent_name: str
    assignment: TaskAssignment
    tool_calls: list[ToolCall] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    iterations: int = 0
    total_duration_ms: float = 0.0
    final_answer: str = ""
    final_reasoning: str | None = None
    exit_reason: str = ""  # llm_concluded | guardrail | max_iterations | exception


class ToolAgent(BaseAgent):
    """Base class for agents that use tools via LLM-driven execution loops.

    Extends BaseAgent. When tools + a tool-capable LLM are available,
    ``execute()`` uses the agentic loop. Otherwise falls back to the
    deterministic ``run()`` method.

    Subclasses should set:
        - ``tools``: a ToolRegistry instance
        - ``system_prompt``: instructions for the LLM
        - ``max_iterations``: loop cap (default 25)
    """

    tools: ToolRegistry
    system_prompt: str = ""
    max_iterations: int = 200
    max_tool_errors: int = 10  # consecutive errors before abort
    max_total_tool_calls: int = 500  # total tool call budget
    stale_window: int = 30  # consecutive fruitless calls before nudge
    compress_after: int = 4  # compress older messages after this many iterations
    keep_recent: int = 10  # keep this many recent iteration pairs uncompressed
    allow_self_conclude: bool = True  # if False, only guardrails/director can stop the agent

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        tools: ToolRegistry | None = None,
        system_prompt: str = "",
        max_iterations: int = 200,
        max_total_tool_calls: int = 500,
        cli_mode: bool = False,
        name: str | None = None,
        cost_tracker: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(bus, llm, name=name)
        if tools is not None:
            self.tools = tools
        from agents.mcp_runtime import attach_mcp_tools
        if getattr(self, "tools", None) is not None:
            attach_mcp_tools(self.tools)
        if system_prompt:
            self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.max_total_tool_calls = max_total_tool_calls
        self.cli_mode = cli_mode
        self._cli_adapter: CLIToolAdapter | None = None
        self._last_trace: AgentTrace | None = None
        self.on_tool_call: Callable[[ToolCall], None] | None = None
        self.on_tool_start: Callable | None = None  # (name, tool_name, tool_input)
        self.on_context_snapshot: Callable | None = None  # (messages, tool_schemas)
        self.on_llm_start: Callable | None = None  # (name, model, messages_count, tools_count)
        self.on_llm_call: Callable | None = None
        self.on_llm_exchange: Callable | None = None  # (name, model, messages, response, duration_ms)
        self.on_agent_done: Callable | None = None  # (name, iterations, tool_calls, duration_ms, exit_reason)
        self.cost_tracker = cost_tracker  # CostTracker instance for recording LLM costs
        # Directive queue — director can inject strategic redirects mid-run
        self._directive_queue: asyncio.Queue[str] = asyncio.Queue()
        # Sticky directives — survive context compression, re-injected every LLM call
        # Each entry: (text, remaining_iterations)
        self._sticky_directives: list[tuple[str, int]] = []
        # Pause event — when cleared, agent loop blocks until resumed
        self._pause_event: asyncio.Event | None = None
        # Guardrail state — reset per agentic run
        self._seen_calls: dict[str, str] = {}  # fingerprint → truncated result
        # TTL result cache for idempotent read tools: fingerprint → (timestamp, full result)
        self._call_cache: dict[str, tuple[float, str]] = {}
        self.tool_cache_ttl: int = 90  # overwritten from settings below
        self._recent_outcomes: list[bool] = []  # True=useful, False=error/empty
        # Durable "already tried / dead ends" memory, surfaced in the per-turn
        # state digest so it survives context compression. Tracks the actual
        # NETWORK host hit (extracted from command/code/url), not the tool
        # fingerprint — so identical calls buried in varying scripts still count.
        self._endpoint_hits: dict[str, int] = {}   # host → times hit
        self._rate_limited: set[str] = set()       # hosts that returned 429/rate-limit
        # Separate from _endpoint_hits: a scope/out-of-scope BLOCKED call often
        # bundles a DIFFERENT set of host names each time (e.g. varying subfinder
        # output), so no single host's hit-count ever crosses the dead-end
        # threshold even though the same mistake (guessing unconfirmed hosts) is
        # repeating. This counts the MISTAKE itself, independent of which hosts
        # were named.
        self._scope_violations: int = 0
        # Rebuild-context mode: assemble a fresh briefing every turn instead of
        # accumulating+compressing history. Set True by PenTesterAgent.
        self.context_mode: bool = False
        self.distill_llm = None                    # cheap LLM for the state distiller
        self.distill_every: int = 4                # re-distill established/leads every N turns
        self._last_results_text: str = ""          # verbatim last tool output(s) for the briefing
        self._raw_notes: str = ""                  # accumulated notes for the distiller
        self._distilled: tuple[list, list] = ([], [])  # (established, leads)
        self.in_scope: list[str] = []              # target list for the Goal/Scope block
        self.out_of_scope: list[str] = []
        self.session_budget: float = 0.0           # for the budget line
        self._stale_nudged: bool = False  # only nudge once per run
        self._consecutive_dupes: int = 0  # consecutive duplicate tool calls
        self._dupe_pivoted: bool = False  # whether we've injected a pivot message
        # Target-hopping detection
        self._claimed_targets: list[str] = []  # targets claimed in order
        self._attack_calls: int = 0  # count of actual attack tool calls
        self._recon_calls: int = 0  # count of recon-only tool calls
        self._hop_warned: bool = False  # whether we've warned about hopping
        # Scope and claim enforcement
        self._shared_memory = None                     # SharedMemory ref for claim checking
        self._cancel_reason: str = ""                    # set by monitor before task.cancel()
        self._allowed_domains: set[str] | None = None  # None = no enforcement
        # Per-category tool budgets — None = unlimited
        self.tool_budgets: dict[str, int | None] | None = None
        self._tool_category_usage: dict[str, int] = {}  # category → calls made
        # Optional LLM for smarter context compression (e.g. the director's LLM)
        self.compress_llm: Any = None
        # Max tokens the LLM may generate per call (reasoning + text + tool args)
        from config import get_settings as _get_settings
        _settings = _get_settings()
        self.max_response_tokens: int = _settings.max_response_tokens
        self.tool_cache_ttl = _settings.tool_cache_ttl
        # Provider-aware compression: scale how much history/evidence we keep to
        # the model's context window. Small windows compress early and truncate
        # hard; large windows (GLM 1M, Qwen3.5-397B, deepseek-flash) keep far
        # more verbatim so we stop shredding HTTP responses the model could
        # easily hold. These base tiers assume a LOCAL model (Ollama): the
        # budget isn't just "does the context window fit it" — holding a big
        # verbatim history in-process ALSO costs host RAM, and with 2-3 local
        # agents each holding verbatim contexts + Playwright + Docker
        # containers, 32K×40 tipped a 32GB M4 into heavy swap and got the run
        # OOM-killed. That RAM constraint doesn't apply to a REMOTE API model
        # (Novita/OpenAI-compatible/Claude/Gemini/Vertex) — the only local cost
        # there is Python strings, not a loaded model. generous_context is the
        # explicit, never-auto-inferred opt-in for that case (see config.py) —
        # it must be set on purpose per run, so memory footprint never changes
        # silently just because a different provider happens to be configured.
        ctx = _settings.agent_context_window
        _TIERS = [  # (min context window, compress_after, result_char_budget)
            (500_000, 24, 16000),
            (200_000, 20, 12000),
            (128_000, 16, 8000),
            (64_000, 10, 4000),
            (32_000, 6, 2000),
            (0, 4, 800),
        ]
        compress_after, result_char_budget = next(
            (ca, rcb) for min_ctx, ca, rcb in _TIERS if ctx >= min_ctx
        )
        if _settings.generous_context:
            compress_after *= 4
            result_char_budget *= 4
        self.compress_after = compress_after
        self._result_char_budget = result_char_budget

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, assignment: TaskAssignment) -> TaskResult:
        """Execute a task: use agentic loop if possible, else deterministic."""
        if self.llm is not None and self.llm.supports_tool_use and self.tools and len(self.tools) > 0:
            return await self._execute_with_tools(assignment)
        # Fallback to deterministic BaseAgent.execute()
        return await super().execute(assignment)

    def inject_directive(self, text: str) -> None:
        """Inject a strategic redirect from the director into this agent's loop.

        The directive will be consumed before the next LLM call and appended
        as a user message so the LLM can adjust its strategy.
        """
        self._directive_queue.put_nowait(text)

    @property
    def last_trace(self) -> AgentTrace | None:
        """Access the trace from the most recent agentic run."""
        return self._last_trace

    # ------------------------------------------------------------------
    # Reflection override — reuse agentic loop's final answer
    # ------------------------------------------------------------------

    async def reflect(self, result: Any, assignment: TaskAssignment) -> str:
        """Reuse the agentic loop's final LLM text instead of a separate call.

        The agentic loop already produces a final text with analysis.
        Making a separate ``llm.analyze()`` call is redundant. Falls back
        to the base ``reflect()`` when no agentic trace is available
        (deterministic path).
        """
        if self._last_trace and self._last_trace.final_answer:
            return self._last_trace.final_answer
        return await super().reflect(result, assignment)

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    async def _execute_with_tools(self, assignment: TaskAssignment) -> TaskResult:
        """Run the full agentic tool-use loop."""
        start = time.monotonic()
        trace = AgentTrace(agent_name=self.name, assignment=assignment)
        # Expose trace immediately so status polling can read live stats
        self._last_trace = trace
        # Reset per-run guardrail state
        self._seen_calls.clear()
        self._call_cache.clear()
        self._endpoint_hits.clear()
        self._rate_limited.clear()
        self._scope_violations = 0
        self._recent_outcomes.clear()
        self._stale_nudged = False
        self._claimed_targets.clear()
        self._attack_calls = 0
        self._recon_calls = 0
        self._hop_warned = False
        self._tool_category_usage.clear()

        try:
            result, final_text = await self._run_with_tools(assignment, trace)
        except asyncio.CancelledError:
            reason = self._cancel_reason or "cancelled"
            logger.warning("%s: agent cancelled (%s)", self.name, reason)
            trace.total_duration_ms = (time.monotonic() - start) * 1000
            trace.exit_reason = f"cancelled: {reason}"
            self._last_trace = trace
            self._fire_agent_done(trace)
            return TaskResult(
                agent_name=self.name,
                task_type=assignment.task_type,
                success=False,
                error=reason,
            )
        except Exception as exc:
            logger.exception("%s: agentic loop failed", self.name)
            trace.total_duration_ms = (time.monotonic() - start) * 1000
            trace.exit_reason = f"exception: {exc}"
            self._last_trace = trace
            self._fire_agent_done(trace)
            return TaskResult(
                agent_name=self.name,
                task_type=assignment.task_type,
                success=False,
                error=str(exc),
            )

        trace.total_duration_ms = (time.monotonic() - start) * 1000
        trace.final_answer = final_text
        self._last_trace = trace
        self._fire_agent_done(trace)

        # Keep for handle_question context
        self._recent_results.append(result)
        if len(self._recent_results) > 3:
            self._recent_results.pop(0)

        reflection = await self.reflect(result, assignment)

        from agents.messaging import AgentMessage

        self.bus.send(AgentMessage(
            sender=self.name,
            recipient="director",
            msg_type="task_result",
            content=reflection,
            data={"task_type": assignment.task_type, "tool_calls": len(trace.tool_calls)},
        ))

        return TaskResult(
            agent_name=self.name,
            task_type=assignment.task_type,
            data=result,
            reflection=reflection,
        )

    def _fire_agent_done(self, trace: AgentTrace) -> None:
        """Fire the on_agent_done hook with trace summary."""
        logger.info(
            "%s: agent done — %d iterations, %d tool calls, %.1fs, exit=%s",
            self.name, trace.iterations, len(trace.tool_calls),
            trace.total_duration_ms / 1000, trace.exit_reason,
        )
        if self.on_agent_done is not None:
            try:
                self.on_agent_done(
                    self.name, trace.iterations, len(trace.tool_calls),
                    trace.total_duration_ms, trace.exit_reason,
                )
            except Exception:
                logger.exception("on_agent_done hook error")

    async def _run_with_tools(
        self, assignment: TaskAssignment, trace: AgentTrace
    ) -> tuple[Any, str]:
        """The core agentic loop.

        Returns (parsed_result, raw_final_text).
        """
        # CLI mode: single "run" tool instead of full schema set
        if self.cli_mode:
            self._cli_adapter = CLIToolAdapter(self.tools)
        else:
            self._cli_adapter = None

        # Build initial conversation
        messages: list[dict] = self._build_initial_messages(assignment)
        if self.cli_mode and self._cli_adapter:
            tool_schemas = self._cli_adapter.tool_schema()
        else:
            tool_schemas = self.tools.to_anthropic_schema()
        consecutive_errors = 0
        consecutive_api_errors = 0

        for iteration in range(self.max_iterations):
            trace.iterations = iteration + 1

            # Pause check — block until resumed if pipeline is paused
            if self._pause_event is not None:
                if not self._pause_event.is_set():
                    logger.info("%s: paused at iteration %d", self.name, iteration + 1)
                await self._pause_event.wait()

            if self.context_mode:
                # Rebuild the WHOLE briefing from live state every turn. The block
                # is the memory — no accumulated/compressed history, no separate
                # digest/directive injections (all folded into the assembled block).
                messages = [{
                    "role": "user",
                    "content": await self._assemble_turn_context(iteration + 1),
                }]
            else:
                # Drain director redirects — add to sticky list with TTL
                while not self._directive_queue.empty():
                    try:
                        directive = self._directive_queue.get_nowait()
                        self._sticky_directives.append((directive, 5))  # expires after 5 iterations
                        logger.info("%s: new director directive (now %d active): %s",
                                    self.name, len(self._sticky_directives), directive[:120])
                    except asyncio.QueueEmpty:
                        break

                # Inject active directives and tick down their TTL
                if self._sticky_directives:
                    # Remove any previous sticky directive message
                    messages = [m for m in messages if not (
                        isinstance(m.get("content"), str)
                        and m.get("content", "").startswith("[DIRECTOR]")
                    )]
                    directives_text = "\n".join(
                        f"- {d}" for d, _ in self._sticky_directives[-3:]  # keep last 3
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            f"[DIRECTOR] ⚠️ ALERT — you are being redirected. Act on this NOW, "
                            f"do not just acknowledge or repeat it back:\n"
                            f"{directives_text}"
                        ),
                    })
                    # Tick down TTL, remove expired
                    self._sticky_directives = [
                        (d, ttl - 1) for d, ttl in self._sticky_directives if ttl > 1
                    ]

                # Inject a fresh self-orientation digest so the agent stays anchored
                # to ITS OWN investigation even after context compression.
                if getattr(self, "_shared_memory", None) is not None:
                    messages = [m for m in messages if not (
                        isinstance(m.get("content"), str)
                        and m.get("content", "").startswith("[YOUR STATE]")
                    )]
                    digest = self._build_progress_digest(iteration + 1)
                    if digest:
                        messages.append({"role": "user", "content": digest})

            # Call LLM with tools
            import time as _time
            _ctx_chars = sum(len(str(m.get("content", ""))) for m in messages)
            _model_name = getattr(self.llm, "model", "?")
            logger.info(
                "%s: LLM call #%d — %d msgs, ~%dK chars, model=%s",
                self.name, iteration + 1, len(messages), _ctx_chars // 1000, _model_name,
            )
            request_messages = messages if getattr(self.llm, "supports_reasoning_history", False) else [
                {k: v for k, v in m.items() if k != "reasoning_content"} for m in messages
            ]
            if self.on_context_snapshot is not None:
                try:
                    self.on_context_snapshot(request_messages, tool_schemas)
                except Exception:
                    logger.exception("Context inspection callback failed")
            if self.on_llm_start is not None:
                try:
                    self.on_llm_start(self.name, str(_model_name), len(messages), len(tool_schemas))
                except Exception:
                    pass
            _llm_start = _time.monotonic()
            response = await self.llm.chat_with_tools(
                messages=request_messages,
                tools=tool_schemas,
                max_tokens=self.max_response_tokens,
            )
            _llm_ms = int((_time.monotonic() - _llm_start) * 1000)
            logger.info("%s: LLM responded in %dms", self.name, _llm_ms)

            content = response["content"]

            # Record cost via the provider's own pricing
            usage = response.get("usage", {})
            if usage and self.cost_tracker and self.llm:
                in_tok = usage.get("input", 0)
                out_tok = usage.get("output", 0)
                cached_tok = usage.get("cached_input", 0)
                cost = self.llm.estimate_cost(in_tok, out_tok, cached_tok)
                model_name = getattr(self.llm, "model", "unknown")
                provider_name = type(self.llm).__name__
                cache_note = f" cached={cached_tok}" if cached_tok else ""
                self.cost_tracker.add_cost(
                    "llm_api", cost,
                    detail=f"{self.name} {provider_name}/{model_name} in={in_tok}{cache_note} out={out_tok}",
                )

            # Separate text blocks from tool_use blocks
            # Handle both Anthropic SDK objects and plain dicts
            text_parts = []
            tool_blocks = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_blocks.append(block)
                else:
                    # Anthropic SDK content block objects
                    if getattr(block, "type", None) == "text":
                        text_parts.append(block.text)
                    elif getattr(block, "type", None) == "tool_use":
                        tool_blocks.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })

            # Fallback: local models sometimes emit tool calls as raw XML text
            # (in content or thinking) instead of structured tool_use blocks.
            if not tool_blocks:
                import re as _re
                _thinking = response.get("thinking", "") or ""
                _raw = "\n".join(text_parts) + ("\n" + _thinking if _thinking else "")
                _xml_calls = _re.findall(
                    r'<tool_call>\s*\{["\']?name["\']?\s*:\s*["\']([^"\']+)["\'].*?["\']?arguments["\']?\s*:\s*(\{.*?\})\s*\}\s*</tool_call>',
                    _raw, _re.DOTALL,
                )
                if not _xml_calls:
                    # Also handle <function=name>\n<parameter=key>\nvalue format
                    _fn_calls = _re.findall(
                        r'<function=(\w+)>(.*?)</function>',
                        _raw, _re.DOTALL,
                    )
                    for fn_name, fn_body in _fn_calls:
                        params = {}
                        for pk, pv in _re.findall(r'<parameter=(\w+)>\s*(.*?)\s*(?:</parameter>|$)', fn_body, _re.DOTALL):
                            pv = pv.strip()
                            # Try to parse as JSON value
                            try:
                                params[pk] = json.loads(pv)
                            except (json.JSONDecodeError, ValueError):
                                params[pk] = pv
                        tool_blocks.append({
                            "type": "tool_use",
                            "id": uuid4().hex[:12],
                            "name": fn_name,
                            "input": params,
                        })
                else:
                    for fn_name, fn_args_str in _xml_calls:
                        try:
                            args = json.loads(fn_args_str)
                        except json.JSONDecodeError:
                            args = {}
                        tool_blocks.append({
                            "type": "tool_use",
                            "id": uuid4().hex[:12],
                            "name": fn_name,
                            "input": args,
                        })
                if tool_blocks:
                    # Clean parsed XML from text_parts only (not thinking)
                    if text_parts:
                        _tp_raw = "\n".join(text_parts)
                        cleaned = _re.sub(r'<tool_call>.*?</tool_call>', '', _tp_raw, flags=_re.DOTALL)
                        cleaned = _re.sub(r'<function=\w+>.*?</function>', '', cleaned, flags=_re.DOTALL)
                        cleaned = cleaned.strip()
                        text_parts = [cleaned] if cleaned else []
                    logger.info(
                        "%s: recovered %d tool call(s) from raw XML text",
                        self.name, len(tool_blocks),
                    )

            reasoning_metadata = ({"reasoning_content": response["reasoning_content"]}
                                  if isinstance(response.get("reasoning_content"), str) else {})

            # Broadcast LLM call event (prefer thinking content over response text)
            thinking = response.get("thinking", "") or ""
            reasoning_text = thinking if thinking else ("\n".join(text_parts) if text_parts else "")
            # Strip raw <tool_call> XML that local models may echo in text
            if "<tool_call>" in reasoning_text:
                import re
                reasoning_text = re.sub(
                    r"<tool_call>.*?</tool_call>", "", reasoning_text, flags=re.DOTALL
                ).strip()
            # Fallback: if reasoning is empty but tool calls exist, show tool names
            if not reasoning_text and tool_blocks:
                reasoning_text = "Calling: " + ", ".join(
                    b.get("name", "?") for b in tool_blocks
                )
            if self.on_llm_call is not None:
                try:
                    model_name = getattr(self.llm, "model", "") or getattr(self.llm, "model_name", "unknown")
                    self.on_llm_call(self.name, str(model_name), len(messages), len(tool_schemas), _llm_ms, reasoning_text)
                except Exception:
                    pass

            # Full LLM exchange for wire-level debugging (dashboard LLM tab)
            if self.on_llm_exchange is not None:
                try:
                    model_name = getattr(self.llm, "model", "") or getattr(self.llm, "model_name", "unknown")
                    self.on_llm_exchange(self.name, str(model_name), messages, response, _llm_ms)
                except Exception:
                    pass

            # Detect LLM errors (timeout or API error) — compress context and retry
            _full_text = "\n".join(text_parts)

            # Malformed tool call (Gemini): just feed the hint back so the
            # model retries — no context compression needed.
            if not tool_blocks and ("[MALFORMED_TOOL_CALL]" in _full_text or "[Tool call failed:" in _full_text):
                logger.warning(
                    "%s: malformed tool call at iteration %d — retrying without compression",
                    self.name, iteration + 1,
                )
                messages.append({"role": "assistant", "content": _full_text, **reasoning_metadata})
                messages.append({"role": "user", "content": (
                    "Your tool call was malformed. Please try again — use exact "
                    "parameter names from the tool schema and ensure valid JSON."
                )})
                consecutive_errors += 1
                continue

            if not tool_blocks and ("[TIMEOUT]" in _full_text or "[API ERROR]" in _full_text):
                error_type = "timed out" if "[TIMEOUT]" in _full_text else "API error"
                consecutive_api_errors += 1
                if consecutive_api_errors >= 3:
                    raise RuntimeError(f"LLM failed after 3 consecutive API errors: {_full_text}")
                logger.warning(
                    "%s: LLM %s at iteration %d — compressing context (%d msgs) and retrying",
                    self.name, error_type, iteration + 1, len(messages),
                )
                # Force aggressive compression: keep only the last 1 pair
                before = len(messages)
                messages = self._compress_messages(messages, keep_recent=1)
                logger.info(
                    "%s: emergency compression %d → %d messages",
                    self.name, before, len(messages),
                )
                consecutive_errors += 1
                continue  # retry with compressed context

            consecutive_api_errors = 0

            if not tool_blocks:
                # Before finishing, check for pending director directives
                if not self._directive_queue.empty():
                    if text_parts:
                        messages.append({"role": "assistant", "content": "\n".join(text_parts), **reasoning_metadata})
                    while not self._directive_queue.empty():
                        try:
                            directive = self._directive_queue.get_nowait()
                            self._sticky_directives.append((directive, 5))
                            logger.info("%s: late directive added (now %d active): %s",
                                        self.name, len(self._sticky_directives), directive[:120])
                        except asyncio.QueueEmpty:
                            break
                    continue  # Re-enter the loop so the LLM sees the directives

                # Block self-conclusion if not allowed
                if not self.allow_self_conclude:
                    logger.info(
                        "%s: tried to conclude at iteration %d — pushing back",
                        self.name, trace.iterations,
                    )
                    messages.append({"role": "assistant", "content": "\n".join(text_parts) if text_parts else "I'll wrap up.", **reasoning_metadata})
                    capability = getattr(self, "capability", "")
                    if "valid" in capability:
                        messages.append({"role": "user", "content": (
                            "You cannot conclude — pentesters are still running and may submit new findings. "
                            "Call read_findings(status='unverified') to check for new unverified findings to validate. "
                            "If you've verified all current findings, re-check — new ones may have appeared. "
                            "Only the director or budget exhaustion can stop you."
                        )})
                    else:
                        messages.append({"role": "user", "content": (
                            "This role is configured to await director completion. Report completed work or blocked prerequisites "
                            "through shared memory. Continue only with an evidence-supported action within the assignment; "
                            "do not run unrelated checks to consume remaining budget."
                        )})
                    continue

                # LLM is done — return the final text
                final_text = "\n".join(text_parts)
                result = self._parse_final_answer(final_text)
                trace.final_reasoning = reasoning_metadata.get("reasoning_content")
                trace.exit_reason = "llm_concluded"
                trace.messages = messages
                logger.info(
                    "%s: concluded after %d iterations, %d tool calls",
                    self.name, trace.iterations, len(trace.tool_calls),
                )
                return result, final_text

            # Execute tool calls in parallel (with duplicate detection)
            tool_results: list[dict | None] = [None] * len(tool_blocks)
            # Synchronous pre-pass: CLI parsing + duplicate detection
            pending: list[tuple[int, str, dict, str]] = []  # (idx, tool_name, tool_input, fingerprint)
            for i, block in enumerate(tool_blocks):
                tool_name = block["name"]
                tool_input = block.get("input", {})

                # ── CLI mode: intercept "run" calls ──
                if self.cli_mode and self._cli_adapter and tool_name == "run":
                    command = tool_input.get("command", "")
                    try:
                        real_name, real_params = self._cli_adapter.parse(command)
                    except Exception as exc:
                        tool_results[i] = {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": f"Failed to parse command: {exc}",
                            "is_error": True,
                        }
                        self._recent_outcomes.append(False)
                        continue
                    tool_name = real_name
                    tool_input = real_params

                # ── Duplicate handling: TTL cache for reads, allow-repeat for tests ──
                # The agent can force a fresh (non-cached) execution by passing
                # force_refresh=true (or no_cache=true) on any call. Strip it BEFORE
                # fingerprinting/executing so it neither pollutes the cache key nor
                # reaches the underlying tool (which doesn't know the param).
                force_fresh = False
                if isinstance(tool_input, dict):
                    force_fresh = bool(
                        tool_input.pop("force_refresh", False)
                        or tool_input.pop("no_cache", False)
                    )

                fingerprint = self._call_fingerprint(tool_name, tool_input)
                cacheable = self._is_cacheable(tool_name, tool_input)

                # (1) TTL cache hit: an idempotent read repeated within the TTL is
                #     served from cache for free — no re-execution, no loop penalty.
                #     Re-referencing the same data mid-investigation is normal work.
                #     force_refresh=true skips the cache and re-fetches.
                if cacheable and not force_fresh:
                    entry = self._call_cache.get(fingerprint)
                    if entry is not None and (time.time() - entry[0]) < self.tool_cache_ttl:
                        age = int(time.time() - entry[0])
                        self._consecutive_dupes = 0
                        tool_results[i] = {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": (
                                f"[CACHED — this exact {tool_name} call was made {age}s ago; "
                                "the identical result below was served WITHOUT re-fetching (saving time+budget). "
                                "If you changed state or need fresh data, call it again with force_refresh=true.]\n"
                                f"{entry[1]}"
                            ),
                            "is_error": False,
                        }
                        self._recent_outcomes.append(True)
                        continue

                # (2) Non-cacheable repeats (fuzz/docker/test/POST) are ALLOWED to run
                #     — a re-sent identical request can BE the test (race, rate-limit,
                #     timing, idempotency). We only TRACK consecutive identical calls so
                #     the >=12 abort below can catch a genuinely stuck loop; we don't
                #     block, and we no longer accumulate a lifetime count that killed
                #     productive long runs. Idempotent-but-stale reads also fall through
                #     to re-execute and refresh the cache.
                if fingerprint in self._seen_calls and not cacheable:
                    self._consecutive_dupes += 1
                    if self._consecutive_dupes >= 5:
                        logger.info(
                            "%s: %d consecutive identical %s calls (allowed — may be intentional)",
                            self.name, self._consecutive_dupes, tool_name,
                        )

                # Check per-category tool budget
                budget_msg = self._check_tool_budget(tool_name)
                if budget_msg:
                    tool_results[i] = {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": budget_msg,
                        "is_error": True,
                    }
                    self._recent_outcomes.append(False)
                    continue

                # Pre-register fingerprint to catch same-batch duplicates
                self._seen_calls[fingerprint] = "(pending)"
                pending.append((i, tool_name, tool_input, fingerprint))

            # Parallel execution of non-duplicate calls
            if pending:
                # Broadcast tool starts for dashboard
                if self.on_tool_start is not None:
                    for _, t_name, t_input, _ in pending:
                        try:
                            self.on_tool_start(self.name, t_name, t_input)
                        except Exception:
                            pass

                async def _run_one(idx: int, t_name: str, t_input: dict, fp: str) -> tuple[int, ToolCall, str]:
                    try:
                        tc = await asyncio.wait_for(
                            self._execute_tool(t_name, t_input),
                            timeout=300,  # 5-minute hard cap per tool call
                        )
                    except asyncio.TimeoutError:
                        tc = ToolCall(tool_name=t_name, input=t_input, error="Tool timed out (300s)")
                        logger.warning("%s: tool %s timed out after 300s", self.name, t_name)
                    if tc.error:
                        result_text = f"Error: {tc.error}"
                    else:
                        result_text = json.dumps(tc.output, default=str)
                    # Update fingerprint cache with real result
                    preview = str(tc.output if not tc.error else tc.error)[:200]
                    self._seen_calls[fp] = preview
                    # Store the FULL result in the TTL cache for idempotent reads so
                    # an identical repeat is served free. Cap per-entry size (memory)
                    # and evict expired entries so the cache stays bounded.
                    if not tc.error and self._is_cacheable(t_name, t_input):
                        cap = getattr(self, "_result_char_budget", 16000)
                        self._call_cache[fp] = (time.time(), result_text[:cap])
                        if len(self._call_cache) > 256:
                            now = time.time()
                            self._call_cache = {
                                k: v for k, v in self._call_cache.items()
                                if now - v[0] < self.tool_cache_ttl
                            }
                    # Durable dead-end memory: record the actual network host(s)
                    # this call touched (extracted from command/code/url — even when
                    # buried in an execute_code script) and whether the result shows
                    # a rate-limit / ban, so the per-turn digest can tell the agent
                    # to STOP repeating it (survives context compression).
                    try:
                        self._record_endpoint_activity(t_name, t_input, tc, result_text)
                    except Exception:
                        pass
                    return idx, tc, result_text

                completed = await asyncio.gather(
                    *(_run_one(idx, tn, ti, fp) for idx, tn, ti, fp in pending),
                    return_exceptions=True,
                )

                any_succeeded = False
                for result in completed:
                    if isinstance(result, Exception):
                        logger.error("%s: parallel tool execution error: %s", self.name, result)
                        continue
                    idx, tc, result_text = result
                    trace.tool_calls.append(tc)
                    block = tool_blocks[idx]

                    if tc.error:
                        tool_results[idx] = {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": result_text,
                            "is_error": True,
                        }
                        self._recent_outcomes.append(False)
                    else:
                        any_succeeded = True
                        # Check for image content (e.g. browser_screenshot)
                        if isinstance(tc.output, dict) and "__image__" in tc.output:
                            img_b64 = tc.output["__image__"]
                            media_type = tc.output.get("__media_type__", "image/png")
                            meta = {k: v for k, v in tc.output.items() if not k.startswith("__")}
                            tool_results[idx] = {
                                "type": "tool_result",
                                "tool_use_id": block["id"],
                                "content": [
                                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                                    {"type": "text", "text": json.dumps(meta, default=str)},
                                ],
                            }
                        else:
                            tool_results[idx] = {
                                "type": "tool_result",
                                "tool_use_id": block["id"],
                                "content": result_text,
                            }
                        is_empty = not result_text or result_text.strip() in ("", "{}", "[]", "null", "None")
                        # For attack/testing tools, empty = negative result = valid test (not fruitless).
                        # Only recon tools with empty results count as fruitless.
                        _ATTACK_TOOLS = {"fuzz_parameter", "test_ssrf", "test_header_injection",
                                         "test_cors_origin", "test_open_redirect", "check_sensitive_path",
                                         "http_fetch", "curl_request", "run_docker_tool",
                                         "execute_python", "regex_scan_url"}
                        if is_empty and tc.tool_name in _ATTACK_TOOLS:
                            self._recent_outcomes.append(True)  # negative result is valid
                        else:
                            self._recent_outcomes.append(not is_empty)

                # Track recon vs attack tool usage for hop detection
                _RECON_TOOLS = {
                    "extract_links", "extract_forms", "extract_html_comments",
                    "extract_headers", "extract_js_urls", "fingerprint_tech",
                    "scan_tech_fingerprint", "check_domain", "dns_resolve",
                    "crtsh_lookup", "dns_bruteforce", "enumerate_subdomains",
                    "read_discoveries", "read_session_memory", "check_claimed",
                    "claim_target", "post_discovery", "http_fetch", "http_probe",
                    "check_ports",
                }
                for _, t_name, t_input, _ in pending:
                    if t_name == "claim_target":
                        key = t_input.get("key", "")
                        if key and key not in self._claimed_targets:
                            self._claimed_targets.append(key)
                    if t_name in _RECON_TOOLS:
                        self._recon_calls += 1
                    else:
                        self._attack_calls += 1

                # Update consecutive_errors: reset if any succeeded, else accumulate
                if any_succeeded:
                    consecutive_errors = 0
                    self._consecutive_dupes = 0  # reset duplicate streak on success
                    self._dupe_pivoted = False   # re-arm pivot for any future loop
                else:
                    consecutive_errors += len(pending)

            # Filter out None entries (shouldn't happen, but be safe)
            tool_results = [r for r in tool_results if r is not None]

            if self.context_mode:
                # Don't accumulate history — capture this turn's output for the
                # NEXT turn's briefing (recent_results) and feed the distiller.
                parts = []
                for blk, res in zip(tool_blocks, tool_results):
                    name = blk.get("name", "?")
                    args = json.dumps(blk.get("input", {}), default=str)[:160]
                    body = res.get("content", "")
                    if isinstance(body, list):  # multimodal (e.g. screenshot)
                        body = " ".join(p.get("text", "[image]") for p in body if isinstance(p, dict))
                    parts.append(f"$ {name}({args})\n{str(body)[:2000]}")
                self._last_results_text = "\n\n".join(parts)
                self._raw_notes = (self._raw_notes + "\n" + self._last_results_text)[-12000:]
                # brief assistant text (reasoning) is useful context too
                _txt = "\n".join(text_parts).strip() if text_parts else ""
                if _txt:
                    self._raw_notes = (self._raw_notes + f"\n[note] {_txt[:500]}")[-12000:]
                continue  # skip history append + compression entirely

            # Append assistant message + tool results
            messages.append({"role": "assistant", "content": content, **reasoning_metadata})
            messages.append({"role": "user", "content": tool_results})

            # Compress older messages to prevent context overflow
            # Prefer LLM compression (preserves evidence quality) over truncation
            if iteration >= self.compress_after and iteration % 2 == 0:
                before = len(messages)
                _compress_llm = self.compress_llm or self.llm
                if _compress_llm and hasattr(_compress_llm, 'analyze'):
                    messages = await self._compress_messages_llm(messages, self.keep_recent)
                else:
                    messages = self._compress_messages(messages, self.keep_recent)
                if len(messages) < before:
                    logger.debug(
                        "%s: compressed context %d → %d messages",
                        self.name, before, len(messages),
                    )

            # Stuck-loop pivot — a genuine tight loop (consecutive identical calls,
            # not a lifetime count). Compress context and force a strategy change
            # once; if the loop persists to 12, abort. Cacheable reads never reach
            # here (served from cache); this only trips on a real repeat loop.
            if self._consecutive_dupes >= 7 and not self._dupe_pivoted:
                self._dupe_pivoted = True
                logger.warning(
                    "%s: %d consecutive duplicate calls — forcing strategy pivot",
                    self.name, self._consecutive_dupes,
                )
                before = len(messages)
                messages = self._compress_messages(messages, keep_recent=1)
                messages.append({"role": "user", "content": (
                    f"SYSTEM: You have repeated the same tool call {self._consecutive_dupes} times. "
                    "Your context has been compressed. You are stuck in a loop. "
                    "CHANGE YOUR APPROACH COMPLETELY. What you were doing is not working. "
                    "Try a different part of the target, a different technique, or a different tool. "
                    "Think about what the application does and where the real bugs might be."
                )})
                logger.info("%s: pivot compression %d → %d messages", self.name, before, len(messages))
            elif self._consecutive_dupes >= 12:
                # Genuinely stuck: 12 identical calls in a row even after the pivot
                # nudge. (Was: _total_dupes >= 30 cumulative — that killed productive
                # 80+ iteration runs from scattered lifetime dupes.)
                logger.warning("%s: %d consecutive duplicates — aborting", self.name, self._consecutive_dupes)
                final_text = "\n".join(text_parts) if text_parts else "Aborted: stuck in a duplicate-call loop."
                result = self._parse_final_answer(final_text)
                trace.exit_reason = f"guardrail: {self._consecutive_dupes} consecutive duplicates"
                trace.messages = messages
                return result, final_text

            # Target-hopping detection: too many targets claimed, not enough attacks
            total_calls = self._recon_calls + self._attack_calls
            if (
                not self._hop_warned
                and len(self._claimed_targets) >= 2
                and total_calls >= 10
                and self._attack_calls < self._recon_calls
            ):
                self._hop_warned = True
                last_target = self._claimed_targets[-1].replace("scan:", "")
                logger.warning(
                    "%s: target-hopping detected (%d targets, %d recon vs %d attack calls) — forcing deep test",
                    self.name, len(self._claimed_targets), self._recon_calls, self._attack_calls,
                )
                messages.append({"role": "user", "content": (
                    f"SYSTEM WARNING: You are target-hopping. You've claimed {len(self._claimed_targets)} "
                    f"targets but made {self._attack_calls} attack calls vs {self._recon_calls} recon calls. "
                    f"STOP claiming new targets. Focus on {last_target} and go DEEPER. "
                    "Interact with the target, test specific hypotheses, look for real bugs. "
                    "Do NOT claim another target until you have thoroughly tested this one."
                )})

            # Check guardrails
            guardrail_msg = self._check_guardrails(iteration, consecutive_errors)
            if guardrail_msg:
                logger.warning("%s: guardrail tripped — %s", self.name, guardrail_msg)
                final_text = "\n".join(text_parts) if text_parts else guardrail_msg
                result = self._parse_final_answer(final_text)
                trace.exit_reason = f"guardrail: {guardrail_msg}"
                trace.messages = messages
                return result, final_text

        # Hit max iterations
        logger.warning("%s: reached max iterations (%d)", self.name, self.max_iterations)
        final_text = "\n".join(text_parts) if text_parts else "Reached maximum iterations."
        trace.exit_reason = "max_iterations"
        trace.messages = messages
        return self._parse_final_answer(final_text), final_text

    # ------------------------------------------------------------------
    # Context compression
    # ------------------------------------------------------------------

    def _compress_messages(self, messages: list[dict], keep_recent: int) -> list[dict]:
        """Compress older tool-result messages to prevent context overflow.

        Keeps the first message (user prompt) and the last `keep_recent`
        iteration pairs intact. Middle messages get their tool results
        summarised to a single line each.

        Each iteration contributes 2 messages: assistant (with tool_use blocks)
        and user (with tool_result blocks). So we keep messages[0] (the initial
        user prompt) + the last keep_recent*2 messages, and compress everything
        in between.
        """
        # messages[0] = initial user prompt
        # messages[1:] = pairs of (assistant, user-tool-results)
        total_pairs = (len(messages) - 1) // 2
        if total_pairs <= keep_recent:
            return messages  # nothing to compress

        # How many pairs to compress (from the start of the iteration messages)
        compress_count = total_pairs - keep_recent

        compressed = [messages[0]]  # keep the initial prompt

        for pair_idx in range(total_pairs):
            assistant_idx = 1 + pair_idx * 2
            user_idx = assistant_idx + 1
            if user_idx >= len(messages):
                compressed.append(messages[assistant_idx])
                break

            if pair_idx < compress_count:
                # Compress this pair: keep assistant tool call names, summarise results
                assistant_msg = messages[assistant_idx]
                user_msg = messages[user_idx]

                # Extract tool names from assistant content
                tool_names = []
                a_content = assistant_msg.get("content", [])
                if isinstance(a_content, list):
                    for block in a_content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_names.append(block.get("name", "?"))

                # Summarise tool results — keep more detail for large-context models
                result_summaries = []
                u_content = user_msg.get("content", [])
                # Truncation budget scales with the model's context window
                # (see __init__). Large-context models keep evidence near-verbatim.
                default_max_chars = getattr(self, "_result_char_budget", max(200, 80 * keep_recent))
                evidence_max_chars = max(2000, default_max_chars * 2)
                _evidence_patterns = ("HTTP/", "200 ", "403 ", "Set-Cookie", "<script", "SQL syntax")
                if isinstance(u_content, list):
                    for block in u_content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            content_str = str(block.get("content", ""))
                            is_err = block.get("is_error", False)
                            # Preserve HTTP/injection evidence with higher char limit
                            has_evidence = any(p in content_str for p in _evidence_patterns)
                            max_result_chars = evidence_max_chars if has_evidence else default_max_chars
                            if is_err:
                                result_summaries.append("error")
                            elif len(content_str) > max_result_chars:
                                result_summaries.append(content_str[:max_result_chars - 3] + "...")
                            else:
                                result_summaries.append(content_str or "ok")

                summary = (
                    f"[Earlier call: {', '.join(tool_names)}] → "
                    f"[{'; '.join(result_summaries)}]"
                )
                # Replace with a single text exchange
                prior_reasoning = assistant_msg.get("reasoning_content", "")
                rationale = ("\n[Prior model reasoning, truncated; not verified evidence]\n" + prior_reasoning[:2000]) if prior_reasoning else ""
                compressed.append({"role": "assistant", "content": f"Called {', '.join(tool_names)}" + rationale})
                compressed.append({"role": "user", "content": summary})
            else:
                # Keep recent pairs intact
                compressed.append(messages[assistant_idx])
                compressed.append(messages[user_idx])

        return compressed

    async def _compress_messages_llm(self, messages: list[dict], keep_recent: int) -> list[dict]:
        """LLM-powered compression: summarize old tool interactions into a concise narrative."""
        total_pairs = (len(messages) - 1) // 2
        if total_pairs <= keep_recent:
            return messages

        compress_count = total_pairs - keep_recent

        # Collect the pairs to compress into a text block
        pairs_text = []
        for pair_idx in range(compress_count):
            assistant_idx = 1 + pair_idx * 2
            user_idx = assistant_idx + 1
            if user_idx >= len(messages):
                break

            # Extract tool names and results
            tool_names = []
            a_content = messages[assistant_idx].get("content", [])
            if isinstance(a_content, list):
                for block in a_content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_names.append(block.get("name", "?"))
                        inp = block.get("input", {})
                        if isinstance(inp, dict):
                            tool_names[-1] += f"({', '.join(f'{k}={str(v)[:50]}' for k, v in list(inp.items())[:3])})"

            results = []
            u_content = messages[user_idx].get("content", [])
            if isinstance(u_content, list):
                for block in u_content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        content_str = str(block.get("content", ""))[:300]
                        is_err = block.get("is_error", False)
                        results.append(f"{'ERROR: ' if is_err else ''}{content_str}")

            reasoning = messages[assistant_idx].get("reasoning_content", "")
            pairs_text.append(f"Prior model reasoning (not verified evidence): {reasoning}\nTools: {', '.join(tool_names)}\nResults: {' | '.join(results)}")

        if not pairs_text:
            return messages

        word_limit = max(500, 100 * keep_recent)  # scale with context budget
        prompt = (
            "Summarize these pentesting tool calls and results into a detailed bullet-point summary. "
            "Focus on: what was discovered, what vulnerabilities were found or ruled out, "
            "what targets were tested, exact URLs/endpoints tested, HTTP responses received, "
            "and any errors. Preserve decision rationale, uncertainties, intended next steps and stopping conditions; do not treat reasoning as evidence. Be specific — include status codes, header values, and payload snippets. "
            "Preserve full HTTP request/response pairs that contain vulnerability evidence. "
            f"Keep it under {word_limit} words.\n\n"
            + "\n---\n".join(pairs_text)
        )

        try:
            llm = self.compress_llm or self.llm
            summary = await llm.analyze(prompt)
            if self.cost_tracker is not None:
                self.cost_tracker.add_cost("llm_api", llm.estimate_cost(len(prompt) // 4, len(summary) // 4),
                                           detail=f"{self.name} context_compression estimated_tokens")
            if not summary or len(summary) < 20:
                return self._compress_messages(messages, keep_recent)
        except Exception:
            logger.debug("%s: LLM compression failed, falling back to truncation", self.name)
            return self._compress_messages(messages, keep_recent)

        # Build compressed message list: system prompt + LLM summary + recent pairs
        compressed = [messages[0]]  # keep initial prompt
        compressed.append({"role": "assistant", "content": "Here's what I've done so far."})
        compressed.append({"role": "user", "content": f"[SUMMARY OF PRIOR WORK]\n{summary}"})

        # Append recent pairs intact
        for pair_idx in range(compress_count, total_pairs):
            assistant_idx = 1 + pair_idx * 2
            user_idx = assistant_idx + 1
            if assistant_idx < len(messages):
                compressed.append(messages[assistant_idx])
            if user_idx < len(messages):
                compressed.append(messages[user_idx])

        logger.info(
            "%s: LLM-compressed %d pairs into summary (%d chars), kept %d recent",
            self.name, compress_count, len(summary), keep_recent,
        )
        return compressed

    # ------------------------------------------------------------------
    # Scope and claim enforcement
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_domain(value: str) -> str | None:
        """Extract a domain from a target (plain domain) or url (full URL).

        Returns the bare hostname (no port, no path), or None if unparseable.
        """
        if not value or not isinstance(value, str):
            return None
        value = value.strip()
        if "://" in value:
            from urllib.parse import urlparse
            try:
                parsed = urlparse(value)
                host = parsed.hostname
                return host.lower() if host else None
            except Exception:
                return None
        from urllib.parse import urlsplit
        try:
            host = urlsplit("//" + value).hostname
            return host.lower() if host and not any(c.isspace() for c in host) else None
        except ValueError:
            return None

    # Common public TLDs — a BARE dotted token (no scheme) is only treated as a
    # host if its last segment is one of these. This filters code tokens like
    # `r.text` / `requests.get` and filenames like `main.chunk.js` while keeping
    # real hosts including odd TLDs used by tooling (crt.sh on .sh, .io, .dev).
    _COMMON_TLDS = frozenset({
        "com", "org", "net", "io", "co", "gov", "edu", "mil", "int", "info", "biz",
        "dev", "app", "ai", "xyz", "me", "tv", "cc", "us", "uk", "ca", "au", "de",
        "fr", "nl", "eu", "in", "cn", "jp", "br", "ru", "es", "it", "se", "ch", "sh",
        "sg", "hk", "kr", "za", "mx", "pl", "tr", "id", "ir", "nz", "ae", "sa", "il",
        "cloud", "tech", "online", "site", "store", "network", "digital",
    })

    @staticmethod
    def _is_local_host(h: str) -> bool:
        return (
            h == "localhost"
            or h.startswith(("127.", "10.", "192.168.", "0.", "169.254."))
            or h.endswith((".local", ".internal", ".localhost", ".svc", ".cluster.local"))
        )

    @classmethod
    def _hosts_in_text(cls, text: str) -> set[str]:
        """Extract recognizable hostnames from a command / code / URL string.

        URL hosts (`https?://host/…`) are taken as-is — a host after a scheme IS a
        host, whatever its TLD. Bare `sub.domain.tld` tokens (nmap/dig/curl style)
        are only accepted when the last segment is a known TLD, so code tokens
        (`r.text`, `requests.get`) and filenames (`main.chunk.js`) don't false-match.
        Local/private URL hosts are included and must also be explicitly scoped.
        """
        import re
        if not text:
            return set()
        hosts: set[str] = set()
        for m in re.finditer(r"https?://[^\s\"'<>`]+", text):
            host = cls._extract_domain(m.group(0))
            if host:
                hosts.add(host)
        for m in re.finditer(r'\b((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})\b', text):
            h = m.group(1).strip(".").lower()
            last = h.rsplit(".", 1)[-1]
            if last in cls._COMMON_TLDS or cls._is_local_host(h):
                hosts.add(h)
        return hosts

    def _record_endpoint_activity(self, tool_name: str, tool_input: dict, tc, result_text: str) -> None:
        """Update durable dead-end memory from a completed call.

        Counts the actual network HOST hit (from target/url or a command/code body)
        and flags it rate-limited if the result shows a 429/ban. Surfaced in the
        per-turn digest so the agent stops re-running dead calls after compression.
        """
        # Key by the FULL URL actually contacted (host+path+query), so the SAME
        # request repeated (e.g. crt.sh/?q=<target> 7×) is a dead end, while varied
        # endpoints on the real target (/session, /login, /user?id=1..2) each count
        # once and are never mislabeled. Query-string domains are part of the key,
        # not separate hosts, so the target is never flagged as a dead end.
        import re
        url_re = re.compile(r'https?://([A-Za-z0-9.\-]+(?:/[^\s"\'`)\]]*)?)')
        urls: set[str] = set()
        for param in ("target", "url"):
            v = tool_input.get(param)
            if not v:
                continue
            m = url_re.search(str(v))
            if m:
                urls.add(m.group(1).lower().rstrip("/"))
            else:
                d = self._extract_domain(str(v))
                if d:
                    urls.add(d)
        for param in ("command", "cmd", "code", "script", "args"):
            v = tool_input.get(param)
            if v is None:
                continue
            text = " ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
            for m in url_re.finditer(text):
                key = m.group(1).lower().rstrip("/")
                if not self._is_local_host(key.split("/")[0]):
                    urls.add(key)
        if not urls:
            return
        for u in urls:
            self._endpoint_hits[u] = self._endpoint_hits.get(u, 0) + 1
        blob = ((result_text or "")[:2000] + " " + str(getattr(tc, "error", "") or "")).lower()
        if any(s in blob for s in ("429", "too many request", "rate limit", "rate-limit",
                                   "ratelimited", "retry-after", "slow down")):
            self._rate_limited |= urls

    def _check_scope_claim(self, tool_name: str, tool_input: dict) -> str | None:
        """Check scope and claim enforcement for a tool call.

        Returns an error string if blocked, None if allowed. Inspects target/url AND
        command/code/args for recognizable hosts. Arbitrary code and redirects
        still require external network controls for strict egress enforcement.
        """
        # Check every explicit destination field, not just the first URL.
        domain = None
        direct_hosts: set[str] = set()
        for param in ("target", "url", "domain", "host", "hostname"):
            val = tool_input.get(param)
            if val:
                host = self._extract_domain(str(val))
                if host:
                    direct_hosts.add(host)
                    domain = domain or host

        # --- Scope check: gather every host this call would touch, incl. those
        #     buried in a docker command / code body / arg list. ---
        if self._allowed_domains is not None:
            hosts: set[str] = set(direct_hosts)
            for param in ("command", "cmd", "code", "script", "args", "data", "payload"):
                val = tool_input.get(param)
                if val is None:
                    continue
                text = " ".join(str(x) for x in val) if isinstance(val, (list, tuple)) else str(val)
                hosts |= self._hosts_in_text(text)

            from utils.scope import host_in_scope

            def _in_scope(h: str) -> bool:
                return host_in_scope(h, self._allowed_domains)

            if hosts:
                out_of_scope = {h for h in hosts if not _in_scope(h)}
                # One allowed hostname must never authorize another destination.
                # This is a conservative text check, not an egress firewall.
                if out_of_scope:
                    sample = ", ".join(sorted(self._allowed_domains)[:8])
                    return (
                        f"BLOCKED: {', '.join(sorted(out_of_scope)[:5])} is OUT OF SCOPE. Do NOT reach out-of-scope hosts (including "
                        f"via docker or code). In-scope: {sample}. Test an in-scope target instead."
                    )

        if not domain:
            return None  # No primary target/url to run the claim check against

        # Reporting / coordination tools are NEVER claim-gated. A claim is an
        # exclusive lock to stop two agents actively *testing* the same target
        # at once — it must not stop an agent from *recording* what it found or
        # reading/posting shared state. Otherwise, with a single target only the
        # one claim-holder could ever submit, and every other agent's findings
        # are silently dropped.
        _CLAIM_EXEMPT_TOOLS = {
            "submit_finding", "verify_finding", "post_discovery",
            "store_graph_finding", "read_discoveries", "read_findings",
            "get_past_findings", "check_claimed", "read_session_memory",
            "write_session_memory", "claim_target", "release_claim",
        }
        if tool_name in _CLAIM_EXEMPT_TOOLS:
            return None

        # Coordination policy's claim_mode governs ONLY whether a target can
        # be exclusively locked — it has no bearing on the scope check above,
        # which already ran and already blocks any out-of-scope host
        # regardless of this setting. "shared" means claims are investigation
        # intent, not a lock: multiple agents may work the same in-scope
        # target (useful when a mechanism already rewards not colliding).
        if (self._shared_memory is not None
                and self._shared_memory.coordination.claim_mode == "shared"):
            return None

        # --- Claim check (only in swarm mode with SharedMemory) ---
        if self._shared_memory is not None:
            # Check if this agent (or a parent domain) is claimed by us
            agent_name = self.name
            # Check exact domain and walk up parent domains
            parts = domain.split(".")
            claimed_by_us = False
            for i in range(max(1, len(parts) - 1)):
                check_domain = ".".join(parts[i:])
                owner = self._shared_memory.is_claimed(f"scan:{check_domain}")
                if owner == agent_name:
                    claimed_by_us = True
                    break
                if owner is not None and owner != agent_name:
                    return (
                        f"BLOCKED: {domain} is claimed by {owner}. "
                        f"Pick a different target."
                    )

            if not claimed_by_us:
                return (
                    f"BLOCKED: You haven't claimed {domain}. "
                    f"Call claim_target with key='scan:{domain}' first."
                )

        return None

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> ToolCall:
        """Execute a single tool call with timing and error handling."""
        tc = ToolCall(tool_name=tool_name, input=tool_input)
        start = time.monotonic()

        tool = self.tools.get(tool_name)
        if tool is None:
            # CLI mode fallback: model may call "claim_target --key X" as tool
            # name instead of "run" with command.  Try to parse it.
            if self.cli_mode and self._cli_adapter:
                # Clean XML fragments that models sometimes inject
                cleaned = tool_name.replace("</parameter", "").replace("</function", "").strip()
                # Build a command string from the tool name + any input values
                cmd = cleaned
                if tool_input:
                    # Append input values as extra args if they look like params
                    for k, v in tool_input.items():
                        if k not in ("command",):
                            cmd += f" --{k} {v}"
                try:
                    real_name, real_params = self._cli_adapter.parse(cmd)
                    logger.info("%s: CLI fallback parsed '%s' → %s(%s)", self.name, cleaned, real_name, real_params)
                    tool_name = real_name
                    tool_input = real_params
                    tc.tool_name = real_name
                    tool = self.tools.get(real_name)
                except Exception:
                    pass
            if tool is None:
                tc.error = f"Unknown tool: {tool_name}"
                tc.duration_ms = (time.monotonic() - start) * 1000
                logger.warning("%s: unknown tool '%s'", self.name, tool_name)
                return tc

        # Normalize parameters: remap unknown keys to schema-expected keys
        if tool.category != "mcp":
            tool_input = self._normalize_params(tool_input, tool.parameters)

        # Scope and claim enforcement. Applies to any tool that can reach the
        # network — scan/recon plus code/docker execution (which can embed an
        # out-of-scope host in its command/code and previously bypassed this).
        _net_capable = tool.category in ("scan", "recon") or any(
            k in tool_input for k in ("command", "cmd", "code", "script", "args", "url", "target", "domain", "host", "hostname")
        )
        if self._allowed_domains is not None and _net_capable:
            violation = self._check_scope_claim(tool_name, tool_input)
            if violation:
                tc.error = violation
                tc.duration_ms = (time.monotonic() - start) * 1000
                if "OUT OF SCOPE" in violation:
                    self._scope_violations += 1
                logger.warning("%s: scope/claim violation on %s: %s", self.name, tool_name, violation)
                return tc

        try:
            tc.output = await tool.handler(tool_input)
        except Exception as exc:
            tc.error = str(exc)
            logger.error("%s: tool '%s' raised: %s", self.name, tool_name, exc)
        finally:
            tc.duration_ms = (time.monotonic() - start) * 1000

        logger.info(
            "%s: tool %s completed in %.0fms%s",
            self.name,
            tool_name,
            tc.duration_ms,
            f" (error: {tc.error})" if tc.error else "",
        )

        if self.on_tool_call is not None:
            try:
                self.on_tool_call(tc)
            except Exception:
                logger.exception("on_tool_call hook error")

        return tc

    # ------------------------------------------------------------------
    # Parameter normalization
    # ------------------------------------------------------------------

    # Common aliases LLMs use interchangeably
    _PARAM_SYNONYMS: dict[str, set[str]] = {
        "target": {"host", "domain", "url", "address", "server"},
        "host": {"target", "domain", "server", "hostname", "address"},
        "domain": {"target", "host", "hostname"},
        "url": {"target", "endpoint", "uri", "link"},
        "record_type": {"type", "rtype", "query_type"},
        "flags": {"options", "args", "arguments", "params"},
    }

    @staticmethod
    def _normalize_params(tool_input: dict, schema: dict) -> dict:
        """Remap LLM-provided params to match the tool's JSON Schema.

        If the LLM sends a param name that isn't in the schema but is a
        known synonym of a schema param, remap it. This fixes the common
        issue where models send 'domain' instead of 'target' etc.
        """
        expected = set(schema.get("properties", {}).keys())
        if not expected:
            return tool_input

        provided = set(tool_input.keys())
        # If all provided keys are expected, no remapping needed
        if provided <= expected:
            return tool_input

        unknown = provided - expected
        missing = expected - provided

        if not unknown or not missing:
            return tool_input

        remapped = dict(tool_input)
        for unk_key in unknown:
            # Find a missing expected key that's a synonym
            for exp_key in missing:
                synonyms = ToolAgent._PARAM_SYNONYMS.get(exp_key, set())
                if unk_key in synonyms:
                    remapped[exp_key] = remapped.pop(unk_key)
                    missing.discard(exp_key)
                    logger.debug("Remapped param '%s' → '%s'", unk_key, exp_key)
                    break

        return remapped

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------

    @staticmethod
    def _call_fingerprint(tool_name: str, tool_input: dict) -> str:
        """Create a hashable fingerprint for a tool call."""
        try:
            sorted_items = tuple(sorted(
                (k, str(v)) for k, v in tool_input.items()
            ))
        except Exception:
            sorted_items = (str(tool_input),)
        return f"{tool_name}::{sorted_items}"

    # Tools that must NEVER be served from cache: they mutate state, submit data,
    # or ARE the test when repeated (race/rate-limit/timing/idempotency probing).
    _NEVER_CACHE = {
        "submit_finding", "claim_target", "release_target", "post_discovery",
        "write_session_memory", "verify_finding", "request_human_help",
        "execute_code", "execute_python", "run_docker_tool", "curl_request",
        "fuzz_parameter", "test_ssrf", "test_open_redirect",
        "test_header_injection", "test_cors_origin",
        "browser_navigate", "browser_get_text", "browser_screenshot",
        "browser_click", "browser_type", "browser_links",
    }

    def _is_cacheable(self, tool_name: str, tool_input: dict) -> bool:
        """Whether an identical repeat of this call can be served from the TTL cache.

        Only idempotent READ tools — re-running them with identical args yields the
        same data, so a repeat within the TTL is free. State-changing / testing /
        timing-sensitive tools are excluded (a repeated identical request can be the
        actual exploit: race, rate-limit, timing, idempotency).
        """
        if self.tool_cache_ttl <= 0 or tool_name in self._NEVER_CACHE:
            return False
        # HTTP fetchers: cache GETs only (a POST may intentionally repeat).
        if tool_name in {"http_fetch", "http_get", "fetch_url"}:
            return str((tool_input or {}).get("method", "GET")).upper() == "GET"
        if tool_name in {"web_search", "regex_scan_url", "get_similar_targets",
                         "get_effective_techniques", "get_past_findings",
                         "query_knowledge_graph"}:
            return True
        return tool_name.startswith(("extract_", "read_", "list_", "check_", "get_"))

    def _check_guardrails(self, iteration: int, consecutive_errors: int) -> str | None:
        """Return an error message if a guardrail is tripped, else None.

        Hard stops (return message → agent stops):
        1. Consecutive tool errors — abort if too many in a row
        2. Cost budget exceeded — the primary limit
        3. Total tool call budget — safety net

        Soft nudge (return None but inject hint into messages):
        4. Stale loop — nudge once if recent calls all fruitless
        """
        # 1. Consecutive errors
        if consecutive_errors >= self.max_tool_errors:
            return f"Aborted: {consecutive_errors} consecutive tool errors"

        # 2. Cost-based budget (primary guardrail)
        if self.cost_tracker:
            from config import get_settings
            budget = getattr(self, "session_budget", None) or get_settings().session_budget
            spent = self.cost_tracker.get_total()
            if spent > budget:
                return (
                    f"Budget exhausted: ${spent:.2f} spent of ${budget:.2f} budget. "
                    "Summarize your findings and conclude."
                )

        # 3. Total tool call budget (safety net)
        total_calls = len(self._seen_calls)
        if total_calls >= self.max_total_tool_calls:
            return (
                f"Aborted: reached tool call budget ({self.max_total_tool_calls}). "
                "Summarize your findings and conclude."
            )

        # 4. Stale loop detection — hard abort after extended fruitlessness
        window = self._recent_outcomes[-self.stale_window:]
        if len(window) >= self.stale_window and not any(window):
            if self._stale_nudged:
                # Already nudged once, now abort
                return (
                    f"Aborted: last {self.stale_window * 2} calls produced no useful results. "
                    "The agent is stuck in a loop."
                )
            self._stale_nudged = True
            logger.warning(
                "%s: last %d tool calls produced no useful results — nudging",
                self.name, self.stale_window,
            )

        return None

    # ------------------------------------------------------------------
    # Per-category tool budgets
    # ------------------------------------------------------------------

    # Tool category classification — maps tool names to budget categories.
    # Categories with None budget are unlimited. Missing tools default to unlimited.
    _TOOL_CATEGORIES: dict[str, str] = {}  # populated by _classify_tool

    @staticmethod
    def _classify_tool(tool_name: str) -> str:
        """Classify a tool into a budget category.

        Categories:
        - recon: enumeration, fingerprinting, link/form extraction
        - docker_heavy: slow Docker tools (gau, katana, nmap, nikto, nuclei, ffuf, sqlmap)
        - scanner: built-in vuln scanners (fast)
        - attack: active testing (fuzz, SSRF, CORS, redirect, etc.)
        - memory: shared memory, graph, session, discovery tools
        - code: code execution, browser, custom scripts
        """
        _RECON = {
            "extract_links", "extract_forms", "extract_html_comments",
            "extract_headers", "extract_js_urls", "fingerprint_tech",
            "scan_tech_fingerprint", "check_domain", "dns_resolve",
            "crtsh_lookup", "dns_bruteforce", "enumerate_subdomains",
            "check_ports", "whois_lookup", "dig_lookup", "ssl_check",
            "http_fetch", "http_probe",
        }
        _DOCKER_HEAVY = {
            "run_docker_tool",  # gau, katana, nmap, ffuf, sqlmap etc.
        }
        _SCANNER = {
            "run_nuclei", "run_ffuf", "run_sqlmap", "run_nikto",
            "run_katana", "scan_cors", "scan_info_disclosure",
            "scan_js_secrets", "scan_open_redirect", "scan_security_headers",
            "scan_subdomain_takeover", "scan_sensitive_paths",
        }
        _ATTACK = {
            "fuzz_parameter",
            "test_ssrf", "test_header_injection", "regex_scan_url",
            "check_sensitive_path", "test_aws_credentials", "curl_request",
        }
        _MEMORY = {
            "read_discoveries", "post_discovery", "claim_target",
            "submit_finding", "release_target", "read_session_memory",
            "write_session_memory", "get_similar_targets",
            "get_effective_techniques", "query_knowledge_graph",
            "store_graph_finding", "get_past_findings", "get_target_history",
            "get_best_vuln_types", "check_scan_history",
        }
        _CODE = {
            "python_script", "run_python", "code_execution",
            "browser_navigate", "browser_click", "browser_type",
            "browser_screenshot", "browser_get_text", "browser_get_links",
            "browser_evaluate", "browser_back",
        }

        if tool_name in _RECON:
            return "recon"
        if tool_name in _DOCKER_HEAVY:
            return "docker"
        if tool_name in _SCANNER:
            return "scanner"
        if tool_name in _ATTACK:
            return "attack"
        if tool_name in _MEMORY:
            return "memory"
        if tool_name in _CODE:
            return "code"
        return "other"

    def _check_tool_budget(self, tool_name: str) -> str | None:
        """Check if a tool call is within its category budget.

        Returns an error message if budget is exhausted, else None.
        Increments the usage counter for the category.
        """
        if not self.tool_budgets:
            return None

        category = self._classify_tool(tool_name)
        budget = self.tool_budgets.get(category)

        if budget is None:
            # Unlimited category
            self._tool_category_usage[category] = self._tool_category_usage.get(category, 0) + 1
            return None

        used = self._tool_category_usage.get(category, 0)
        if used >= budget:
            remaining = {
                cat: f"{bud - self._tool_category_usage.get(cat, 0)}/{bud}"
                for cat, bud in self.tool_budgets.items()
                if bud is not None
            }
            return (
                f"BUDGET EXCEEDED: You've used all {budget} allowed '{category}' tool calls. "
                f"{tool_name} is a '{category}' tool. Remaining budgets: {remaining}. "
                f"Switch to unlimited tools (code execution, browser, shared memory) "
                f"or use a different category. Write a Python script if you need "
                f"to do more {category} work."
            )

        self._tool_category_usage[category] = used + 1

        # Warn at 80% usage
        if budget > 3 and (used + 1) >= budget * 0.8:
            logger.info(
                "%s: tool budget '%s' at %d/%d (%.0f%%)",
                self.name, category, used + 1, budget, (used + 1) / budget * 100,
            )

        return None

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_progress_digest(self, iteration: int) -> str:
        """Build a compact self-orientation block from shared memory.

        Re-injected every turn so the agent always sees what IT has claimed,
        discovered, and submitted — this survives context compression and keeps
        the agent anchored to its own investigation instead of target-hopping.
        Returns "" if there's no shared memory (non-swarm agents).
        """
        sm = getattr(self, "_shared_memory", None)
        if sm is None:
            return ""
        agent_id = getattr(self, "agent_id", self.name)
        lines = [f"[YOUR STATE] iteration {iteration}/{self.max_iterations}"]

        try:
            lines.append(f"Score: {sm.get_score(agent_id).total}")
        except Exception:
            pass

        try:
            lines.extend(sm.turn_lines(agent_id))
        except Exception:
            pass

        try:
            my_claims = [k for k, a in sm.claims.items() if a == agent_id]
        except Exception:
            my_claims = []
        if my_claims:
            lines.append("Claimed by you: " + ", ".join(my_claims))

        try:
            my_disc = [
                e for e in sm.read_all()
                if e.agent_id == agent_id and e.entry_type != "finding"
            ]
        except Exception:
            my_disc = []
        try:
            my_findings = [
                f for f, rep in sm.get_findings_with_reporters() if rep == agent_id
            ]
        except Exception:
            my_findings = []

        if my_disc:
            lines.append("Your discoveries so far:")
            for e in my_disc[-6:]:
                snippet = (e.content or "")[:120].replace("\n", " ")
                lines.append(f"  - [{e.category}] {e.key}: {snippet}")
        if my_findings:
            lines.append(f"Your submitted findings: {len(my_findings)}")
            for f in my_findings[-4:]:
                lines.append(
                    f"  - {getattr(f, 'vuln_type', '?')} on "
                    f"{getattr(f, 'target', '?')} ({getattr(f, 'severity', '?')})"
                )

        # Durable dead-end memory: the same request repeated (esp. rate-limited).
        # Re-shown every turn so you don't re-derive the same dead call after your
        # context is compressed. Rate-limited = hard STOP; high-count = soft reuse.
        rate_limited = sorted(
            ((u, n) for u, n in self._endpoint_hits.items() if u in self._rate_limited),
            key=lambda x: -x[1],
        )
        heavy = sorted(
            ((u, n) for u, n in self._endpoint_hits.items()
             if n >= 5 and u not in self._rate_limited),
            key=lambda x: -x[1],
        )
        if rate_limited or heavy:
            lines.append("DEAD ENDS — you already ran these; do NOT repeat them:")
            for u, n in rate_limited[:6]:
                lines.append(f"  - {u[:75]} — hit {n}× ⛔ RATE-LIMITED/BANNED. STOP; use the data you already have.")
            for u, n in heavy[:4]:
                lines.append(f"  - {u[:75]} — hit {n}×. Reuse the earlier result unless you changed something.")

        # Scope-violation counter: a BLOCKED out-of-scope call often bundles a
        # DIFFERENT set of host names each time, so no single host's hit-count
        # above ever catches the pattern. Count the mistake itself and escalate
        # independent of which specific hosts were named.
        if self._scope_violations >= 8:
            lines.append(
                f"⚠️ ALERT — YOU ARE STUCK IN A LOOP: {self._scope_violations} blocked "
                "out-of-scope calls so far, burning budget on hosts that are NOT in scope. "
                "Do not repeat this pattern again. Before your NEXT call: pick ONE host, "
                "check it against the in-scope list above yourself (suffix match only — "
                "'*.example.com' covers 'x.example.com' but never bare 'example.com'), and "
                "only then call a tool on it."
            )
        elif self._scope_violations >= 3:
            lines.append(
                f"SCOPE VIOLATIONS: {self._scope_violations} blocked out-of-scope calls so "
                "far — check each host against the in-scope list before calling, not after "
                "being blocked."
            )

        # Orientation nudge based on where this agent actually is.
        if my_disc and not my_findings:
            lines.append(
                f"You have {len(my_disc)} logged discoveries but have called "
                "submit_finding 0 times. A post_discovery is only a note — it pays "
                "NOTHING. A finding pays only when you call submit_finding with "
                "request/response evidence. Decide about your best lead NOW: if it "
                "meets the bar (a REAL victim's data or a crossed privilege boundary, "
                "proven with the actual request+response), call submit_finding. If it "
                "genuinely does not (the gate held, it's by-design, or it's self-only), "
                "record that conclusion. Pursue another lead only when evidence justifies it; "
                "otherwise conclude with findings and remaining uncertainty."
            )
        elif not my_claims and not my_disc:
            lines.append(
                "You have not claimed a target or logged any discovery yet. Claim ONE "
                "target and investigate it deeply."
            )

        return "\n".join(lines)

    def _findings_list(self) -> list[str]:
        """[severity] title for findings in shared memory (for the briefing)."""
        sm = getattr(self, "_shared_memory", None)
        if sm is None:
            return []
        out = []
        try:
            for f in sm.get_findings():
                title = getattr(f, "title", None) or getattr(f, "vuln_type", "?")
                out.append(f"[{getattr(f, 'severity', '?')}] {title}")
        except Exception:
            pass
        return out[-8:]

    async def _assemble_turn_context(self, iteration: int) -> str:
        """Rebuild the full per-turn briefing from live state (context_mode)."""
        from agents.turn_context import TurnState, build_turn_context, distill_state

        # Director directives become notes the distiller folds into leads/guidance.
        while not self._directive_queue.empty():
            try:
                self._raw_notes += f"\n[DIRECTOR]: {self._directive_queue.get_nowait()}"
            except asyncio.QueueEmpty:
                break

        # Re-distill established facts + ranked leads every N turns (cheap LLM).
        if self._raw_notes.strip() and iteration % self.distill_every == 0:
            llm = self.distill_llm or self.compress_llm or self.llm
            est, leads = await distill_state(llm, self.in_scope, self._raw_notes, self._findings_list(), cost_tracker=self.cost_tracker)
            if est or leads:
                self._distilled = (est, leads)
        established, leads = self._distilled

        dead = []
        for u, n in sorted(self._endpoint_hits.items(), key=lambda x: -x[1]):
            if u in self._rate_limited:
                dead.append(f"{u[:75]} — hit {n}× RATE-LIMITED, use the data you already have")
            elif n >= 5:
                dead.append(f"{u[:75]} — hit {n}×, reuse the earlier result unless something changed")

        scope = self.in_scope or (sorted(self._allowed_domains) if self._allowed_domains else [])
        state = TurnState(
            in_scope=scope,
            out_of_scope=self.out_of_scope,
            budget_spent=self.cost_tracker.get_total() if self.cost_tracker else 0.0,
            budget_total=self.session_budget or 0.0,
            dead_ends=dead[:8],
            established_facts=established,
            findings=self._findings_list(),
            ranked_leads=leads,
            recent_results=self._last_results_text,
        )
        methodology = getattr(self, "methodology", None)
        custom_template = getattr(methodology, "config", {}).get("turn_template") if methodology else None
        briefing = build_turn_context(state, template=custom_template)
        # Both context modes must dispatch turn hooks exactly once per turn.
        digest = self._build_progress_digest(iteration)
        parts = [self.system_prompt, briefing, digest]
        return "\n\n".join(part for part in parts if part)

    def _build_initial_messages(self, assignment: TaskAssignment) -> list[dict]:
        return [{"role": "user", "content": self._build_user_message(assignment)}]

    def _build_user_message(self, assignment: TaskAssignment) -> str:
        """Build the initial user message from a task assignment."""
        parts = []
        if self.system_prompt:
            parts.append(self.system_prompt)
        if self.cli_mode and self._cli_adapter:
            parts.append("\n" + self._cli_adapter.help_text())
        parts.append(f"\n## Task: {assignment.task_type}")
        if assignment.context:
            parts.append(f"Context: {assignment.context}")
        if assignment.parameters:
            parts.append(f"Parameters: {json.dumps(assignment.parameters, default=str)}")
        if self.cli_mode:
            parts.append(
                "\nUse the `run` tool to execute commands. "
                "Format: run(command=\"command_name --arg1 value1 --arg2 value2\"). "
                "When you are done, respond with your final findings as JSON."
            )
        else:
            parts.append(
                "\nUse the available tools to accomplish this task. "
                "When you are done, respond with your final findings as JSON."
            )
        return "\n".join(parts)

    def _parse_final_answer(self, text: str) -> Any:
        """Try to parse the final LLM text as JSON, fall back to raw text."""
        # Try to find a JSON block in the text
        stripped = text.strip()
        # Try ```json ... ``` blocks first
        if "```json" in stripped:
            start = stripped.index("```json") + 7
            end = stripped.index("```", start)
            json_str = stripped[start:end].strip()
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass
        # Try raw JSON
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            if start_char in stripped:
                start = stripped.index(start_char)
                # Find matching closing brace from the end
                rindex = stripped.rfind(end_char)
                if rindex > start:
                    try:
                        return json.loads(stripped[start : rindex + 1])
                    except json.JSONDecodeError:
                        pass
        return text

    # ------------------------------------------------------------------
    # Deterministic fallback
    # ------------------------------------------------------------------

    async def run(self, assignment: TaskAssignment) -> Any:
        """Default deterministic implementation — subclasses should override."""
        return {"note": f"{self.name} has no deterministic fallback", "assignment": assignment.context}
