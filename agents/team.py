"""AgentTeam — a team of pen-tester agents managed by a team lead (Director).

An AgentTeam encapsulates:
- A SharedMemory instance (per-team)
- N PenTesterAgent instances
- A monitor loop that enforces budget/guardrails
- Director review integration (team lead supervises its agents)

The team does NOT own the Director, MessageBus, CostTracker, or Graph — these
are shared resources injected from the pipeline.  This design enables future
multi-team (AgentGroup) mode where teams share a Director and cost tracker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from agents.llm_config import LLMConfigMap, create_llm_for_agent
from agents.messaging import AgentMessage, MessageBus, TaskAssignment
from agents.pentester import PenTesterAgent
from memory.shared import SharedMemory
from models.finding import Severity, VulnFinding, VulnType

if TYPE_CHECKING:
    from agents.director import Director
    from agents.recorder import Recorder
    from cost.tracker import CostTracker
    from memory.graph import GraphMemory
    from memory.store import PersistentMemory
    from utils.browser import BrowserPool
    from web.bridge import PipelineBridge

logger = logging.getLogger(__name__)


class AgentTeam:
    """A team of pen-tester agents coordinating via shared memory."""

    MAX_REPLACEMENTS_PER_SLOT = 2

    def __init__(
        self,
        team_id: str,
        num_agents: int,
        director: Director | None,
        bus: MessageBus,
        cost_tracker: CostTracker,
        persistent: PersistentMemory,
        graph: GraphMemory | None = None,
        llm_config: LLMConfigMap | None = None,
        dry_run: bool = False,
        settings: Any = None,
        methodology: Any = None,
    ) -> None:
        self.team_id = team_id
        self.num_agents = num_agents
        self.director = director
        self.bus = bus
        self.cost_tracker = cost_tracker
        self.persistent = persistent
        self.graph = graph
        self._llm_config = llm_config or LLMConfigMap()
        self.dry_run = dry_run
        self.settings = settings
        if methodology is None:
            from agents.methodology import PentestMethodology
            methodology = PentestMethodology()
        self.methodology = methodology

        # State
        self.shared_memory: SharedMemory | None = None
        self.pentesters: list[PenTesterAgent] = []
        self.recorder: Recorder | None = None
        self._terminated_agents: set[str] = set()
        self._stop_flag = False
        self._pause_event: asyncio.Event = asyncio.Event()
        self._pause_event.set()  # not paused

        # Injected resources (set before run)
        self.browser_pool: BrowserPool | None = None
        self.web_bridge: PipelineBridge | None = None
        self._ollama_urls: list[str] | None = None

        # Run state
        self._all_domains: list[str] = []
        self._assignment_context: str = ""
        self._current_scan_id: int = 0
        self.effective_budget: float | None = None

        # External tasks the monitor should track (e.g. auditor)
        self._external_tasks: list[asyncio.Task] = []
        # Non-pentester agents that should receive director redirects (e.g. validator)
        self._extra_agents: dict[str, Any] = {}
        # Callback invoked when first finding appears (for auditor lazy-start)
        self.on_first_finding: Callable[[], Awaitable[asyncio.Task | None]] | None = None
        # Callback invoked every 4th monitor tick (e.g. checkpoint save)
        self.on_periodic: Callable[[], None] | None = None
        # Running tasks — populated during run(), exposed for cancel_all()
        self._running_tasks: list[asyncio.Task] = []
        self.run_errors: list[str] = []

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setup_shared_memory(
        self,
        domains: list[str],
        scope_info: dict[str, str] | None = None,
        quality_gates: dict | None = None,
        persist_callback: Callable | None = None,
    ) -> SharedMemory:
        """Create and configure the team's SharedMemory."""
        self.shared_memory = SharedMemory()
        self._all_domains = domains

        # Seed targets
        for domain in domains:
            self.shared_memory.post(
                agent_id="pipeline",
                category="target",
                key=f"target:{domain}",
                content=domain,
            )

        # Seed scope
        if scope_info:
            for url, text in scope_info.items():
                self.shared_memory.post(
                    agent_id="pipeline",
                    category="scope",
                    key=f"scope:{url}",
                    content=text,
                )

        # Quality gates
        if quality_gates:
            self.shared_memory._quality_gates = quality_gates

        # Wire finding persistence callback
        if persist_callback:
            self.shared_memory.on_finding_accepted = persist_callback

        return self.shared_memory

    # ------------------------------------------------------------------
    # Agent spawning
    # ------------------------------------------------------------------

    async def spawn_agents(
        self,
        keep_recent: int | None = None,
    ) -> None:
        """Create N PenTesterAgent instances with LLMs and browser sessions."""
        self.pentesters = []
        for i in range(self.num_agents):
            await self._spawn_single_agent(
                f"pentester-{i}",
                ollama_index=i,
                keep_recent=keep_recent,
            )

    async def _spawn_single_agent(
        self,
        agent_id: str,
        *,
        ollama_index: int | None = None,
        keep_recent: int | None = None,
    ) -> PenTesterAgent:
        """Create a single PenTesterAgent with LLM, browser, and all wiring."""
        cli_mode = self.settings.cli_tool_mode if self.settings else False
        help_cb = self.web_bridge.request_human_help if self.web_bridge else None

        # LLM instance
        if self._ollama_urls and ollama_index is not None and ollama_index < len(self._ollama_urls):
            from llm.ollama_provider import OllamaProvider
            llm = None if self.dry_run else OllamaProvider(base_url=self._ollama_urls[ollama_index])
            if llm:
                logger.info("Agent %s → %s", agent_id, self._ollama_urls[ollama_index])
        else:
            llm = None if self.dry_run else create_llm_for_agent("scanner", self._llm_config)

        # Browser session
        browser_session = None
        if self.browser_pool:
            try:
                browser_session = await self.browser_pool.create_session(agent_id)
            except Exception:
                logger.debug("Could not create browser session for %s", agent_id)

        auth_testing = self.settings.auth_testing if self.settings else False
        compact = self.settings.compact_prompt if self.settings else False
        agent = PenTesterAgent(
            agent_id=agent_id,
            bus=self.bus,
            llm=llm,
            shared_memory=self.shared_memory,
            browser_session=browser_session,
            help_callback=help_cb,
            cli_mode=cli_mode,
            cost_tracker=self.cost_tracker,
            auth_testing=auth_testing,
            methodology=self.methodology,
            compact=compact,
        )
        agent.session_budget = self.effective_budget
        agent._pause_event = self._pause_event
        if keep_recent is not None:
            agent.keep_recent = keep_recent
        if self.director and self.director.llm:
            agent.compress_llm = self.director.llm
        agent._allowed_domains = set(self._all_domains) if self._all_domains else None

        if self.graph:
            from agents.tools import create_graph_memory_tools
            agent.tools.merge(create_graph_memory_tools(self.graph))

        if self.web_bridge:
            bridge = self.web_bridge
            agent.on_tool_start = lambda name, tool, inp, _id=agent_id: bridge.on_tool_start(_id, tool, inp)
            agent.on_tool_call = lambda tc, n=agent_id: bridge.on_tool_call(n, tc)
            agent.on_llm_start = lambda name, model, msgs, tools, _id=agent_id: bridge.on_llm_start(_id, model, msgs, tools)
            agent.on_llm_call = lambda name, model, msgs, tools, dur, reasoning="", _id=agent_id: bridge.on_llm_call(_id, model, msgs, tools, dur, reasoning)
            agent.on_llm_exchange = lambda name, model, msgs, resp, dur, _id=agent_id: bridge.on_llm_exchange(_id, model, msgs, resp, dur)
            agent.on_agent_done = lambda name, iters, calls, dur, reason, _id=agent_id: bridge.on_agent_done(_id, iters, calls, dur, reason)

        self.pentesters.append(agent)
        logger.info("Spawned %s (team: %s)", agent_id, self.team_id)
        return agent

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(
        self,
        targets: list[str],
        assignment_context: str,
        budget: float,
    ) -> list[VulnFinding]:
        """Run all agents concurrently, return deduplicated findings."""
        self._assignment_context = assignment_context

        # Create agent tasks
        tasks: list[asyncio.Task] = []
        for agent in self.pentesters:
            task = asyncio.create_task(
                agent.execute(TaskAssignment(
                    agent_name=agent.agent_id,
                    task_type="pentest",
                    parameters={"targets": targets},
                    context=assignment_context,
                )),
                name=agent.agent_id,
            )
            tasks.append(task)

        # Include any external tasks (monitor tracks but doesn't own them)
        tasks.extend(self._external_tasks)

        # Monitor task
        monitor = asyncio.create_task(
            self._monitor(tasks, budget),
            name="swarm-monitor",
        )
        tasks.append(monitor)

        # Expose for cancel_all()
        self._running_tasks = tasks

        # Run all
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            logger.exception("Team %s: gather failed", self.team_id)

        # Cancel monitor if still running
        if not monitor.done():
            monitor.cancel()

        # Collect findings
        findings = self.shared_memory.get_findings() if self.shared_memory else []
        return findings

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------

    async def _monitor(
        self,
        agent_tasks: list[asyncio.Task],
        budget: float,
    ) -> None:
        """Background task that monitors team progress and enforces limits."""
        start = time.monotonic()
        last_findings_count = 0
        stale_since: float | None = None
        stale_limit = 300  # 5 minutes with no new findings
        monitor_tick = 0
        replacement_count: dict[str, int] = {}
        terminated_agents = self._terminated_agents
        auditor_launched = False

        # Build agent lookup for directive routing
        agent_map: dict[str, PenTesterAgent] = {
            agent.agent_id: agent for agent in self.pentesters
        }
        agent_map.update(self._extra_agents)

        review_interval = (self.shared_memory.coordination.director_review_interval_s
                           if self.shared_memory else 30)
        try:
            while True:
                for _ in range(review_interval):
                    await asyncio.sleep(1)
                    if self._stop_flag:
                        break
                monitor_tick += 1

                elapsed = time.monotonic() - start
                current_findings = self.shared_memory.findings_count if self.shared_memory else 0
                claimed = len(self.shared_memory.claims) if self.shared_memory else 0
                entries = self.shared_memory.entry_count if self.shared_memory else 0

                # Check if all agent tasks are done
                active = [t for t in agent_tasks if not t.done() and t.get_name() != "swarm-monitor"]
                if not active:
                    logger.info("Team %s: all agents have finished", self.team_id)
                    return

                logger.info(
                    "Team %s monitor: %.0fs elapsed, %d findings, %d claims, "
                    "%d entries, %d/%d agents active",
                    self.team_id, elapsed, current_findings, claimed, entries,
                    len(active), self.num_agents,
                )

                # Periodic callback (e.g. checkpoint save) — every 4th tick (~2min)
                if monitor_tick % 4 == 0 and self.on_periodic:
                    try:
                        self.on_periodic()
                    except Exception:
                        logger.debug("on_periodic callback failed", exc_info=True)

                # Lazy-start auditor when first finding appears
                if (not auditor_launched and self.on_first_finding
                        and current_findings > 0):
                    auditor_launched = True
                    logger.info("Team %s: first finding — triggering auditor launch", self.team_id)
                    try:
                        auditor_task = await self.on_first_finding()
                        if auditor_task:
                            agent_tasks.append(auditor_task)
                            # Refresh agent_map with any newly added extra agents
                            agent_map.update(self._extra_agents)
                    except Exception:
                        logger.exception("Failed to launch auditor")

                # Budget
                active_budget = self.effective_budget or budget
                if not self.cost_tracker.check_budget(active_budget):
                    logger.warning("Team %s: budget exceeded — stopping", self.team_id)
                    for t in active:
                        agent = agent_map.get(t.get_name())
                        if agent:
                            agent._cancel_reason = f"budget exceeded (${self.cost_tracker.get_total():.2f}/${active_budget:.2f})"
                        t.cancel()
                    return

                # Stop flag
                if self._stop_flag:
                    logger.info("Team %s: stop requested", self.team_id)
                    n_active = len(active)
                    for t in active:
                        agent = agent_map.get(t.get_name())
                        if agent:
                            agent._cancel_reason = "user stop requested"
                        t.cancel()
                    for tick in range(15):
                        await asyncio.sleep(1)
                        still_running = [t for t in active if not t.done()]
                        if not still_running:
                            break
                    return

                # Stale detection
                if current_findings > last_findings_count:
                    last_findings_count = current_findings
                    stale_since = None
                else:
                    if stale_since is None:
                        stale_since = time.monotonic()
                    elif time.monotonic() - stale_since >= stale_limit:
                        logger.warning("Team %s: stale for %ds", self.team_id, stale_limit)

                # Director active supervision — every 2nd tick (~60s)
                if monitor_tick % 2 == 0 and self.shared_memory and self.director:
                    active_ids = {t.get_name() for t in active}
                    await self._director_review(agent_map, elapsed, active_budget, active_ids)

                # Score-based termination
                if self.shared_memory:
                    to_terminate = self.shared_memory.get_agents_to_terminate()
                    for bad_id in to_terminate:
                        if bad_id in terminated_agents or bad_id == "validator":
                            continue
                        for t in agent_tasks:
                            if t.get_name() == bad_id and not t.done():
                                bad_score = self.shared_memory.get_score(bad_id).total
                                logger.warning(
                                    "Team %s: terminating %s (score %d)",
                                    self.team_id, bad_id, bad_score,
                                )
                                agent = agent_map.get(bad_id)
                                if agent:
                                    agent._cancel_reason = f"score too low ({bad_score})"
                                t.cancel()
                                terminated_agents.add(bad_id)

                                # Release claims
                                for key, owner in list(self.shared_memory.claims.items()):
                                    if owner == bad_id:
                                        await self.shared_memory.release(bad_id, key)

                                # Clean up browser session
                                if self.browser_pool:
                                    try:
                                        await self.browser_pool.close_session(bad_id)
                                    except Exception:
                                        logger.debug("Failed to close browser for %s", bad_id)

                                # Spawn replacement
                                # Extract base agent ID (e.g. "pentester-2r1" → "pentester-2")
                                import re as _re
                                base_id = _re.sub(r'r\d+$', '', bad_id)
                                rcount = replacement_count.get(base_id, 0) + 1
                                replacement_count[base_id] = rcount

                                if rcount > self.MAX_REPLACEMENTS_PER_SLOT:
                                    logger.warning("Max replacements for %s", base_id)
                                    self.bus.send(AgentMessage(
                                        sender="director",
                                        recipient="all",
                                        msg_type="observation",
                                        content=f"{bad_id} terminated (score {bad_score}). No replacement — max respawns reached.",
                                        data={"type": "agent_terminated", "agent": bad_id, "score": bad_score},
                                    ))
                                    continue

                                new_id = f"{base_id}r{rcount}"
                                self.bus.send(AgentMessage(
                                    sender="director",
                                    recipient="all",
                                    msg_type="observation",
                                    content=f"{bad_id} terminated (score {bad_score}). Replacement: {new_id}",
                                    data={"type": "agent_replaced", "old": bad_id, "new": new_id, "score": bad_score},
                                ))

                                try:
                                    original_idx = int(base_id.split("-")[1]) if "-" in base_id else None
                                    new_agent = await self._spawn_single_agent(new_id, ollama_index=original_idx)
                                    agent_map[new_id] = new_agent

                                    replace_ctx = (
                                        self._assignment_context +
                                        f"\n\n=== REPLACEMENT AGENT ===\n"
                                        f"You are replacing {bad_id} who was terminated for poor performance "
                                        f"(score {bad_score}). Check shared memory for prior work. "
                                        f"Avoid the same mistakes — focus on deep testing with real evidence.\n"
                                        f"=== END ==="
                                    )

                                    new_task = asyncio.create_task(
                                        new_agent.execute(TaskAssignment(
                                            agent_name=new_id,
                                            task_type="pentest",
                                            parameters={"targets": self._all_domains},
                                            context=replace_ctx,
                                        )),
                                        name=new_id,
                                    )
                                    agent_tasks.append(new_task)
                                    logger.info("Replacement %s spawned for %s", new_id, bad_id)
                                except Exception:
                                    logger.exception("Failed to spawn replacement for %s", bad_id)
                                break

        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Director review
    # ------------------------------------------------------------------

    async def _director_review(
        self,
        agent_map: dict[str, PenTesterAgent],
        elapsed_seconds: float,
        budget: float,
        active_ids: set[str] | None = None,
    ) -> None:
        """Ask the director to review this team's agents."""
        if not self.shared_memory or not self.director:
            return

        logger.info("Team %s: director reviewing progress...", self.team_id)

        claims = self.shared_memory.claims
        agent_activity: dict[str, dict[str, Any]] = {}

        for agent_id, agent in agent_map.items():
            recent_calls: list[str] = []
            iterations = 0
            trace = agent.last_trace
            if trace:
                iterations = trace.iterations
                for tc in trace.tool_calls[-10:]:
                    status = f"error: {tc.error}" if tc.error else "ok"
                    output_preview = ""
                    if tc.output and not tc.error:
                        output_preview = str(tc.output)[:80]
                    recent_calls.append(
                        f"{tc.tool_name}({', '.join(f'{k}={v}' for k, v in list(tc.input.items())[:2])}) "
                        f"→ {status}" + (f" [{output_preview}]" if output_preview else "")
                    )

            agent_claims = [k for k, v in claims.items() if v == agent_id]
            agent_findings = sum(
                1 for e in self.shared_memory.read_all(category="finding")
                if e.agent_id == agent_id
            )

            agent_activity[agent_id] = {
                "recent_calls": recent_calls,
                "claims": agent_claims,
                "findings": agent_findings,
                "iterations": iterations,
            }

        current_findings = self.shared_memory.get_findings()

        try:
            redirects, scores, human_response = await self.director.review_swarm_progress(
                shared_memory_summary=self.shared_memory.summary(),
                agent_activity=agent_activity,
                elapsed_minutes=elapsed_seconds / 60,
                budget_spent=self.cost_tracker.get_total(),
                budget_total=budget,
                findings_count=self.shared_memory.findings_count,
                cost_tracker=self.cost_tracker,
                shared_memory=self.shared_memory,
                findings=current_findings,
            )
        except Exception:
            logger.exception("Team %s: director review failed", self.team_id)
            return

        if human_response:
            self.bus.send(AgentMessage(
                sender="director",
                recipient="human",
                msg_type="response",
                content=human_response,
                data={"type": "director_response"},
            ))

        for score_entry in scores:
            aid = score_entry["agent"]
            delta = score_entry["delta"]
            reason = score_entry.get("reason", "director review")
            self.shared_memory.add_score(aid, delta, f"director: {reason}")
            logger.info("Team %s: director score → %s: %+d (%s)", self.team_id, aid, delta, reason)
            self.bus.send(AgentMessage(
                sender="director",
                recipient=aid,
                msg_type="observation",
                content=f"Score adjustment: {delta:+d} — {reason}",
                data={"type": "score", "delta": delta, "reason": reason},
            ))

        if not redirects:
            logger.info("Team %s: director review — all agents on track", self.team_id)
            return

        for redirect in redirects:
            agent_id = redirect["agent"]
            instruction = redirect["instruction"]
            agent = agent_map.get(agent_id)
            if agent is None:
                logger.warning("Team %s: director redirect targets unknown agent: %s", self.team_id, agent_id)
                continue
            if active_ids is not None and agent_id not in active_ids:
                logger.info("Team %s: director redirect skipped — %s already finished", self.team_id, agent_id)
                continue

            agent.inject_directive(instruction)
            logger.info("Team %s: director redirect → %s: %s", self.team_id, agent_id, instruction[:120])
            self.bus.send(AgentMessage(
                sender="director",
                recipient=agent_id,
                msg_type="directive",
                content=instruction,
                data={"type": "swarm_redirect", "target_agent": agent_id},
            ))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_agents_status(self) -> list[dict]:
        """Return per-agent status dicts for the web bridge."""
        agents_status = []
        for agent in self.pentesters:
            t = agent.last_trace
            is_terminated = agent.agent_id in self._terminated_agents
            agents_status.append({
                "id": agent.agent_id,
                "iterations": t.iterations if t else 0,
                "max_iterations": agent.max_iterations,
                "tool_calls": len(t.tool_calls) if t else 0,
                "max_tool_calls": agent.max_total_tool_calls,
                "unique_calls": len(agent._seen_calls),
                "exit_reason": t.exit_reason if t else "",
                "terminated": is_terminated,
            })
        return agents_status

    def stop(self) -> None:
        """Signal the team to stop."""
        self._stop_flag = True

    def cancel_all(self) -> None:
        """Force-cancel all running tasks."""
        for t in self._running_tasks:
            if not t.done():
                t.cancel()

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()
