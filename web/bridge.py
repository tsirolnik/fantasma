"""Pipeline ↔ WebSocket bridge.

Subscribes to the MessageBus and ToolAgent hooks, broadcasts events to
connected WebSocket clients, and relays client commands back into the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp.web import WebSocketResponse
    from agents.pipeline import AgentPipeline
    from agents.tool_agent import ToolCall

logger = logging.getLogger(__name__)


def _mechanism_names(pipeline) -> list[str]:
    """Display names for whatever mechanisms the pipeline is configured with
    — the richer pipeline.mechanisms (list[str | dict], possibly with tuned
    knobs or a custom 'class') if set, else the simple settings.mechanisms
    comma-string fallback for direct-construction callers."""
    specs = getattr(pipeline, "mechanisms", None)
    if not specs:
        raw = getattr(pipeline.settings, "mechanisms", None)
        return raw.split(",") if raw else []
    return [s if isinstance(s, str) else (s.get("name") or s.get("class") or "?") for s in specs]


class PipelineBridge:
    """Glue between the AgentPipeline and WebSocket clients."""

    def __init__(self, pipeline: AgentPipeline) -> None:
        self.pipeline = pipeline
        self.clients: list[WebSocketResponse] = []
        self._pending_checkpoints: dict[str, asyncio.Future[str]] = {}
        self._pending_help: dict[str, asyncio.Future[str]] = {}
        self._status_task: asyncio.Task | None = None
        self._current_phase: str = "idle"
        self._current_program: str = ""
        self._pipeline_state: str = "running"  # running | paused | stopped
        self._llm_exchanges: list[dict] = []  # rolling buffer for LLM wire tab
        self._max_exchanges: int = 200
        self._browser_subs: dict[WebSocketResponse, str] = {}  # ws -> agent_id
        self._browser_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Hook into the pipeline's MessageBus and start status loop."""
        self.pipeline.bus.subscribe("*", self._on_bus_message)
        self._status_task = asyncio.ensure_future(self._status_loop())
        self._browser_task = asyncio.ensure_future(self._browser_capture_loop())

    async def stop(self) -> None:
        tasks = []
        if self._status_task and not self._status_task.done():
            self._status_task.cancel()
            tasks.append(self._status_task)
        if self._browser_task and not self._browser_task.done():
            self._browser_task.cancel()
            tasks.append(self._browser_task)
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def add_client(self, ws: WebSocketResponse) -> None:
        self.clients.append(ws)

    def remove_client(self, ws: WebSocketResponse) -> None:
        if ws in self.clients:
            self.clients.remove(ws)
        self._browser_subs.pop(ws, None)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    def _broadcast(self, payload: dict) -> None:
        """Send a JSON payload to all connected clients (fire-and-forget)."""
        if not self.clients:
            return
        text = json.dumps(payload, default=str)
        stale: list[WebSocketResponse] = []
        for ws in self.clients:
            if ws.closed:
                stale.append(ws)
                continue
            try:
                asyncio.ensure_future(ws.send_str(text))
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.remove_client(ws)

    # ------------------------------------------------------------------
    # MessageBus subscriber
    # ------------------------------------------------------------------

    def _on_bus_message(self, message) -> None:
        """Called for every AgentMessage on the bus."""
        self._broadcast({
            "type": "agent_message",
            "id": message.id,
            "sender": message.sender,
            "recipient": message.recipient,
            "msg_type": message.msg_type,
            "content": message.content,
            "priority": message.priority,
            "timestamp": message.timestamp.isoformat(),
            "data": message.data,
        })

    # ------------------------------------------------------------------
    # Tool-call hook (set on each ToolAgent)
    # ------------------------------------------------------------------

    def on_tool_start(self, agent_name: str, tool_name: str, tool_input: dict) -> None:
        """Callback invoked when a tool starts executing."""
        self._broadcast({
            "type": "tool_start",
            "agent": agent_name,
            "tool": tool_name,
        })

    def on_llm_start(self, agent_name: str, model: str, messages_count: int, tools_count: int) -> None:
        """Callback invoked when an LLM API call begins."""
        self._broadcast({
            "type": "llm_start",
            "agent": agent_name,
            "model": model,
            "messages": messages_count,
            "tools": tools_count,
        })

    def on_tool_call(self, agent_name: str, tc: ToolCall) -> None:
        """Callback invoked after a ToolAgent completes a tool call."""
        output_preview = ""
        if tc.output is not None:
            output_preview = str(tc.output)[:500]

        self._broadcast({
            "type": "tool_call",
            "agent": agent_name,
            "tool": tc.tool_name,
            "input": tc.input,
            "output_preview": output_preview,
            "duration_ms": tc.duration_ms,
            "error": tc.error,
        })

    def on_llm_call(self, agent_name: str, model: str, messages_count: int, tools_count: int, duration_ms: int, response_preview: str = "") -> None:
        """Callback invoked when an LLM API call completes."""
        self._broadcast({
            "type": "llm_call",
            "agent": agent_name,
            "model": model,
            "messages": messages_count,
            "tools": tools_count,
            "duration_ms": duration_ms,
            "response_preview": response_preview,
        })

    def on_llm_exchange(self, agent_name: str, model: str, messages: list[dict], response: dict, duration_ms: int) -> None:
        """Capture full LLM input/output for the wire debug tab."""
        import time as _time

        # Summarize messages for display (don't send full content over WS — too large)
        msg_summaries = []
        for m in messages:
            role = m.get("role", "?") if isinstance(m, dict) else getattr(m, "role", "?")
            content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
            if content is None:
                content = ""
            if isinstance(content, str):
                text = content[:2000] + ("..." if len(content) > 2000 else "")
                msg_summaries.append({"role": role, "text": text})
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            parts.append(block.get("text", "")[:1000])
                        elif btype == "tool_use":
                            parts.append(f"[tool_use: {block.get('name', '?')}({json.dumps(block.get('input', {}), default=str)[:200]})]")
                        elif btype == "tool_result":
                            result_text = str(block.get("content", ""))[:500]
                            parts.append(f"[tool_result: {result_text}]")
                        else:
                            parts.append(str(block)[:300])
                    elif isinstance(block, str):
                        parts.append(block[:1000])
                    else:
                        btype = getattr(block, "type", "")
                        if btype == "text":
                            parts.append(getattr(block, "text", "")[:1000])
                        elif btype == "tool_use":
                            parts.append(f"[tool_use: {getattr(block, 'name', '?')}({json.dumps(getattr(block, 'input', {}), default=str)[:200]})]")
                        elif btype == "tool_result":
                            parts.append(f"[tool_result: {str(getattr(block, 'content', ''))[:500]}]")
                        else:
                            parts.append(str(block)[:300])
                msg_summaries.append({"role": role, "text": "\n".join(parts)})
            else:
                # Fallback for unexpected types
                msg_summaries.append({"role": role, "text": str(content)[:2000]})

        # Summarize response — handle both dicts and SDK objects
        resp_parts = []
        tool_calls = []
        for block in response.get("content", []):
            if isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    resp_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    args = block.get("input", {})
                    tool_calls.append({
                        "name": block.get("name", "?"),
                        "args": json.dumps(args, default=str)[:500] if args else "{}",
                    })
            else:
                btype = getattr(block, "type", "")
                if btype == "text":
                    resp_parts.append(getattr(block, "text", ""))
                elif btype == "tool_use":
                    args = getattr(block, "input", {})
                    tool_calls.append({
                        "name": getattr(block, "name", "?"),
                        "args": json.dumps(args, default=str)[:500] if args else "{}",
                    })

        exchange = {
            "id": len(self._llm_exchanges),
            "agent": agent_name,
            "model": model,
            "timestamp": _time.time(),
            "duration_ms": duration_ms,
            "messages": msg_summaries,
            "thinking": response.get("thinking", ""),
            "response_text": "\n".join(resp_parts),
            "tool_calls": tool_calls,
            "usage": response.get("usage", {}),
            "stop_reason": response.get("stop_reason", ""),
        }

        self._llm_exchanges.append(exchange)
        if len(self._llm_exchanges) > self._max_exchanges:
            self._llm_exchanges = self._llm_exchanges[-self._max_exchanges:]
        logger.debug(
            "LLM exchange #%d: agent=%s model=%s msgs=%d thinking=%d response=%d tools=%d",
            exchange["id"], agent_name, model, len(msg_summaries),
            len(exchange["thinking"]), len(exchange["response_text"]), len(tool_calls),
        )

    def get_llm_exchanges(self, agent: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        """Return stored LLM exchanges for the wire tab, optionally filtered by agent."""
        exchanges = self._llm_exchanges
        if agent:
            exchanges = [e for e in exchanges if e["agent"] == agent]
        # Return newest first
        return list(reversed(exchanges[-(offset + limit):][:limit] if offset else exchanges[-limit:]))

    def on_graph_op(self, op_type: str, details: dict) -> None:
        """Callback invoked when a graph memory operation occurs."""
        self._broadcast({
            "type": "graph_op",
            "op": op_type,
            "details": {k: str(v)[:120] for k, v in details.items()},
        })

    def on_agent_done(self, agent_name: str, iterations: int, tool_calls: int, duration_ms: float, exit_reason: str) -> None:
        """Callback invoked when an agent finishes its agentic loop."""
        self._broadcast({
            "type": "agent_done",
            "agent": agent_name,
            "iterations": iterations,
            "tool_calls": tool_calls,
            "duration_ms": duration_ms,
            "exit_reason": exit_reason,
        })

    # ------------------------------------------------------------------
    # Checkpoint handling (replaces blocking input())
    # ------------------------------------------------------------------

    async def request_checkpoint(
        self,
        phase: str,
        data: Any,
        cost_so_far: float,
    ) -> str:
        """Create a pending checkpoint and wait for a client response.

        Returns one of: 'continue', 'skip', 'abort'.
        """
        chk_id = f"chk_{uuid.uuid4().hex[:8]}"

        summary = self._checkpoint_summary(phase, data)

        self._broadcast({
            "type": "checkpoint",
            "id": chk_id,
            "phase": phase,
            "summary": summary,
            "cost_so_far": cost_so_far,
        })

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_checkpoints[chk_id] = future

        try:
            action = await asyncio.wait_for(future, timeout=600)  # 10min max
        except asyncio.TimeoutError:
            action = "continue"
            logger.warning("Checkpoint %s timed out, auto-continuing", chk_id)
        finally:
            self._pending_checkpoints.pop(chk_id, None)

        return action

    async def request_human_help(
        self,
        help_id: str,
        description: str,
        context: dict,
    ) -> str:
        """Send a help request to dashboard clients and wait for a response.

        Same Future pattern as checkpoints. The agent blocks until the human
        responds via the dashboard, or the 10-minute timeout expires.
        """
        self._broadcast({
            "type": "help_needed",
            "id": help_id,
            "description": description,
            "context": context,
        })

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_help[help_id] = future

        try:
            response = await asyncio.wait_for(future, timeout=600)  # 10min
        except asyncio.TimeoutError:
            response = ""
            logger.warning("Help request %s timed out", help_id)
        finally:
            self._pending_help.pop(help_id, None)

        return response

    def _checkpoint_summary(self, phase: str, data: Any) -> str:
        if phase == "discover":
            programs = data
            lines = [f"Selected {len(programs)} programs:"]
            for p in programs:
                lines.append(f"  - {p.name} ({p.platform}) ROI={p.roi_score:.2f}")
            return "\n".join(lines)
        elif phase == "analyze":
            findings = data
            lines = [f"{len(findings)} findings ready for reporting:"]
            for f in findings[:10]:
                lines.append(
                    f"  [{f.effective_severity.value.upper()}] {f.title} "
                    f"(~${f.ai_bounty_estimate:.0f})"
                )
            if len(findings) > 10:
                lines.append(f"  ... and {len(findings) - 10} more")
            return "\n".join(lines)
        return f"Checkpoint at phase '{phase}'"

    # ------------------------------------------------------------------
    # Phase tracking
    # ------------------------------------------------------------------

    def set_phase(self, phase: str, program: str = "") -> None:
        self._current_phase = phase
        self._current_program = program
        self._broadcast({
            "type": "phase",
            "phase": phase,
            "program": program,
            "status": "started",
        })

    # ------------------------------------------------------------------
    # Client → pipeline commands
    # ------------------------------------------------------------------

    async def handle_client_message(self, data: dict, ws: WebSocketResponse) -> None:
        """Process an upstream WebSocket message from a client."""
        msg_type = data.get("type", "")

        if msg_type == "control":
            await self._handle_control(data)

        elif msg_type == "checkpoint_response":
            self._handle_checkpoint_response(data)

        elif msg_type == "help_response":
            self._handle_help_response(data)

        elif msg_type == "inject":
            self._handle_inject(data)

        elif msg_type == "browser_watch":
            agent_id = data.get("agent")
            if agent_id:
                self._browser_subs[ws] = agent_id
            else:
                self._browser_subs.pop(ws, None)

        else:
            await ws.send_json({"type": "error", "message": f"Unknown type: {msg_type}"})

    async def _handle_control(self, data: dict) -> None:
        action = data.get("action", "")
        terminal = getattr(self.pipeline, "_terminal_controller", None)
        if terminal and action in ("pause", "resume", "stop"):
            await terminal.handle("/" + action)
            self._pipeline_state = {"pause": "paused", "resume": "running", "stop": "stopped"}[action]
            self._broadcast({"type": "status_update", "pipeline_state": self._pipeline_state})
            return
        if action == "pause":
            self._pipeline_state = "paused"
            self.pipeline.pause()
            self._broadcast({"type": "status_update", "pipeline_state": "paused"})
        elif action == "resume":
            self._pipeline_state = "running"
            self.pipeline.resume()
            self._broadcast({"type": "status_update", "pipeline_state": "running"})
        elif action == "stop":
            self._pipeline_state = "stopped"
            self.pipeline.stop()
            self._broadcast({"type": "status_update", "pipeline_state": "stopped"})
        elif action == "checkpoint":
            self._save_checkpoint()
        elif action == "set_budget":
            self._set_budget(data.get("value"))

    def _save_checkpoint(self) -> None:
        """Manually save a checkpoint from the dashboard."""
        from agents.swarm_pipeline import SwarmPipeline
        if isinstance(self.pipeline, SwarmPipeline):
            self.pipeline._save_checkpoint_now("swarm")
            self._broadcast({
                "type": "status_update",
                "checkpoint_saved": True,
                "message": "Checkpoint saved",
            })
            logger.info("Manual checkpoint saved via dashboard")
        else:
            self._broadcast({
                "type": "status_update",
                "checkpoint_saved": False,
                "message": "Checkpoints only available in swarm mode",
            })

    def _set_budget(self, value: Any) -> None:
        """Update the effective budget on the pipeline."""
        try:
            new_budget = float(value)
        except (TypeError, ValueError):
            self._broadcast({"type": "error", "message": "Invalid budget value"})
            return
        if new_budget <= 0:
            self._broadcast({"type": "error", "message": "Budget must be positive"})
            return
        self.pipeline.effective_budget = new_budget
        # Also update team's effective_budget so monitor picks it up
        team = getattr(self.pipeline, "_team", None)
        if team:
            team.effective_budget = new_budget
        logger.info("Budget updated to $%.2f via dashboard", new_budget)
        self._broadcast({
            "type": "status_update",
            "message": f"Budget set to ${new_budget:.2f}",
            "budget": new_budget,
        })

    def _handle_checkpoint_response(self, data: dict) -> None:
        chk_id = data.get("id", "")
        action = data.get("action", "continue")
        future = self._pending_checkpoints.get(chk_id)
        if future and not future.done():
            future.set_result(action)
        else:
            logger.warning("Checkpoint %s not found or already resolved", chk_id)

    def _handle_help_response(self, data: dict) -> None:
        help_id = data.get("id", "")
        response = data.get("response", "")
        future = self._pending_help.get(help_id)
        if future and not future.done():
            future.set_result(response)
        else:
            logger.warning("Help request %s not found or already resolved", help_id)

    def _handle_inject(self, data: dict) -> None:
        """Inject a god-mode directive onto the MessageBus."""
        from agents.messaging import AgentMessage

        recipient = data.get("recipient", "director")
        content = data.get("content", "")
        priority = data.get("priority", "critical")

        if not content:
            return

        msg = AgentMessage(
            sender="human",
            recipient=recipient,
            msg_type="directive",
            content=content,
            priority=priority,
        )
        self.pipeline.bus.send(msg)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        ct = self.pipeline.cost_tracker
        session = self.pipeline.session
        status = {
            "pipeline_state": self._pipeline_state,
            "current_phase": self._current_phase,
            "current_program": self._current_program,
            "cost": {
                "total": ct.get_total(),
                "budget": getattr(self.pipeline, "effective_budget", None) or self.pipeline.settings.session_budget,
                "breakdown": ct.get_breakdown(),
            },
            "findings": {
                "raw": len(session.raw_findings),
                "analyzed": len(session.analyzed_findings),
            },
            "programs": len(session.programs),
            # The UI is a pure consumer of this: what methodology/targets/
            # mechanisms this run is configured with (profiles/__init__.py).
            # No dashboard-specific wiring — just reading the pieces the
            # pipeline was already constructed with.
            "profile": {
                "methodology": getattr(getattr(self.pipeline, "methodology", None), "name",
                                        type(getattr(self.pipeline, "methodology", None)).__name__),
                "target_provider": type(getattr(self.pipeline, "target_provider", None)).__name__,
                "mechanisms": _mechanism_names(self.pipeline),
            },
        }

        # Active browser sessions
        pool = getattr(self.pipeline, "browser_pool", None)
        status["browsers"] = list(pool._sessions.keys()) if pool and pool._sessions else []

        # Graph memory stats
        graph = getattr(self.pipeline, "graph", None)
        if graph and graph.available:
            status["graph"] = {
                "connected": True,
                "total_ops": graph.total_ops,
                "op_counts": graph.op_counts,
            }
        else:
            status["graph"] = {"connected": False}

        # Swarm/Group mode: add agent count and shared memory stats
        is_swarm_like = getattr(self.pipeline, "shared_memory", None) is not None
        if is_swarm_like:
            is_group = getattr(self.pipeline, "_group_mode", False)
            status["mode"] = "group" if is_group else "swarm"
            agents_status = []
            terminated = getattr(self.pipeline, "_team_terminated", None) or getattr(self.pipeline, "_terminated_agents", set())

            # In group mode, get agents from the group; in swarm mode, from pipeline
            group = getattr(self.pipeline, "_group", None)
            if group and is_group:
                agents_status = group.get_all_agents_status()
                # Add team summary
                status["teams"] = group.get_team_summary()
            else:
                for a in self.pipeline.pentesters:
                    trace = a.last_trace
                    is_terminated = a.agent_id in terminated
                    exit_reason = trace.exit_reason if trace else ""
                    if is_terminated and not exit_reason:
                        score = self.pipeline.shared_memory.get_score(a.agent_id) if self.pipeline.shared_memory else None
                        exit_reason = f"terminated (score {score.total})" if score else "terminated"
                    agent_info: dict = {
                        "id": a.agent_id,
                        "iterations": trace.iterations if trace else 0,
                        "max_iterations": a.max_iterations,
                        "tool_calls": len(trace.tool_calls) if trace else 0,
                        "max_tool_calls": a.max_total_tool_calls,
                        "unique_calls": len(a._seen_calls),
                        "exit_reason": exit_reason,
                        "terminated": is_terminated,
                    }
                    agents_status.append(agent_info)

            # Include validator/auditor agent
            validator = getattr(self.pipeline, "validator", None)
            auditor = group.auditor if group and is_group else None
            display_validator = auditor or validator
            if display_validator:
                vt = display_validator.last_trace
                agents_status.append({
                    "id": display_validator.agent_id,
                    "iterations": vt.iterations if vt else 0,
                    "max_iterations": display_validator.max_iterations,
                    "tool_calls": len(vt.tool_calls) if vt else 0,
                    "max_tool_calls": display_validator.max_total_tool_calls,
                    "unique_calls": len(display_validator._seen_calls),
                    "exit_reason": vt.exit_reason if vt else "",
                    "terminated": bool(vt and vt.exit_reason),
                    "role": "auditor" if auditor else "validator",
                })
            status["swarm"] = {
                "num_agents": self.pipeline.num_agents,
                "agents": agents_status,
            }
            if self.pipeline.shared_memory is not None:
                status["swarm"]["shared_memory"] = self.pipeline.shared_memory.summary()
            recorder = getattr(self.pipeline, "recorder", None)
            if recorder:
                status["swarm"]["recorder"] = recorder.summary()
        else:
            status["mode"] = "lab"

        return status

    def get_profile(self) -> dict:
        """Full run configuration — methodology/target-provider/scoring/
        mechanisms/topology — in the same shape profiles.Profile.summary()
        returns. This is the API surface: any consumer (the dashboard, a
        script, an external agent) reads this to know what the running
        session is configured to do; it never needs pipeline internals."""
        from dataclasses import asdict
        p = self.pipeline
        methodology = getattr(p, "methodology", None)
        target_provider = getattr(p, "target_provider", None)
        scoring = getattr(p, "scoring", None)
        mechanisms = _mechanism_names(p)
        is_group = getattr(p, "_group_mode", False)
        mode = "group" if is_group else ("swarm" if hasattr(p, "num_agents") else "lab")
        return {
            "interaction": {"mode": getattr(p, "interaction_mode", "web")},
            "workflow": asdict(p.workflow) if mode == "lab" and getattr(p, "workflow", None) else None,
            "methodology": getattr(methodology, "name", type(methodology).__name__) if methodology else None,
            "target_provider": type(target_provider).__name__ if target_provider else None,
            "scoring": asdict(scoring) if scoring else None,
            "mechanisms": mechanisms,
            "topology": {
                "mode": mode,
                "agents_per_team": getattr(p, "num_agents", None),
                "num_teams": getattr(p, "_num_teams", None) if is_group else None,
            },
        }

    async def _browser_capture_loop(self) -> None:
        """Capture screenshots from watched browser sessions at ~1.5 FPS."""
        try:
            while True:
                await asyncio.sleep(0.67)
                if not self._browser_subs:
                    continue

                pool = getattr(self.pipeline, "browser_pool", None)
                if not pool:
                    continue

                # Prune closed WS clients
                stale = [ws for ws in self._browser_subs if ws.closed]
                for ws in stale:
                    self._browser_subs.pop(ws, None)

                # Group subscribers by agent_id
                agent_clients: dict[str, list[WebSocketResponse]] = {}
                for ws, agent_id in self._browser_subs.items():
                    agent_clients.setdefault(agent_id, []).append(ws)

                for agent_id, clients in agent_clients.items():
                    session = await pool.get_session(agent_id)
                    if not session or not session._page or session._page.is_closed():
                        # No active page — send null frame
                        payload = json.dumps({
                            "type": "browser_frame",
                            "agent": agent_id,
                            "url": "",
                            "title": "",
                            "image": None,
                        })
                        for ws in clients:
                            if not ws.closed:
                                try:
                                    asyncio.ensure_future(ws.send_str(payload))
                                except Exception:
                                    pass
                        continue

                    try:
                        image_b64 = await session.screenshot_jpeg(quality=60)
                        url = session.current_url
                        title = await session.get_title()
                    except Exception:
                        continue

                    payload = json.dumps({
                        "type": "browser_frame",
                        "agent": agent_id,
                        "url": url,
                        "title": title,
                        "image": f"data:image/jpeg;base64,{image_b64}",
                    })
                    for ws in clients:
                        if not ws.closed:
                            try:
                                asyncio.ensure_future(ws.send_str(payload))
                            except Exception:
                                pass
        except asyncio.CancelledError:
            pass

    async def _status_loop(self) -> None:
        """Broadcast status updates every 2 seconds."""
        try:
            while True:
                await asyncio.sleep(2)
                if self.clients:
                    status = self.get_status()
                    status["type"] = "status"
                    self._broadcast(status)
        except asyncio.CancelledError:
            pass
