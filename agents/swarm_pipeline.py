"""SwarmPipeline — runs N identical pen-tester agents concurrently.

Replaces the sequential recon → scan → analyze flow with a swarm of
identical PenTesterAgent instances that each have ALL tools and coordinate
via SharedMemory.

Flow:
  1. Discovery phase (unchanged — DiscoverToolWorker)
  2. Director selects programs (unchanged)
  3. Scope enrichment (unchanged)
  4. Create SharedMemory, seed with targets + scope
  5. Spawn N PenTesterAgents, run concurrently via asyncio.gather
  6. Monitor progress, enforce timeout/budget
  7. Collect deduplicated findings from SharedMemory
  8. Report phase (unchanged — ReportWorker)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from typing import TYPE_CHECKING, Any

from agents.llm_config import LLMConfigMap, create_llm_for_agent
from agents.messaging import TaskAssignment
from agents.pentester import PenTesterAgent
from agents.team import AgentTeam
from agents.validator import ValidatorAgent
from agents.pipeline import AgentPipeline
from memory.checkpoint import (
    PipelineCheckpoint,
    capture_session,
    capture_shared_memory,
    delete_checkpoint,
    generate_run_name,
    restore_session,
    restore_shared_memory,
    save_checkpoint,
)
from memory.shared import SharedMemory
from models.finding import Severity, VulnFinding, VulnType

if TYPE_CHECKING:
    from web.bridge import PipelineBridge

logger = logging.getLogger(__name__)


# Nudges the swarm toward BREADTH of vulnerability classes instead of every
# agent piling onto the first class that pays off (e.g. SQLi). Appended to the
# assignment brief every agent receives.
_COVERAGE_NUDGE = (
    "\n\nCOVERAGE — the team must collectively test a BREADTH of vulnerability "
    "classes, not converge on one. Across the swarm, deliberately cover: "
    "injection (SQLi, NoSQLi, command, XXE), XSS, broken access control "
    "(IDOR/BOLA), broken authentication, SSRF, CSRF, mass assignment, "
    "file inclusion / path traversal, security misconfiguration, and known CVEs "
    "/ outdated components. Before testing a class, read shared memory: if "
    "another agent already covers it on this target, PIVOT to an UNCOVERED "
    "class. Breadth of classes is rewarded; duplicating a covered class is not."
)


class SwarmPipeline(AgentPipeline):
    """Pipeline that runs N identical pen-tester agents concurrently."""

    def __init__(
        self,
        num_agents: int = 3,
        swarm_timeout_minutes: int = 30,
        llm_config: LLMConfigMap | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(llm_config=llm_config, **kwargs)
        self.num_agents = num_agents
        self.swarm_timeout_minutes = swarm_timeout_minutes
        self.shared_memory: SharedMemory | None = None
        self.pentesters: list[PenTesterAgent] = []
        self.validator: ValidatorAgent | None = None
        self._llm_config = llm_config or LLMConfigMap()
        self._ollama_pool = None
        self._ollama_urls: list[str] | None = None

        # Checkpoint state (set during _run_pipeline, used by _build_checkpoint)
        self._all_domains: list[str] = []
        self._assignment_context: str = ""
        self._selected_programs: list = []
        self._current_budget: float = 0.0
        self.effective_budget: float | None = None  # set via dashboard; overrides budget param
        self._team: AgentTeam | None = None
        self._terminated_agents: set[str] = set()

    # ------------------------------------------------------------------
    # Forward pause/resume/stop to team
    # ------------------------------------------------------------------

    def pause(self) -> None:
        super().pause()
        if self._team:
            self._team.pause()
        if getattr(self, "_group", None):
            self._group.pause()

    def resume(self) -> None:
        super().resume()
        if self._team:
            self._team.resume()
        if getattr(self, "_group", None):
            self._group.resume()

    def stop(self) -> None:
        super().stop()
        if self._team:
            self._team.stop()
        if getattr(self, "_group", None):
            self._group.stop()

    @property
    def effective_budget(self) -> float | None:
        return getattr(self, "_effective_budget", None)

    @effective_budget.setter
    def effective_budget(self, value: float | None) -> None:
        self._effective_budget = value
        group = getattr(self, "_group", None)
        team = getattr(self, "_team", None)
        teams = group.teams if group else ([team] if team else [])
        if group:
            group.effective_budget = value
        for current in teams:
            current.effective_budget = value
            for agent in getattr(current, "pentesters", []):
                agent.session_budget = value
        for agent in (getattr(self, "validator", None), getattr(group, "auditor", None)):
            if agent:
                agent.session_budget = value

    @property
    def _team_terminated(self) -> set[str]:
        """Terminated agents — delegates to team if available."""
        if self._team:
            return self._team._terminated_agents
        return self._terminated_agents

    # ------------------------------------------------------------------
    # Override the per-program processing
    # ------------------------------------------------------------------

    async def _run_pipeline(
        self,
        top_n: int,
        min_bounty: float,
        budget: float,
    ) -> None:
        """Swarm pipeline: discover → spawn N agents → collect findings."""
        learning_summary = self.learning.summary()
        if "No historical data" not in learning_summary:
            logger.info("\n%s", learning_summary)

        # ── Start Ollama pool early (needed for discovery worker too) ──
        worker_provider = self._llm_config.default_worker.provider
        if worker_provider == "ollama" and self.settings.ollama_per_agent:
            from utils.ollama_pool import OllamaPool
            self._ollama_pool = OllamaPool(
                num_instances=self.num_agents,
                model=self.settings.ollama_model,
            )
            self._ollama_urls = await self._ollama_pool.start()

        # ── Resolve targets (pluggable: direct / bug-bounty discovery / ──
        #    any other TargetProvider a profile chose) ──────────────────
        selected = await self.target_provider.get_targets(self, top_n, min_bounty)
        if selected is None:
            return

        # ── Scope enrichment ──────────────────────────────────────────
        if self.browser_pool or getattr(self, "scope_pdf_only", False):
            await self._enrich_scope(selected)

        # In PDF-only mode, drop programs that have no scope PDF loaded
        if getattr(self, "scope_pdf_only", False):
            from agents.pipeline import AgentPipeline
            has_pdf = set()
            for p in selected:
                if AgentPipeline._load_scope_pdf(p):
                    has_pdf.add(p.url)
            before = len(selected)
            selected = [p for p in selected if p.url in has_pdf]
            if len(selected) < before:
                dropped = before - len(selected)
                logger.info(
                    "scope-pdf-only: dropped %d programs without PDFs (kept %d: %s)",
                    dropped, len(selected),
                    ", ".join(p.name for p in selected),
                )
                self.session.programs = selected

        # ── Collect all domains to scan ───────────────────────────────
        all_domains: list[str] = []
        program_map: dict[str, Any] = {}  # domain → program
        for program in selected:
            for domain in program.all_domains:
                should_scan, reason = self.learning.should_scan_target(domain)
                if should_scan:
                    all_domains.append(domain)
                    program_map[domain] = program
                else:
                    logger.info("Skipping %s: %s", domain, reason)
                    self.session.mark_skipped(domain, reason)

        if not all_domains:
            logger.warning("No domains to scan after filtering.")
            self._print_conversation_log()
            return

        # Store on instance for checkpoint access
        self._all_domains = all_domains
        self._selected_programs = selected
        self._current_budget = budget
        if self.effective_budget is None:
            self.effective_budget = budget

        # ── Smart Domain Triage (optional, --domain-triage) ─────────
        domain_rankings = None
        if self.settings.domain_triage:
            self._emit_phase("triage")
            logger.info("=== Domain Triage: fingerprinting %d domains ===", len(all_domains))
            graph_triage_ctx = ""
            if self.graph:
                try:
                    graph_triage_ctx = await self.graph.build_scan_context(all_domains[:10])
                except Exception:
                    logger.debug("Failed to build graph triage context", exc_info=True)

            domain_rankings = await self.director.triage_domains(
                all_domains, graph_context=graph_triage_ctx, cost_tracker=self.cost_tracker,
            )

        # ── Phase 2: Swarm / Group ─────────────────────────────────────
        if getattr(self, "_group_mode", False):
            await self._run_group_phase(
                all_domains=all_domains,
                selected=selected,
                budget=budget,
                domain_rankings=domain_rankings,
                scan_id=None,  # will be created inside
            )
            return

        self._emit_phase("swarm")
        logger.info("=== Phase 2: Swarm (%d agents, %d targets) ===", self.num_agents, len(all_domains))

        self.shared_memory = SharedMemory(scoring=self.scoring, coordination=self.coordination)
        self.shared_memory.configure_mechanisms(self.mechanisms)

        # Quality gate: restrict findings to in-scope domains
        self.shared_memory.allowed_domains = {d.lower() for d in all_domains}

        # Quality gate: collect excluded vuln types from all selected programs
        excluded = set()
        for program in selected:
            if program.excluded_vuln_types:
                for vt in program.excluded_vuln_types:
                    excluded.add(vt.lower().strip().replace(" ", "_"))
        self.shared_memory.excluded_vuln_types = excluded or None

        # Wire incremental persistence — save findings as they come in
        self.shared_memory.on_finding = self._persist_finding

        # Seed shared memory with targets and scope
        for domain in all_domains:
            await self.shared_memory.post(
                "pipeline", "discovery", "target",
                f"target:{domain}", json.dumps({"domain": domain}),
            )
            program = program_map.get(domain)
            if program and program.scope_raw_text:
                await self.shared_memory.post(
                    "pipeline", "note", "scope",
                    f"scope:{domain}",
                    program.scope_raw_text,
                )

        # Record scan (store on instance for incremental persistence callback)
        self._current_scan_id: int | None = None
        scan_id = self.persistent.start_scan(
            target=",".join(all_domains[:10]),
            metadata={"domain_count": len(all_domains), "mode": "swarm", "agents": self.num_agents},
        )
        self._current_scan_id = scan_id
        self._run_name = generate_run_name(scan_id)
        logger.info("Run identifier: %s (scan #%d)", self._run_name, scan_id)
        print(f"\n  Run: {self._run_name}\n")

        # Ollama parallelism warning (when not using --ollama-per-agent)
        if self._llm_config.default_worker.provider == "ollama" and not self._ollama_urls:
            num_parallel = os.environ.get("OLLAMA_NUM_PARALLEL", "")
            try:
                np = int(num_parallel) if num_parallel else 1
            except ValueError:
                np = 1
            if np < self.num_agents:
                logger.warning(
                    "Ollama OLLAMA_NUM_PARALLEL=%s but swarm has %d agents. "
                    "Set OLLAMA_NUM_PARALLEL=%d for parallel inference, "
                    "or use --ollama-per-agent to auto-spawn instances. "
                    "Run: OLLAMA_NUM_PARALLEL=%d ollama serve",
                    num_parallel or "1", self.num_agents,
                    self.num_agents, self.num_agents,
                )

        # ── Create AgentTeam ────────────────────────────────────────
        team = AgentTeam(
            team_id="alpha",
            num_agents=self.num_agents,
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
        team.shared_memory = self.shared_memory
        team.browser_pool = self.browser_pool
        team.web_bridge = self.web_bridge
        team._ollama_urls = self._ollama_urls
        team._all_domains = all_domains
        team.effective_budget = self.effective_budget
        team.on_periodic = lambda: self._save_checkpoint_now("swarm")
        self._team = team

        # Spawn pen-tester agents
        keep_recent = getattr(self, "_keep_recent", None) or self.settings.keep_recent
        await team.spawn_agents(keep_recent=keep_recent)
        self.pentesters = team.pentesters  # backward compat for bridge/checkpoint

        # Build enriched assignment context (full scope, past findings, graph, learning)
        assignment_context = await self._build_assignment_context(
            all_domains, selected, domain_rankings,
        )
        self._assignment_context = assignment_context

        # Save checkpoint after discovery (before swarm starts)
        self._save_checkpoint_now("swarm")

        # Start recorder agent (watches SharedMemory → graph)
        self.recorder = None
        if self.graph and self.shared_memory:
            from agents.recorder import Recorder
            # Recorder uses director's LLM (stronger model for reliable JSON extraction)
            recorder_llm = self.director.llm if (self.director and self.director.llm and not self.dry_run) else None
            broadcast_fn = self.web_bridge._broadcast if self.web_bridge else None
            self.recorder = Recorder(
                self.shared_memory, self.graph,
                llm=recorder_llm,
                broadcast=broadcast_fn,
                cost_tracker=self.cost_tracker,
            )
            self.recorder.start()

        # Spawn validator agent (wired into bridge/UI but NOT started yet —
        # the team monitor will launch it when the first finding appears)
        await self._spawn_validator()
        self._validator_task: asyncio.Task | None = None

        # Wire validator lazy-start via team callback
        async def _launch_validator():
            logger.info("First finding submitted — launching validator agent")
            current_n = self.shared_memory.findings_count if self.shared_memory else 0
            task = asyncio.create_task(
                self.validator.execute(TaskAssignment(
                    agent_name=self.validator.agent_id,
                    task_type="validate",
                    parameters={},
                    context=(
                        f"There are {current_n} finding(s) in shared memory. "
                        "Use read_discoveries to see them, then independently verify "
                        "each one with attack tools. Call verify_finding with your result. "
                        "Keep checking for new findings as pentesters submit them."
                    ),
                )),
                name=self.validator.agent_id,
            )
            self._validator_task = task
            team._extra_agents[self.validator.agent_id] = self.validator
            return task

        team.on_first_finding = _launch_validator
        team._assignment_context = assignment_context

        # Install SIGINT handler for graceful shutdown on first Ctrl+C
        original_handler = signal.getsignal(signal.SIGINT)
        sigint_count = 0

        def _sigint_handler(sig, frame):
            nonlocal sigint_count
            sigint_count += 1
            if sigint_count == 1:
                active = [t for t in team._running_tasks if not t.done() and t.get_name() != "swarm-monitor"]
                findings_n = self.shared_memory.findings_count if self.shared_memory else 0
                run_label = getattr(self, "_run_name", "")
                print(f"\n  Stopping {len(active)} agent(s)... (Ctrl+C again to force)")
                print(f"  Checkpoint: {run_label} | {findings_n} findings so far")
                self._stop_flag = True
                team.stop()
                self._save_checkpoint_now("swarm")
            else:
                # Second Ctrl+C — force cancel all tasks
                print(f"\n  Force stopping...")
                team.cancel_all()

        signal.signal(signal.SIGINT, _sigint_handler)

        try:
            findings_from_team = await team.run(
                targets=all_domains,
                assignment_context=assignment_context,
                budget=budget,
            )
        except Exception as exc:
            logger.exception("Swarm execution error — saving checkpoint")
            team.run_errors.append(str(exc))
            self._save_checkpoint_now("swarm")
        finally:
            signal.signal(signal.SIGINT, original_handler)

        # Stop recorder and wait for final flush before graph closes
        if self.recorder:
            self.recorder.stop()
            if self.recorder._task:
                try:
                    await self.recorder._task
                except (asyncio.CancelledError, Exception):
                    pass
            rec = self.recorder.summary()
            logger.info("Recorder: stored %d, processed %d, errors %d",
                        rec["stored_count"], rec["processed"], rec["graph_errors"] + rec["llm_empty"])

        # ── Collect findings ──────────────────────────────────────────
        findings = self.shared_memory.get_findings()
        logger.info("=== Swarm Complete: %d deduplicated findings ===", len(findings))

        # Also collect any findings returned directly by agents
        for agent in self.pentesters:
            if agent.last_trace and agent.last_trace.final_answer:
                parsed = agent._parse_final_answer(agent.last_trace.final_answer)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, VulnFinding):
                            accepted, reason = await self.shared_memory.submit_finding(agent.agent_id, item)
                            if reason:
                                logger.info("Rejected final-answer finding from %s: %s", agent.agent_id, reason)

        if self.shared_memory.mechanisms:
            from memory.mechanisms import apply_run_end
            apply_run_end([self.shared_memory])

        findings = self.shared_memory.get_findings()
        self.session.raw_findings.extend(findings)
        self.session.analyzed_findings.extend(findings)

        # Mark domains as scanned
        for domain in all_domains:
            self.session.mark_scanned(domain)

        # Findings already persisted incrementally via _persist_finding callback

        # Store per-domain scan metadata in graph
        if self.graph:
            per_domain_costs = self.cost_tracker.get_per_target_costs()
            for domain in all_domains:
                domain_findings = [f for f in findings if domain in f.target]
                domain_cost = per_domain_costs.get(domain, 0.0)
                await self.graph.store_scan(scan_id, domain, len(domain_findings), domain_cost)

        self.persistent.complete_scan(scan_id, len(findings), self.cost_tracker.get_total())

        # Update program stats
        for program in selected:
            program_findings = [
                f for f in findings
                if any(d in f.target for d in program.all_domains)
            ]
            self.persistent.update_program_stats(program.url, findings=len(program_findings))

        # On Ctrl+C: prompt user for next action
        if self._stop_flag:
            self._print_shutdown_summary()
            action = self._prompt_shutdown_action()
            if action == "exit":
                self._print_conversation_log()
                return
            # action == "debrief" — fall through to debrief, skip chain+report

        # ── Attack Chain Synthesis (skip on stop) ─────────────────────
        if not self._stop_flag and findings and len(findings) >= 2:
            logger.info("=== Chain Synthesis: analyzing %d findings for attack chains ===", len(findings))
            discoveries = self.shared_memory.read_all() if self.shared_memory else []

            # Query graph for historical chain patterns
            graph_context = ""
            if self.graph:
                try:
                    graph_context = await self.graph.build_chain_context(all_domains[:10])
                    if graph_context:
                        logger.info("Chain synthesis enriched with graph context (%d chars)", len(graph_context))
                except Exception:
                    logger.debug("Failed to build graph chain context", exc_info=True)

            chains = await self.director.synthesize_attack_chains(
                findings=findings,
                discoveries=discoveries,
                cost_tracker=self.cost_tracker,
                graph_context=graph_context,
            )
            if chains:
                logger.info("=== Chain Synthesis: %d attack chains identified ===", len(chains))
                for chain in chains:
                    # Build evidence referencing component findings
                    component_refs = []
                    for idx in chain.get("chain_components", []):
                        if 0 <= idx < len(findings):
                            f = findings[idx]
                            component_refs.append(f"- [{f.severity.value.upper()}] {f.title or f.vuln_type.value} on {f.target}")

                    chain_evidence = (
                        chain.get("evidence", "") +
                        "\n\nComponent findings:\n" + "\n".join(component_refs)
                    )

                    chain_finding = VulnFinding(
                        target=chain.get("target", findings[0].target),
                        vuln_type=VulnType.ATTACK_CHAIN,
                        severity=Severity(chain["severity"]),
                        title=chain["title"],
                        description=chain.get("description", ""),
                        evidence=chain_evidence,
                        url=findings[chain["chain_components"][0]].url if chain["chain_components"] else "",
                        confidence_score=0.8,
                        estimated_bounty=chain.get("estimated_bounty", 0),
                        ai_bounty_estimate=chain.get("estimated_bounty", 0),
                        ai_severity=Severity(chain["severity"]),
                        ai_exploitability="Chain attack — requires combining multiple vulnerabilities",
                    )
                    self.session.analyzed_findings.append(chain_finding)

                    # Persist the chain finding
                    try:
                        self.persistent.save_finding(scan_id, chain_finding.model_dump(mode="json"))
                    except Exception:
                        logger.debug("Failed to persist chain finding", exc_info=True)

                    logger.info(
                        "Chain: [%s] %s → ~$%.0f",
                        chain["severity"].upper(), chain["title"],
                        chain.get("estimated_bounty", 0),
                    )
            else:
                logger.info("Chain Synthesis: no viable chains found")

        # ── Debrief: Director compiles summary ─────────────────────────
        self._emit_phase("debrief")
        logger.info("=== Phase 3: Director Debrief ===")

        agent_traces = []
        for agent in self.pentesters:
            trace = agent.last_trace
            agent_traces.append({
                "agent": agent.agent_id,
                "iterations": trace.iterations if trace else 0,
                "tool_calls": len(trace.tool_calls) if trace else 0,
                "exit_reason": trace.exit_reason if trace else "unknown",
            })

        debrief = await self.director.debrief_swarm(
            shared_memory=self.shared_memory,
            agent_traces=agent_traces,
            targets=all_domains,
            findings_count=len(findings),
            cost_spent=self.cost_tracker.get_total(),
            cost_budget=self.effective_budget or budget,
            cost_tracker=self.cost_tracker,
        )

        logger.info("\n── Director Debrief ──\n%s", debrief)

        # Broadcast debrief to dashboard
        from agents.messaging import AgentMessage
        self.bus.send(AgentMessage(
            sender="director",
            recipient="all",
            msg_type="debrief",
            content=debrief,
            data={"findings_count": len(findings), "agent_count": len(self.pentesters)},
        ))

        # Human checkpoint before reporting (skip on stop)
        if not self._stop_flag and findings and not await self._checkpoint("analyze", findings):
            self._print_conversation_log()
            return

        # ── Phase 4: Report (skip on stop) ───────────────────────────
        if not self._stop_flag and findings:
            self._emit_phase("report")
            logger.info("── Report: %d findings ──", len(findings))
            await self.reporter.execute(
                TaskAssignment(
                    agent_name="reporter",
                    task_type="report",
                    parameters={
                        "findings": findings,
                        "estimated_costs": self.cost_tracker.get_per_target_costs(),
                    },
                    context=f"Generate reports for {len(findings)} findings from swarm scan",
                )
            )

        # ── Cleanup Ollama pool ────────────────────────────────────
        if self._ollama_pool is not None:
            self._ollama_pool.stop()
            self._ollama_pool = None
            logger.info("Ollama pool stopped")

        # ── Cleanup checkpoint on successful completion ──────────────
        if not self._stop_flag and scan_id:
            try:
                delete_checkpoint(self.persistent, scan_id)
            except Exception:
                logger.debug("Failed to delete checkpoint", exc_info=True)

        # ── Summary ──────────────────────────────────────────────────
        self.session.cost_snapshot = self.cost_tracker.get_total()
        logger.info("\n=== Swarm Pipeline Complete ===")
        logger.info("Total findings: %d", len(findings))
        logger.info("Total cost: $%.2f", self.cost_tracker.get_total())
        logger.info("Shared memory: %s", self.shared_memory.summary())
        logger.info("Session: %s", self.session.summary())

        self._print_conversation_log()

    # ------------------------------------------------------------------
    # Validator spawning (stays on pipeline — auditor is transparent to team)
    # ------------------------------------------------------------------

    async def _spawn_validator(self) -> None:
        """Create the ValidatorAgent with its own LLM and browser session."""
        agent_id = "validator"
        cli_mode = self.settings.cli_tool_mode
        help_cb = self.web_bridge.request_human_help if self.web_bridge else None

        llm = None if self.dry_run else create_llm_for_agent("validator", self._llm_config)

        browser_session = None
        if self.browser_pool:
            try:
                browser_session = await self.browser_pool.create_session(agent_id)
            except Exception:
                logger.debug("Could not create browser session for validator")

        agent = ValidatorAgent(
            agent_id=agent_id,
            bus=self.bus,
            llm=llm,
            shared_memory=self.shared_memory,
            browser_session=browser_session,
            help_callback=help_cb,
            cli_mode=cli_mode,
            cost_tracker=self.cost_tracker,
        )
        agent.session_budget = getattr(self, "effective_budget", None)
        agent._pause_event = self._pause_event
        if self.director and self.director.llm:
            agent.compress_llm = self.director.llm
        agent._allowed_domains = set(self._all_domains) if self._all_domains else None

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

        self.validator = agent
        logger.info("Spawned validator agent")

    # ------------------------------------------------------------------
    # Incremental persistence
    # ------------------------------------------------------------------

    async def _persist_finding(self, agent_id: str, finding: VulnFinding) -> None:
        """Save a finding to SQLite + graph as soon as it's submitted."""
        scan_id = self._current_scan_id
        try:
            finding_data = finding.model_dump(mode="json")
            row_id = self.persistent.save_finding(scan_id, finding_data)
            finding.id = row_id  # Use the real SQLite auto-increment ID
            logger.info("Persisted finding #%d from %s: %s", row_id, agent_id, finding.title or finding.vuln_type)
            # Keep the scan's cost/findings_count live so spend survives an
            # interrupted run (don't wait for complete_scan at the very end).
            self.persistent.update_scan_progress(
                scan_id, self.cost_tracker.get_total(),
                self.shared_memory.findings_count if self.shared_memory else None,
            )
        except Exception:
            logger.debug("Failed to persist finding to SQLite", exc_info=True)

        if self.graph:
            try:
                stored = await self.graph.store_finding(finding_data, scan_id=scan_id)
                if not stored:
                    logger.warning("Failed to store finding in graph: %s", finding.title or finding.vuln_type)
            except Exception:
                logger.warning("Failed to persist finding to graph", exc_info=True)

    # ------------------------------------------------------------------
    # Group mode (multi-team)
    # ------------------------------------------------------------------

    async def _run_group_phase(
        self,
        all_domains: list[str],
        selected: list,
        budget: float,
        domain_rankings: list | None,
        scan_id: int | None,
    ) -> None:
        """Run multi-team group phase using AgentGroup."""
        from agents.group import AgentGroup

        num_teams = getattr(self, "_num_teams", 2)
        self._emit_phase("swarm")
        logger.info(
            "=== Phase 2: Group (%d teams × %d agents, %d targets) ===",
            num_teams, self.num_agents, len(all_domains),
        )

        self._all_domains = all_domains
        self._selected_programs = selected
        self._current_budget = budget
        if self.effective_budget is None:
            self.effective_budget = budget

        # Record scan
        self._current_scan_id = self.persistent.start_scan(
            target=",".join(all_domains[:10]),
            metadata={
                "domain_count": len(all_domains),
                "mode": "swarm",
                "teams": num_teams,
                "agents_per_team": self.num_agents,
            },
        )
        scan_id = self._current_scan_id
        self._run_name = generate_run_name(scan_id)
        logger.info("Run identifier: %s (scan #%d)", self._run_name, scan_id)
        print(f"\n  Run: {self._run_name} (group: {num_teams} teams)\n")

        # Build assignment context
        assignment_context = await self._build_assignment_context(
            all_domains, selected, domain_rankings,
        )
        self._assignment_context = assignment_context

        # Build scope info for team SharedMemory seeding
        scope_info: dict[str, str] = {}
        for program in selected:
            if program.scope_raw_text:
                for domain in program.all_domains:
                    scope_info[domain] = program.scope_raw_text[:2000]

        # Create group
        group = AgentGroup(
            num_teams=num_teams,
            agents_per_team=self.num_agents,
            director=self.director,
            bus=self.bus,
            cost_tracker=self.cost_tracker,
            persistent=self.persistent,
            graph=self.graph,
            llm_config=self._llm_config,
            dry_run=self.dry_run,
            settings=self.settings,
            methodology=self.methodology,
            scoring=self.scoring,
            mechanisms=self.mechanisms,
            coordination=self.coordination,
        )
        group.browser_pool = self.browser_pool
        group.web_bridge = self.web_bridge
        group._ollama_urls = self._ollama_urls
        group.effective_budget = self.effective_budget
        group.on_periodic = lambda: self._save_checkpoint_now("swarm")
        self._group = group
        self._group_mode = True  # tells the web bridge to pull team/agent state from the group

        # Create teams + spawn agents
        group.create_teams()
        await group.setup_teams(
            all_domains=all_domains,
            scope_info=scope_info,
            finding_callback=self._persist_finding,
        )

        # Set quality gates on each team's SharedMemory
        excluded = set()
        for program in selected:
            if program.excluded_vuln_types:
                for vt in program.excluded_vuln_types:
                    excluded.add(vt.lower().strip().replace(" ", "_"))
        for team in group.teams:
            if team.shared_memory:
                team.shared_memory.excluded_vuln_types = excluded or None

        # Expose first team's shared memory for bridge/checkpoint compat
        if group.teams:
            self.shared_memory = group.teams[0].shared_memory
        self.pentesters = group.all_pentesters

        # Spawn cross-team auditor
        await group.spawn_auditor(pause_event=self._pause_event)

        # Start recorder (uses first team's shared memory — TODO: multi-team recorder)
        self.recorder = None
        if self.graph and self.shared_memory:
            from agents.recorder import Recorder
            recorder_llm = self.director.llm if (self.director and self.director.llm and not self.dry_run) else None
            broadcast_fn = self.web_bridge._broadcast if self.web_bridge else None
            self.recorder = Recorder(
                self.shared_memory, self.graph,
                llm=recorder_llm,
                broadcast=broadcast_fn,
                cost_tracker=self.cost_tracker,
            )
            self.recorder.start()

        # Save checkpoint before execution
        self._save_checkpoint_now("swarm")

        # SIGINT handler
        original_handler = signal.getsignal(signal.SIGINT)
        sigint_count = 0

        def _sigint_handler(sig, frame):
            nonlocal sigint_count
            sigint_count += 1
            if sigint_count == 1:
                total_findings = sum(
                    t.shared_memory.findings_count for t in group.teams
                    if t.shared_memory
                )
                run_label = getattr(self, "_run_name", "")
                print(f"\n  Stopping {num_teams} teams... (Ctrl+C again to force)")
                print(f"  Checkpoint: {run_label} | {total_findings} findings so far")
                self._stop_flag = True
                group.stop()
                self._save_checkpoint_now("swarm")
            else:
                print(f"\n  Force stopping...")
                group.cancel_all()

        signal.signal(signal.SIGINT, _sigint_handler)

        try:
            findings = await group.run(
                targets=all_domains,
                assignment_context=assignment_context,
                budget=budget,
            )
        except Exception as exc:
            logger.exception("Group execution error — saving checkpoint")
            group.run_errors.append(str(exc))
            self._save_checkpoint_now("swarm")
            findings = group._collect_all_findings()
        finally:
            signal.signal(signal.SIGINT, original_handler)

        # Stop recorder
        if self.recorder:
            self.recorder.stop()
            if self.recorder._task:
                try:
                    await self.recorder._task
                except (asyncio.CancelledError, Exception):
                    pass
            rec = self.recorder.summary()
            logger.info("Recorder: stored %d, processed %d, errors %d",
                        rec["stored_count"], rec["processed"], rec["graph_errors"] + rec["llm_empty"])

        # ── Collect findings ──────────────────────────────────────────
        logger.info("=== Group Complete: %d deduplicated findings across %d teams ===",
                     len(findings), num_teams)

        team_memories = [t.shared_memory for t in group.teams if t.shared_memory]
        if any(m.mechanisms for m in team_memories):
            from memory.mechanisms import apply_run_end
            apply_run_end(team_memories)

        # Log per-team stats
        for team in group.teams:
            team_n = team.shared_memory.findings_count if team.shared_memory else 0
            logger.info("  Team %s: %d findings", team.team_id, team_n)

        self.session.raw_findings.extend(findings)
        self.session.analyzed_findings.extend(findings)
        for domain in all_domains:
            self.session.mark_scanned(domain)

        # Store per-domain scan metadata in graph
        if self.graph:
            per_domain_costs = self.cost_tracker.get_per_target_costs()
            for domain in all_domains:
                domain_findings = [f for f in findings if domain in f.target]
                domain_cost = per_domain_costs.get(domain, 0.0)
                await self.graph.store_scan(scan_id, domain, len(domain_findings), domain_cost)

        self.persistent.complete_scan(scan_id, len(findings), self.cost_tracker.get_total())

        for program in selected:
            program_findings = [
                f for f in findings
                if any(d in f.target for d in program.all_domains)
            ]
            self.persistent.update_program_stats(program.url, findings=len(program_findings))

        if self._stop_flag:
            self._print_shutdown_summary()
            action = self._prompt_shutdown_action()
            if action == "exit":
                self._print_conversation_log()
                return

        # ── Attack Chain Synthesis ─────────────────────────────────────
        if not self._stop_flag and findings and len(findings) >= 2:
            logger.info("=== Chain Synthesis: %d findings ===", len(findings))
            discoveries = []
            for team in group.teams:
                if team.shared_memory:
                    discoveries.extend(team.shared_memory.read_all())

            graph_context = ""
            if self.graph:
                try:
                    graph_context = await self.graph.build_chain_context(all_domains[:10])
                except Exception:
                    logger.debug("Failed to build graph chain context", exc_info=True)

            chains = await self.director.synthesize_attack_chains(
                findings=findings,
                discoveries=discoveries,
                cost_tracker=self.cost_tracker,
                graph_context=graph_context,
            )
            if chains:
                for chain in chains:
                    component_refs = []
                    for idx in chain.get("chain_components", []):
                        if 0 <= idx < len(findings):
                            f = findings[idx]
                            component_refs.append(
                                f"- [{f.severity.value.upper()}] {f.title or f.vuln_type.value} on {f.target}"
                            )
                    chain_evidence = (
                        chain.get("evidence", "") +
                        "\n\nComponent findings:\n" + "\n".join(component_refs)
                    )
                    chain_finding = VulnFinding(
                        target=chain.get("target", findings[0].target),
                        vuln_type=VulnType.ATTACK_CHAIN,
                        severity=Severity(chain["severity"]),
                        title=chain["title"],
                        description=chain.get("description", ""),
                        evidence=chain_evidence,
                        url=findings[chain["chain_components"][0]].url if chain["chain_components"] else "",
                        confidence_score=0.8,
                        estimated_bounty=chain.get("estimated_bounty", 0),
                        ai_bounty_estimate=chain.get("estimated_bounty", 0),
                        ai_severity=Severity(chain["severity"]),
                        ai_exploitability="Chain attack — requires combining multiple vulnerabilities",
                    )
                    self.session.analyzed_findings.append(chain_finding)
                    try:
                        self.persistent.save_finding(scan_id, chain_finding.model_dump(mode="json"))
                    except Exception:
                        logger.debug("Failed to persist chain finding", exc_info=True)

        # ── Debrief ────────────────────────────────────────────────────
        self._emit_phase("debrief")
        logger.info("=== Phase 3: Director Debrief (Group) ===")

        # Aggregate all agent traces across teams
        agent_traces = []
        for team in group.teams:
            for agent in team.pentesters:
                trace = agent.last_trace
                agent_traces.append({
                    "agent": f"{team.team_id}/{agent.agent_id}",
                    "team": team.team_id,
                    "iterations": trace.iterations if trace else 0,
                    "tool_calls": len(trace.tool_calls) if trace else 0,
                    "exit_reason": trace.exit_reason if trace else "unknown",
                })

        # Use first team's shared memory for debrief (Director sees aggregated data)
        debrief_sm = group.teams[0].shared_memory if group.teams else self.shared_memory
        debrief = await self.director.debrief_swarm(
            shared_memory=debrief_sm,
            agent_traces=agent_traces,
            targets=all_domains,
            findings_count=len(findings),
            cost_spent=self.cost_tracker.get_total(),
            cost_budget=self.effective_budget or budget,
            cost_tracker=self.cost_tracker,
        )

        logger.info("\n── Director Debrief ──\n%s", debrief)
        from agents.messaging import AgentMessage
        self.bus.send(AgentMessage(
            sender="director",
            recipient="all",
            msg_type="debrief",
            content=debrief,
            data={"findings_count": len(findings), "agent_count": len(group.all_pentesters)},
        ))

        # ── Report (skip on stop) ────────────────────────────────────
        if not self._stop_flag and findings:
            self._emit_phase("report")
            logger.info("── Report: %d findings ──", len(findings))
            await self.reporter.execute(
                TaskAssignment(
                    agent_name="reporter",
                    task_type="report",
                    parameters={
                        "findings": findings,
                        "estimated_costs": self.cost_tracker.get_per_target_costs(),
                    },
                    context=f"Generate reports for {len(findings)} findings from group scan ({num_teams} teams)",
                )
            )

        # Cleanup
        if self._ollama_pool is not None:
            self._ollama_pool.stop()
            self._ollama_pool = None

        if not self._stop_flag and scan_id:
            try:
                delete_checkpoint(self.persistent, scan_id)
            except Exception:
                logger.debug("Failed to delete checkpoint", exc_info=True)

        self.session.cost_snapshot = self.cost_tracker.get_total()
        logger.info("\n=== Group Pipeline Complete ===")
        logger.info("Total findings: %d (across %d teams)", len(findings), num_teams)
        logger.info("Total cost: $%.2f", self.cost_tracker.get_total())
        self._print_conversation_log()

    async def _build_assignment_context(
        self,
        all_domains: list[str],
        selected: list,
        domain_rankings: list | None,
    ) -> str:
        """Build enriched assignment context string for agents.

        Takes advantage of large context windows (262K+ for Qwen3.5-397B) to
        inject full scope, past findings, graph intelligence, and learning
        insights so agents start with maximum knowledge.
        """
        # ── 1. Target domains (ranked or flat) ─────────────────────────
        if domain_rankings:
            high = [d for d in domain_rankings if d["priority"] == "high"]
            medium = [d for d in domain_rankings if d["priority"] == "medium"]
            low = [d for d in domain_rankings if d["priority"] == "low"]

            domain_sections = []
            if high:
                domain_sections.append("HIGH PRIORITY (attack these first):\n" + "\n".join(
                    f"  - {d['domain']}: {d['notes']}" for d in high
                ))
            if medium:
                domain_sections.append("MEDIUM PRIORITY:\n" + "\n".join(
                    f"  - {d['domain']}: {d['notes']}" for d in medium
                ))
            if low:
                domain_sections.append("LOW PRIORITY (skip unless high-priority exhausted):\n" + "\n".join(
                    f"  - {d['domain']}: {d['notes']}" for d in low[:10]
                ) + (f"\n  ... +{len(low)-10} more" if len(low) > 10 else ""))

            context = (
                "=== TARGET DOMAINS (ranked by attack potential) ===\n"
                + "\n\n".join(domain_sections)
                + f"\n\nYou are one of {self.num_agents} concurrent pen-testers. "
                "Use shared memory to coordinate. Claim targets before scanning them. "
                "Focus on HIGH PRIORITY targets first."
                + _COVERAGE_NUDGE
            )
        else:
            context = (
                f"Targets: {', '.join(all_domains[:50])}"
                + (f" ... and {len(all_domains) - 50} more" if len(all_domains) > 50 else "")
                + f"\n\nYou are one of {self.num_agents} concurrent pen-testers. "
                "Use shared memory to coordinate. Claim targets before scanning them."
                + _COVERAGE_NUDGE
            )

        # ── 2. Full scope (no truncation — use that 262K context) ──────
        scope_snippets = []
        for program in selected:
            prog_parts = []
            prog_parts.append(f"### {program.name}")
            if getattr(program, "platform", None):
                prog_parts.append(f"Platform: {program.platform}")
            if getattr(program, "max_bounty", None):
                prog_parts.append(f"Max bounty: ${program.max_bounty}")
            if program.scope_raw_text:
                prog_parts.append(program.scope_raw_text)  # full scope, not truncated
            if getattr(program, "in_scope", None):
                in_scope_str = ", ".join(str(s) for s in program.in_scope[:50])
                prog_parts.append(f"In scope assets: {in_scope_str}")
            if program.out_of_scope:
                prog_parts.append(f"Out of scope: {', '.join(program.out_of_scope)}")
            if program.excluded_vuln_types:
                prog_parts.append(f"Excluded vuln types: {', '.join(program.excluded_vuln_types)}")
            scope_snippets.append("\n".join(prog_parts))
        if scope_snippets:
            context += "\n\n=== SCOPE & PROGRAM DETAILS ===\n" + "\n\n".join(scope_snippets) + "\n=== END SCOPE ==="

        # ── 3. Past findings from SQLite (avoid rediscovering known vulns) ──
        if self.persistent:
            past_sections = []
            for domain in all_domains[:20]:
                past = self.persistent.get_past_findings(target=domain, limit=20)
                if past:
                    lines = []
                    for f in past:
                        status = "ACCEPTED" if f.get("was_accepted") else ("submitted" if f.get("was_submitted") else "found")
                        bounty = f.get("actual_bounty", 0)
                        bounty_str = f" — ${bounty}" if bounty else ""
                        lines.append(
                            f"  - [{status}] {f.get('vuln_type', '?')} "
                            f"({f.get('severity', '?')}): {f.get('title', 'untitled')}{bounty_str}"
                        )
                    past_sections.append(f"{domain}:\n" + "\n".join(lines))

            if past_sections:
                context += (
                    "\n\n=== PAST FINDINGS (from prior scans — avoid duplicates, build on these) ===\n"
                    + "\n\n".join(past_sections)
                    + "\n=== END PAST FINDINGS ==="
                )

        # ── 4. Graph intelligence (similar targets, effective techniques) ──
        if self.graph:
            try:
                graph_ctx = await self.graph.build_scan_context(all_domains[:10])
                if graph_ctx:
                    context += "\n\n" + graph_ctx
            except Exception:
                logger.debug("Failed to build graph scan context", exc_info=True)

        # ── 5. Learning insights (best vuln types from historical ROI) ──
        if self.learning:
            try:
                best_types = self.learning.get_best_vuln_types(top_n=10)
                if best_types:
                    context += (
                        "\n\n=== HISTORICAL INSIGHTS (what works best) ===\n"
                        f"Top vuln types by ROI: {', '.join(best_types)}\n"
                    )
                    # Per-target history for assigned domains
                    target_hints = []
                    for domain in all_domains[:10]:
                        hist = self.learning.get_target_history(domain)
                        if hist.get("scan_count", 0) > 0:
                            target_hints.append(
                                f"  {domain}: {hist['scan_count']} prior scans, "
                                f"{hist['total_findings']} findings ({hist['accepted_findings']} accepted), "
                                f"types: {', '.join(hist['finding_types'][:5]) or 'none'}"
                            )
                    if target_hints:
                        context += "Per-target history:\n" + "\n".join(target_hints) + "\n"
                    context += "=== END INSIGHTS ==="
            except Exception:
                logger.debug("Failed to build learning insights", exc_info=True)

        return context

    # ------------------------------------------------------------------
    # Shutdown helpers
    # ------------------------------------------------------------------

    def _print_shutdown_summary(self) -> None:
        """Print a clean shutdown summary to the terminal."""
        findings_n = self.shared_memory.findings_count if self.shared_memory else 0
        if getattr(self, "_terminal_controller", None):
            return "exit"  # terminal controller owns stdin; never call blocking input()
        entries_n = self.shared_memory.entry_count if self.shared_memory else 0
        run_label = getattr(self, "_run_name", "")
        cost = self.cost_tracker.get_total()

        # Recorder stats
        rec_line = ""
        if self.recorder:
            rec = self.recorder.summary()
            rec_line = f"  Graph:    {rec['stored_count']} stored, {rec['processed']} processed"
            if rec["graph_errors"]:
                rec_line += f", {rec['graph_errors']} errors"

        print(f"\n  Shutdown complete.")
        print(f"  Run:      {run_label}")
        print(f"  Findings: {findings_n}")
        print(f"  Entries:  {entries_n}")
        print(f"  Cost:     ${cost:.2f}")
        if rec_line:
            print(rec_line)
        print(f"\n  Resume:   python main.py full --resume-scan {run_label}\n")

    def _prompt_shutdown_action(self) -> str:
        """Ask the user what to do after Ctrl+C stop.

        Returns "debrief" or "exit".
        """
        if getattr(self, "_terminal_controller", None):
            return "exit"  # terminal controller owns stdin; never call blocking input()
        entries_n = self.shared_memory.entry_count if self.shared_memory else 0
        findings_n = self.shared_memory.findings_count if self.shared_memory else 0
        print("  What would you like to do?")
        print(f"    [d] Debrief — director summarizes {entries_n} entries & {findings_n} findings")
        print(f"    [x] Exit    — save checkpoint and quit")
        print()
        try:
            choice = input("  Choice [d/x]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "exit"
        if choice in ("x", "exit", "q", "quit"):
            return "exit"
        return "debrief"

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    def _build_checkpoint(self, phase: str) -> PipelineCheckpoint:
        """Capture current pipeline state into a checkpoint."""
        scan_id = getattr(self, "_current_scan_id", None) or 0
        direct_targets = getattr(self, "_direct_targets", None)
        direct_program_url = getattr(self, "_direct_program_url", None)

        sm_snapshot = {}
        if self.shared_memory:
            sm_snapshot = capture_shared_memory(self.shared_memory)

        return PipelineCheckpoint(
            scan_id=scan_id,
            created_at=time.time(),
            pipeline_mode="swarm",
            phase=phase,
            phase_completed=self._completed_phases(),
            run_name=getattr(self, "_run_name", ""),
            num_agents=self.num_agents,
            swarm_timeout_minutes=self.swarm_timeout_minutes,
            budget=self._current_budget,
            selected_programs=[p.model_dump() for p in self._selected_programs],
            all_domains=list(self._all_domains),
            assignment_context=self._assignment_context,
            shared_memory=sm_snapshot,
            session=capture_session(self.session),
            cost_spent=self.cost_tracker.get_total(),
            direct_targets=list(direct_targets) if direct_targets else None,
            direct_program_url=direct_program_url,
        )

    def _completed_phases(self) -> list[str]:
        """Infer which phases have completed based on current state."""
        phases = []
        if self._selected_programs:
            phases.append("discover")
        if self.shared_memory and self.shared_memory.entry_count > 0:
            phases.append("swarm")
        return phases

    def _save_checkpoint_now(self, phase: str) -> None:
        """Build and persist a checkpoint (synchronous, safe to call anywhere)."""
        try:
            checkpoint = self._build_checkpoint(phase)
            save_checkpoint(self.persistent, checkpoint)
            # Flush live cost to the scans row on the same periodic beat, so
            # spend is durable within one checkpoint interval even if no new
            # findings land and the run is later killed (e.g. on timeout).
            self.persistent.update_scan_progress(
                self._current_scan_id, self.cost_tracker.get_total()
            )
        except Exception:
            logger.debug("Failed to save checkpoint", exc_info=True)

    async def _resume_from_checkpoint(self, checkpoint: PipelineCheckpoint, budget: float) -> None:
        """Resume a swarm pipeline from a saved checkpoint."""
        from models.program import BountyProgram

        logger.info(
            "=== Resuming from checkpoint: scan=%d phase=%s domains=%d findings=%d cost=$%.2f ===",
            checkpoint.scan_id, checkpoint.phase,
            len(checkpoint.all_domains),
            len(checkpoint.shared_memory.get("findings", [])),
            checkpoint.cost_spent,
        )

        # Restore programs
        selected = []
        for p_data in checkpoint.selected_programs:
            try:
                selected.append(BountyProgram(**p_data))
            except Exception:
                logger.debug("Could not restore program: %s", p_data, exc_info=True)

        if not selected:
            logger.warning("No programs restored from checkpoint")
            return

        self._selected_programs = selected
        self.session.programs = selected

        # Restore domains
        all_domains = checkpoint.all_domains
        self._all_domains = all_domains
        self._assignment_context = checkpoint.assignment_context
        self._current_budget = budget
        if self.effective_budget is None:
            self.effective_budget = budget

        if not all_domains:
            logger.warning("No domains in checkpoint")
            return

        # Restore session state
        restore_session(self.session, checkpoint.session)

        # Restore cost baseline
        if checkpoint.cost_spent > 0:
            self.cost_tracker.add_baseline(checkpoint.cost_spent)
            logger.info("Restored cost baseline: $%.2f", checkpoint.cost_spent)

        # Recreate SharedMemory and restore state
        self._emit_phase("swarm")
        logger.info("=== Resuming Swarm (%d agents, %d targets) ===", self.num_agents, len(all_domains))

        self.shared_memory = SharedMemory(scoring=self.scoring, coordination=self.coordination)
        self.shared_memory.configure_mechanisms(self.mechanisms)
        self.shared_memory.on_finding = self._persist_finding

        # Quality gate: restrict findings to in-scope domains
        self.shared_memory.allowed_domains = {d.lower() for d in all_domains}

        # Quality gate: collect excluded vuln types from all selected programs
        excluded = set()
        for program in selected:
            if program.excluded_vuln_types:
                for vt in program.excluded_vuln_types:
                    excluded.add(vt.lower().strip().replace(" ", "_"))
        self.shared_memory.excluded_vuln_types = excluded or None

        if checkpoint.shared_memory:
            restore_shared_memory(self.shared_memory, checkpoint.shared_memory)

        # Reuse the existing scan_id and run name
        self._current_scan_id = checkpoint.scan_id
        self._run_name = checkpoint.run_name or generate_run_name(checkpoint.scan_id)
        logger.info("Resuming run: %s (scan #%d)", self._run_name, checkpoint.scan_id)

        # Start Ollama pool if needed
        worker_provider = self._llm_config.default_worker.provider
        if worker_provider == "ollama" and self.settings.ollama_per_agent:
            from utils.ollama_pool import OllamaPool
            self._ollama_pool = OllamaPool(
                num_instances=self.num_agents,
                model=self.settings.ollama_model,
            )
            self._ollama_urls = await self._ollama_pool.start()

        # ── Create AgentTeam ────────────────────────────────────────
        team = AgentTeam(
            team_id="alpha",
            num_agents=self.num_agents,
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
        team.shared_memory = self.shared_memory
        team.browser_pool = self.browser_pool
        team.web_bridge = self.web_bridge
        team._ollama_urls = self._ollama_urls
        team._all_domains = all_domains
        team.effective_budget = self.effective_budget
        team.on_periodic = lambda: self._save_checkpoint_now("swarm")
        self._team = team

        # Spawn fresh agents (conversations restart, but shared memory has context)
        keep_recent = getattr(self, "_keep_recent", None) or self.settings.keep_recent
        await team.spawn_agents(keep_recent=keep_recent)
        self.pentesters = team.pentesters  # backward compat

        # Add resume context to the assignment
        prior_findings = len(checkpoint.shared_memory.get("findings", []))
        prior_entries = len(checkpoint.shared_memory.get("entries", []))
        prior_scores = checkpoint.shared_memory.get("scores", {})
        score_summary = ", ".join(
            f"{aid}: {sd['total']}" for aid, sd in prior_scores.items()
        ) if prior_scores else "no prior scores"
        resume_hint = (
            f"\n\n=== RESUMED SESSION ===\n"
            f"This is a resumed scan. Prior session had {prior_findings} findings "
            f"and {prior_entries} shared memory entries. Your scores from the prior "
            f"session have been preserved ({score_summary}). Check read_discoveries "
            f"for your current score and prior work.\n"
            f"=== END RESUME ==="
        )
        assignment_context = checkpoint.assignment_context + resume_hint

        # Start recorder
        self.recorder = None
        if self.graph and self.shared_memory:
            from agents.recorder import Recorder
            recorder_llm = self.director.llm if (self.director and self.director.llm and not self.dry_run) else None
            broadcast_fn = self.web_bridge._broadcast if self.web_bridge else None
            self.recorder = Recorder(
                self.shared_memory, self.graph,
                llm=recorder_llm,
                broadcast=broadcast_fn,
                cost_tracker=self.cost_tracker,
            )
            self.recorder.start()

        # SIGINT handler
        original_handler = signal.getsignal(signal.SIGINT)
        sigint_count = 0

        def _sigint_handler(sig, frame):
            nonlocal sigint_count
            sigint_count += 1
            if sigint_count == 1:
                active = [t for t in team._running_tasks if not t.done() and t.get_name() != "swarm-monitor"]
                findings_n = self.shared_memory.findings_count if self.shared_memory else 0
                run_label = getattr(self, "_run_name", "")
                print(f"\n  Stopping {len(active)} agent(s)... (Ctrl+C again to force)")
                print(f"  Checkpoint: {run_label} | {findings_n} findings so far")
                self._stop_flag = True
                team.stop()
                self._save_checkpoint_now("swarm")
            else:
                print(f"\n  Force stopping...")
                team.cancel_all()

        signal.signal(signal.SIGINT, _sigint_handler)

        try:
            await team.run(
                targets=all_domains,
                assignment_context=assignment_context,
                budget=budget,
            )
        except Exception:
            logger.exception("Resumed swarm execution error — saving checkpoint")
            self._save_checkpoint_now("swarm")
        finally:
            signal.signal(signal.SIGINT, original_handler)

        if self.recorder:
            self.recorder.stop()
            if self.recorder._task:
                try:
                    await self.recorder._task
                except (asyncio.CancelledError, Exception):
                    pass
            rec = self.recorder.summary()
            logger.info("Recorder: stored %d, processed %d, errors %d",
                        rec["stored_count"], rec["processed"], rec["graph_errors"] + rec["llm_empty"])

        # ── Collect findings ──────────────────────────────────────────
        findings = self.shared_memory.get_findings()
        logger.info("=== Resumed Swarm Complete: %d deduplicated findings ===", len(findings))

        for agent in self.pentesters:
            if agent.last_trace and agent.last_trace.final_answer:
                parsed = agent._parse_final_answer(agent.last_trace.final_answer)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, VulnFinding):
                            accepted, reason = await self.shared_memory.submit_finding(agent.agent_id, item)
                            if reason:
                                logger.info("Rejected final-answer finding from %s: %s", agent.agent_id, reason)

        if self.shared_memory.mechanisms:
            from memory.mechanisms import apply_run_end
            apply_run_end([self.shared_memory])

        findings = self.shared_memory.get_findings()
        self.session.raw_findings.extend(findings)
        self.session.analyzed_findings.extend(findings)

        for domain in all_domains:
            self.session.mark_scanned(domain)

        scan_id = checkpoint.scan_id
        if self.graph:
            per_domain_costs = self.cost_tracker.get_per_target_costs()
            for domain in all_domains:
                domain_findings = [f for f in findings if domain in f.target]
                domain_cost = per_domain_costs.get(domain, 0.0)
                await self.graph.store_scan(scan_id, domain, len(domain_findings), domain_cost)

        self.persistent.complete_scan(scan_id, len(findings), self.cost_tracker.get_total())

        for program in selected:
            program_findings = [
                f for f in findings
                if any(d in f.target for d in program.all_domains)
            ]
            self.persistent.update_program_stats(program.url, findings=len(program_findings))

        # On Ctrl+C: prompt user for next action
        if self._stop_flag:
            self._print_shutdown_summary()
            action = self._prompt_shutdown_action()
            if action == "exit":
                self._print_conversation_log()
                return
            # action == "debrief" — fall through to debrief, skip chain+report

        # ── Attack Chain Synthesis (skip on stop) ─────────────────────
        if not self._stop_flag and findings and len(findings) >= 2:
            logger.info("=== Chain Synthesis: analyzing %d findings for attack chains ===", len(findings))
            discoveries = self.shared_memory.read_all() if self.shared_memory else []

            # Query graph for historical chain patterns
            graph_context = ""
            if self.graph:
                try:
                    graph_context = await self.graph.build_chain_context(all_domains[:10])
                    if graph_context:
                        logger.info("Chain synthesis enriched with graph context (%d chars)", len(graph_context))
                except Exception:
                    logger.debug("Failed to build graph chain context", exc_info=True)

            chains = await self.director.synthesize_attack_chains(
                findings=findings,
                discoveries=discoveries,
                cost_tracker=self.cost_tracker,
                graph_context=graph_context,
            )
            if chains:
                logger.info("=== Chain Synthesis: %d attack chains identified ===", len(chains))
                for chain in chains:
                    component_refs = []
                    for idx in chain.get("chain_components", []):
                        if 0 <= idx < len(findings):
                            f = findings[idx]
                            component_refs.append(f"- [{f.severity.value.upper()}] {f.title or f.vuln_type.value} on {f.target}")

                    chain_evidence = (
                        chain.get("evidence", "") +
                        "\n\nComponent findings:\n" + "\n".join(component_refs)
                    )

                    chain_finding = VulnFinding(
                        target=chain.get("target", findings[0].target),
                        vuln_type=VulnType.ATTACK_CHAIN,
                        severity=Severity(chain["severity"]),
                        title=chain["title"],
                        description=chain.get("description", ""),
                        evidence=chain_evidence,
                        url=findings[chain["chain_components"][0]].url if chain["chain_components"] else "",
                        confidence_score=0.8,
                        estimated_bounty=chain.get("estimated_bounty", 0),
                        ai_bounty_estimate=chain.get("estimated_bounty", 0),
                        ai_severity=Severity(chain["severity"]),
                        ai_exploitability="Chain attack — requires combining multiple vulnerabilities",
                    )
                    self.session.analyzed_findings.append(chain_finding)

                    try:
                        self.persistent.save_finding(scan_id, chain_finding.model_dump(mode="json"))
                    except Exception:
                        logger.debug("Failed to persist chain finding", exc_info=True)

                    logger.info(
                        "Chain: [%s] %s → ~$%.0f",
                        chain["severity"].upper(), chain["title"],
                        chain.get("estimated_bounty", 0),
                    )

        # ── Debrief ─────────────────────────────────────────────────
        self._emit_phase("debrief")
        logger.info("=== Phase 3: Director Debrief (resumed) ===")

        agent_traces = []
        for agent in self.pentesters:
            trace = agent.last_trace
            agent_traces.append({
                "agent": agent.agent_id,
                "iterations": trace.iterations if trace else 0,
                "tool_calls": len(trace.tool_calls) if trace else 0,
                "exit_reason": trace.exit_reason if trace else "unknown",
            })

        debrief = await self.director.debrief_swarm(
            shared_memory=self.shared_memory,
            agent_traces=agent_traces,
            targets=all_domains,
            findings_count=len(findings),
            cost_spent=self.cost_tracker.get_total(),
            cost_budget=self.effective_budget or budget,
            cost_tracker=self.cost_tracker,
        )

        logger.info("\n── Director Debrief ──\n%s", debrief)

        from agents.messaging import AgentMessage
        self.bus.send(AgentMessage(
            sender="director",
            recipient="all",
            msg_type="debrief",
            content=debrief,
            data={"findings_count": len(findings), "agent_count": len(self.pentesters)},
        ))

        # Human checkpoint before reporting (skip on stop)
        if not self._stop_flag and findings and not await self._checkpoint("analyze", findings):
            self._print_conversation_log()
            return

        # ── Report (skip on stop) ────────────────────────────────────
        if not self._stop_flag and findings:
            self._emit_phase("report")
            logger.info("── Report: %d findings ──", len(findings))
            await self.reporter.execute(
                TaskAssignment(
                    agent_name="reporter",
                    task_type="report",
                    parameters={
                        "findings": findings,
                        "estimated_costs": self.cost_tracker.get_per_target_costs(),
                    },
                    context=f"Generate reports for {len(findings)} findings from resumed swarm scan",
                )
            )

        # ── Cleanup ──────────────────────────────────────────────────
        if self._ollama_pool is not None:
            self._ollama_pool.stop()
            self._ollama_pool = None

        # Delete checkpoint on successful completion (keep on stop for resume)
        if not self._stop_flag:
            try:
                delete_checkpoint(self.persistent, scan_id)
            except Exception:
                logger.debug("Failed to delete checkpoint", exc_info=True)

        self.session.cost_snapshot = self.cost_tracker.get_total()
        logger.info("\n=== Resumed Swarm Pipeline Complete ===")
        logger.info("Total findings: %d", len(findings))
        logger.info("Total cost: $%.2f", self.cost_tracker.get_total())
        logger.info("Shared memory: %s", self.shared_memory.summary())
        logger.info("Session: %s", self.session.summary())
        self._print_conversation_log()
