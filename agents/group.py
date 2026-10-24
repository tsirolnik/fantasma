"""AgentGroup — orchestrates multiple AgentTeams for competitive multi-team scanning.

Architecture:
    AgentGroup (CEO)
    ├── Director (shared — oversees all TeamLeads)
    ├── AgentTeam "alpha" (TeamLead + N Pentesters + SharedMemory)
    ├── AgentTeam "beta"  (TeamLead + N Pentesters + SharedMemory)
    ├── ...
    └── Auditor (cross-team validator — transparent to agents)

Key design:
    - Each team has its OWN SharedMemory (isolated claims/findings per team)
    - GraphMemory is GLOBAL (shared knowledge across all teams)
    - Auditor pulls findings from ALL teams, verifies independently
    - Director reallocates budget between teams based on results
    - Findings are deduped at the group level before reporting
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import TYPE_CHECKING, Any

from agents.llm_config import LLMConfigMap, create_llm_for_agent
from agents.messaging import AgentMessage, MessageBus, TaskAssignment
from agents.team import AgentTeam
from agents.validator import ValidatorAgent
from memory.shared import SharedMemory
from models.finding import VulnFinding

if TYPE_CHECKING:
    from agents.director import Director
    from agents.recorder import Recorder
    from cost.tracker import CostTracker
    from memory.graph import GraphMemory
    from memory.store import PersistentMemory
    from utils.browser import BrowserPool
    from web.bridge import PipelineBridge

logger = logging.getLogger(__name__)


class AgentGroup:
    """Orchestrates multiple AgentTeams running concurrently.

    Each team has its own SharedMemory and set of pentesters.
    The group manages cross-team deduplication, budget allocation,
    and an optional cross-team auditor (validator).
    """

    def __init__(
        self,
        num_teams: int,
        agents_per_team: int,
        director: Director,
        bus: MessageBus,
        cost_tracker: CostTracker,
        persistent: PersistentMemory,
        graph: GraphMemory | None = None,
        llm_config: LLMConfigMap | None = None,
        dry_run: bool = False,
        settings: Any = None,
        methodology: Any = None,
        scoring: Any = None,
        mechanisms: Any = None,
        coordination: Any = None,
    ) -> None:
        self.num_teams = num_teams
        self.agents_per_team = agents_per_team
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
        from memory.scoring_policy import DEFAULT_SCORING
        self.scoring = scoring or DEFAULT_SCORING
        self.mechanisms: list = list(mechanisms) if mechanisms else []
        from memory.coordination_policy import DEFAULT_COORDINATION
        self.coordination = coordination or DEFAULT_COORDINATION

        # Teams
        self.teams: list[AgentTeam] = []
        self._team_names = self._generate_team_names(num_teams)

        # Cross-team auditor (validator)
        self.auditor: ValidatorAgent | None = None
        self._auditor_task: asyncio.Task | None = None

        # Aggregated state
        self._all_findings: list[VulnFinding] = []
        self._stop_flag = False
        self.run_errors: list[str] = []
        self.effective_budget: float | None = None

        # Injected resources
        self.browser_pool: BrowserPool | None = None
        self.web_bridge: PipelineBridge | None = None
        self._ollama_urls: list[str] | None = None

        # For SIGINT / cancel_all
        self._team_tasks: list[asyncio.Task] = []

        # Callbacks
        self.on_finding_persisted: Any = None  # async callback(agent_id, finding)
        self.on_periodic: Any = None  # sync callback() for checkpoints

    @staticmethod
    def _generate_team_names(n: int) -> list[str]:
        """Generate team names: alpha, beta, gamma, ..."""
        names = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta",
                 "eta", "theta", "iota", "kappa"]
        return names[:n] if n <= len(names) else [f"team-{i}" for i in range(n)]

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def create_teams(self) -> list[AgentTeam]:
        """Create all AgentTeam instances (but don't spawn agents yet)."""
        self.teams = []
        budget_per_team = (self.effective_budget or 0) / max(self.num_teams, 1)

        for i, name in enumerate(self._team_names):
            team = AgentTeam(
                team_id=name,
                num_agents=self.agents_per_team,
                director=self.director,
                bus=self.bus,
                cost_tracker=self.cost_tracker,
                persistent=self.persistent,
                graph=self.graph,
                llm_config=self._llm_config,
                dry_run=self.dry_run,
                settings=self.settings,
                methodology=self.methodology,
            )
            team.browser_pool = self.browser_pool
            team.web_bridge = self.web_bridge
            team._ollama_urls = self._ollama_urls
            team.on_periodic = self.on_periodic
            self.teams.append(team)

        return self.teams

    async def setup_teams(
        self,
        all_domains: list[str],
        scope_info: dict[str, str] | None = None,
        finding_callback: Any = None,
    ) -> None:
        """Set up SharedMemory and spawn agents for each team.

        Each team gets ALL domains (they compete on the same targets).
        Teams coordinate via claims within their own SharedMemory.
        """
        for team in self.teams:
            team._all_domains = all_domains
            team.effective_budget = self.effective_budget

            # Each team gets its own SharedMemory
            # Finding IDs are globally unique (process-wide counter in shared.py)
            sm = SharedMemory(scoring=self.scoring, coordination=self.coordination)
            sm.team_id = team.team_id
            sm.configure_mechanisms(
                self.mechanisms or (self.settings.mechanisms.split(",") if getattr(self.settings, "mechanisms", None) else None))
            sm.allowed_domains = {d.lower() for d in all_domains}

            # Wire incremental persistence callback
            if finding_callback:
                sm.on_finding = finding_callback

            # Seed targets
            for domain in all_domains:
                await sm.post(
                    "pipeline", "discovery", "target",
                    f"target:{domain}", json.dumps({"domain": domain}),
                )

            # Seed scope
            if scope_info:
                for url, text in scope_info.items():
                    await sm.post(
                        "pipeline", "note", "scope",
                        f"scope:{url}", text[:2000],
                    )

            team.shared_memory = sm

            # Spawn agents
            keep_recent = getattr(self.settings, "keep_recent", None) if self.settings else None
            await team.spawn_agents(keep_recent=keep_recent)

            # Wire auditor lazy-start on first finding (any team)
            team.on_first_finding = self._make_auditor_launcher(team)

        logger.info(
            "Group: created %d teams × %d agents = %d total pentesters",
            self.num_teams, self.agents_per_team,
            self.num_teams * self.agents_per_team,
        )

    def _make_auditor_launcher(self, triggering_team: AgentTeam):
        """Create a callback that launches the auditor on first finding from any team."""
        async def _launch():
            if self._auditor_task is not None:
                return None  # already launched by another team
            if not self.auditor:
                return None

            total_findings = sum(
                t.shared_memory.findings_count for t in self.teams
                if t.shared_memory
            )
            logger.info("Group: first finding — launching cross-team auditor (%d findings total)", total_findings)

            self._auditor_task = asyncio.create_task(
                self.auditor.execute(TaskAssignment(
                    agent_name=self.auditor.agent_id,
                    task_type="validate",
                    parameters={},
                    context=(
                        f"There are {total_findings} finding(s) across {self.num_teams} teams. "
                        "Use read_findings to see them, then independently verify "
                        "each one with attack tools. Call verify_finding with your result. "
                        "Keep checking for new findings as teams submit them."
                    ),
                )),
                name=self.auditor.agent_id,
            )
            # Register auditor in each team's extra_agents for directive routing
            for team in self.teams:
                team._extra_agents[self.auditor.agent_id] = self.auditor

            return self._auditor_task
        return _launch

    async def spawn_auditor(
        self,
        pause_event: asyncio.Event | None = None,
    ) -> ValidatorAgent:
        """Create the cross-team auditor (validator).

        The auditor's SharedMemory is a merged view — it reads findings
        from ALL teams. We create a special SharedMemory that aggregates.
        """
        agent_id = "auditor"
        cli_mode = self.settings.cli_tool_mode if self.settings else False
        help_cb = self.web_bridge.request_human_help if self.web_bridge else None

        llm = None if self.dry_run else create_llm_for_agent("validator", self._llm_config)

        browser_session = None
        if self.browser_pool:
            try:
                browser_session = await self.browser_pool.create_session(agent_id)
            except Exception:
                logger.debug("Could not create browser session for auditor")

        # Auditor gets a merged SharedMemory that reads from all teams
        auditor_sm = self._create_merged_shared_memory()

        agent = ValidatorAgent(
            agent_id=agent_id,
            bus=self.bus,
            llm=llm,
            shared_memory=auditor_sm,
            browser_session=browser_session,
            help_callback=help_cb,
            cli_mode=cli_mode,
            cost_tracker=self.cost_tracker,
        )
        agent.session_budget = self.effective_budget
        if pause_event:
            agent._pause_event = pause_event
        if self.director and self.director.llm:
            agent.compress_llm = self.director.llm

        all_domains = set()
        for team in self.teams:
            all_domains.update(team._all_domains)
        agent._allowed_domains = all_domains or None

        if self.graph:
            from agents.tools import create_graph_memory_tools
            agent.tools.merge(create_graph_memory_tools(self.graph))

        if self.web_bridge:
            bridge = self.web_bridge
            agent.on_tool_start = lambda name, tool, inp: bridge.on_tool_start(agent_id, tool, inp)
            agent.on_tool_call = lambda tc: bridge.on_tool_call(agent_id, tc)
            agent.on_llm_start = lambda name, model, msgs, tools: bridge.on_llm_start(agent_id, model, msgs, tools)
            agent.on_llm_call = lambda name, model, msgs, tools, dur, reasoning="": bridge.on_llm_call(agent_id, model, msgs, tools, dur, reasoning)
            agent.on_llm_exchange = lambda name, model, msgs, resp, dur: bridge.on_llm_exchange(agent_id, model, msgs, resp, dur)
            agent.on_agent_done = lambda name, iters, calls, dur, reason: bridge.on_agent_done(agent_id, iters, calls, dur, reason)

        self.auditor = agent
        logger.info("Spawned cross-team auditor")
        return agent

    def _create_merged_shared_memory(self) -> SharedMemory:
        """Create a SharedMemory that aggregates findings from all teams.

        The auditor reads findings from all teams' SharedMemory instances.
        When the auditor updates verification status, it's reflected in the
        original team's SharedMemory (transparent to the team agents).
        """
        merged = SharedMemory()

        # Override get_findings to aggregate from all teams
        _original_get_findings = merged.get_findings

        def _merged_get_findings(**kwargs) -> list[VulnFinding]:
            all_findings = []
            seen_keys: set[str] = set()
            for team in self.teams:
                if team.shared_memory:
                    for f in team.shared_memory.get_findings():
                        key = f"{f.target}:{f.vuln_type.value}:{f.url}"
                        if key not in seen_keys:
                            seen_keys.add(key)
                            all_findings.append(f)
            return all_findings

        merged.get_findings = _merged_get_findings

        def _merged_get_finding_by_id(finding_id: int) -> VulnFinding | None:
            for team in self.teams:
                if team.shared_memory:
                    f = team.shared_memory.get_finding_by_id(finding_id)
                    if f is not None:
                        return f
            return None

        merged.get_finding_by_id = _merged_get_finding_by_id

        def _merged_get_findings_with_reporters() -> list[tuple[VulnFinding, str]]:
            all_pairs = []
            seen_keys: set[str] = set()
            for team in self.teams:
                if team.shared_memory:
                    for f, r in team.shared_memory.get_findings_with_reporters():
                        key = f"{f.target}:{f.vuln_type.value}:{f.url}"
                        if key not in seen_keys:
                            seen_keys.add(key)
                            all_pairs.append((f, r))
            return all_pairs

        merged.get_findings_with_reporters = _merged_get_findings_with_reporters

        # Aggregate findings_count across teams. Do this on a per-instance
        # SUBCLASS — never on type(merged), which is the shared SharedMemory
        # class: patching the class made every team's .findings_count re-invoke
        # this sum, causing infinite recursion (RecursionError) once the group
        # dashboard read team state. Sum the raw lists to stay recursion-proof.
        _teams = self.teams

        class _MergedSharedMemory(type(merged)):
            @property
            def findings_count(_self):
                return sum(len(t.shared_memory._findings)
                           for t in _teams if t.shared_memory)

        merged.__class__ = _MergedSharedMemory

        return merged

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(
        self,
        targets: list[str],
        assignment_context: str,
        budget: float,
    ) -> list[VulnFinding]:
        """Run all teams concurrently, return deduplicated findings."""

        # Create team run tasks
        team_coros = []
        for team in self.teams:
            team_ctx = (
                f"[Team {team.team_id.upper()}] {assignment_context}\n\n"
                f"You are part of team '{team.team_id}' — one of {self.num_teams} "
                f"competing teams. Your team has {self.agents_per_team} pen-testers. "
                f"Focus on finding unique, high-quality vulnerabilities."
            )
            team_coros.append(
                team.run(targets=targets, assignment_context=team_ctx, budget=budget)
            )

        # Run all teams concurrently
        self._team_tasks = [
            asyncio.create_task(coro, name=f"team-{self.teams[i].team_id}")
            for i, coro in enumerate(team_coros)
        ]

        # Also track auditor if it gets launched
        # (teams' on_first_finding callbacks will launch it)

        # Group monitor — checks cross-team metrics, budget reallocation
        monitor = asyncio.create_task(
            self._group_monitor(budget),
            name="group-monitor",
        )
        self._team_tasks.append(monitor)

        try:
            await asyncio.gather(*self._team_tasks, return_exceptions=True)
        except Exception:
            logger.exception("Group execution error")

        if not monitor.done():
            monitor.cancel()

        # Wait for auditor to finish
        if self._auditor_task and not self._auditor_task.done():
            self._auditor_task.cancel()
            try:
                await self._auditor_task
            except (asyncio.CancelledError, Exception):
                pass

        # Collect and deduplicate findings across all teams
        return self._collect_all_findings()

    async def _group_monitor(self, budget: float) -> None:
        """Monitor that tracks cross-team progress and reallocates budget."""
        tick = 0
        try:
            while True:
                for _ in range(60):  # 60s ticks (teams have their own 30s monitor)
                    await asyncio.sleep(1)
                    if self._stop_flag:
                        break
                tick += 1

                # Check if all teams are done
                active = [t for t in self._team_tasks
                          if not t.done() and t.get_name() != "group-monitor"]
                if not active:
                    logger.info("Group: all teams have finished")
                    return

                # Gather cross-team stats
                team_stats = []
                for team in self.teams:
                    findings_n = team.shared_memory.findings_count if team.shared_memory else 0
                    team_stats.append({
                        "team": team.team_id,
                        "findings": findings_n,
                        "agents": len(team.pentesters),
                        "active": len([a for a in team.pentesters
                                      if a.last_trace and not a.last_trace.exit_reason]),
                    })

                total_findings = sum(s["findings"] for s in team_stats)
                logger.info(
                    "Group monitor: tick %d, %d teams, %d total findings, "
                    "per-team: %s",
                    tick, self.num_teams, total_findings,
                    ", ".join(f"{s['team']}={s['findings']}" for s in team_stats),
                )

                # Budget check
                active_budget = self.effective_budget or budget
                if not self.cost_tracker.check_budget(active_budget):
                    logger.warning("Group: budget exceeded — stopping all teams")
                    self.stop()
                    return

                if self._stop_flag:
                    logger.info("Group: stop requested — stopping all teams")
                    for team in self.teams:
                        team.stop()
                    return

                # Periodic callback (checkpoints)
                if tick % 2 == 0 and self.on_periodic:
                    try:
                        self.on_periodic()
                    except Exception:
                        logger.debug("on_periodic failed", exc_info=True)

                # Cross-team dedup broadcast — every 3rd tick
                if tick % 3 == 0:
                    self._broadcast_cross_team_intel()

        except asyncio.CancelledError:
            pass

    def _broadcast_cross_team_intel(self) -> None:
        """Share high-value findings across teams via the message bus.

        Agents don't know about other teams — they just see "intel" messages
        from the director about interesting attack surfaces discovered elsewhere.
        """
        if self.teams and not self.teams[0].shared_memory.coordination.cross_team_sharing:
            return
        all_findings = self._collect_all_findings()
        if not all_findings:
            return

        # Group findings by target for cross-pollination
        targets_with_findings: dict[str, list[str]] = {}
        for f in all_findings:
            targets_with_findings.setdefault(f.target, []).append(
                f"{f.vuln_type.value}: {f.title or f.description[:60]}"
            )

        # Broadcast to all agents as a director observation
        if len(all_findings) > 0:
            summary_lines = []
            for target, vulns in list(targets_with_findings.items())[:5]:
                summary_lines.append(f"  {target}: {', '.join(vulns[:3])}")

            self.bus.send(AgentMessage(
                sender="director",
                recipient="all",
                msg_type="observation",
                content=(
                    f"Cross-team intelligence: {len(all_findings)} findings so far.\n"
                    + "\n".join(summary_lines)
                ),
                data={"type": "cross_team_intel", "findings_count": len(all_findings)},
            ))

    def _collect_all_findings(self) -> list[VulnFinding]:
        """Collect and deduplicate findings from all teams."""
        all_findings: list[VulnFinding] = []
        seen_keys: set[str] = set()

        for team in self.teams:
            if not team.shared_memory:
                continue
            for finding in team.shared_memory.get_findings():
                # Dedup by target + vuln_type + url
                key = f"{finding.target}:{finding.vuln_type.value}:{finding.url}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_findings.append(finding)

        return all_findings

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_all_agents_status(self) -> list[dict]:
        """Get status for all agents across all teams."""
        all_status = []
        for team in self.teams:
            for agent_status in team.get_agents_status():
                agent_status["team"] = team.team_id
                all_status.append(agent_status)

        # Include auditor
        if self.auditor:
            t = self.auditor.last_trace
            all_status.append({
                "id": self.auditor.agent_id,
                "team": "group",
                "role": "auditor",
                "iterations": t.iterations if t else 0,
                "max_iterations": self.auditor.max_iterations,
                "tool_calls": len(t.tool_calls) if t else 0,
                "max_tool_calls": self.auditor.max_total_tool_calls,
                "unique_calls": len(self.auditor._seen_calls),
                "exit_reason": t.exit_reason if t else "",
                "terminated": bool(t and t.exit_reason),
            })

        return all_status

    def get_team_summary(self) -> list[dict]:
        """Get per-team summary for the dashboard."""
        summaries = []
        for team in self.teams:
            findings = team.shared_memory.findings_count if team.shared_memory else 0
            entries = team.shared_memory.entry_count if team.shared_memory else 0
            active = len([a for a in team.pentesters
                         if a.last_trace and not a.last_trace.exit_reason])
            summaries.append({
                "team_id": team.team_id,
                "num_agents": len(team.pentesters),
                "active_agents": active,
                "findings": findings,
                "entries": entries,
            })
        return summaries

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop all teams."""
        self._stop_flag = True
        for team in self.teams:
            team.stop()

    def cancel_all(self) -> None:
        """Force-cancel all running tasks."""
        for team in self.teams:
            team.cancel_all()
        for t in self._team_tasks:
            if not t.done():
                t.cancel()
        if self._auditor_task and not self._auditor_task.done():
            self._auditor_task.cancel()

    def pause(self) -> None:
        for team in self.teams:
            team.pause()

    def resume(self) -> None:
        for team in self.teams:
            team.resume()

    @property
    def all_pentesters(self) -> list:
        """All pentesters across all teams."""
        result = []
        for team in self.teams:
            result.extend(team.pentesters)
        return result

    @property
    def all_shared_memories(self) -> list[SharedMemory]:
        """All team SharedMemory instances."""
        return [t.shared_memory for t in self.teams if t.shared_memory]

    @property
    def terminated_agents(self) -> set[str]:
        """All terminated agent IDs across teams."""
        result: set[str] = set()
        for team in self.teams:
            result.update(team._terminated_agents)
        return result
