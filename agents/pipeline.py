"""Director-driven pipeline runner.

Replaces the procedural Orchestrator with an adaptive Director/Worker flow
where agents reason about their results and communicate via the MessageBus.

When the worker LLM supports tool calling, tool-using workers are created
instead of deterministic ones — giving agents autonomy to adapt their
behavior based on what they observe.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from config import get_settings

if TYPE_CHECKING:
    from web.bridge import PipelineBridge
from cost.tracker import CostTracker
from llm import get_provider
from memory.learning import LearningMemory
from memory.session import SessionMemory
from memory.store import PersistentMemory
from models.finding import VulnFinding

from agents.director import Director
from agents.llm_config import LLMConfigMap, create_llm_for_agent
from agents.messaging import MessageBus, TaskAssignment
from agents.workers import (
    AnalyzeWorker,
    DiscoverWorker,
    ReconWorker,
    ReportWorker,
    ScanWorker,
)
from memory.graph import GraphMemory
from utils.browser import BrowserPool

logger = logging.getLogger(__name__)

import re as _re

# PDF exports of program pages interleave the out-of-scope list with page
# footers ("52/66"), export timestamps ("3/20/26, 6:08 PM") and column headers
# ("Name / Location"). A genuine exclusion rule reads like a sentence.
_NOISE_LINES = {
    "name / location", "tags", "known issues",
    "web applications", "mobile applications",
}


def _looks_like_rule(line: str) -> bool:
    """True if a host-free out-of-scope line is a real vuln-class rule rather
    than PDF pagination/date/header noise."""
    s = line.strip()
    if not (8 <= len(s) <= 200):
        return False
    if s.lower() in _NOISE_LINES:
        return False
    # Pure numbers / dates / page markers ("52/66", "3/20/26, 6:08 PM").
    if _re.fullmatch(r"[\d/:.,%\s\-APMapm]+", s):
        return False
    # A rule has words: at least one space between alphabetic tokens.
    return bool(_re.search(r"[A-Za-z]", s)) and " " in s


class AgentPipeline:
    """Director-driven pipeline with LLM-powered agent communication.

    The Director assigns work to Workers, reads their reflections, handles
    concerns, and makes strategic decisions — all via the MessageBus.
    """

    def __init__(
        self,
        llm_config: LLMConfigMap | None = None,
        dry_run: bool = False,
        auto_mode: bool = False,
        pause_points: list[str] | None = None,
        web_bridge: PipelineBridge | None = None,
        target_provider: Any = None,
        methodology: Any = None,
        scoring: Any = None,
        mechanisms: Any = None,
        coordination: Any = None,
        workflow: Any = None,
    ) -> None:
        self.settings = get_settings()
        from profiles import LabWorkflow
        self.workflow = workflow if workflow is not None else LabWorkflow()
        self.dry_run = dry_run
        self.auto_mode = auto_mode
        self.pause_points = set(pause_points or [])
        self.web_bridge = web_bridge

        # Pluggable "what are we testing" / "how do agents think" strategies.
        # Defaults preserve exact pre-existing behavior (direct --targets or
        # bug-bounty discovery; pentest methodology) for any caller that
        # doesn't explicitly choose a profile. See agents/target_provider.py
        # and agents/methodology.py.
        from agents.target_provider import AutoTargetProvider
        from agents.methodology import PentestMethodology
        from memory.scoring_policy import DEFAULT_SCORING
        self.target_provider = target_provider or AutoTargetProvider()
        self.methodology = methodology or PentestMethodology()
        self.scoring = scoring or DEFAULT_SCORING
        # Resolved mechanism specs (list[str | dict], see memory/mechanisms.py
        # build_mechanisms). Authoritative when set; falls back to the simpler
        # settings.mechanisms comma-string (plain --mechanisms names only) at
        # construction only when omitted (None); an explicit [] disables mechanisms.
        self.mechanisms: list = (
            list(mechanisms) if mechanisms is not None else
            [name.strip() for name in (getattr(self.settings, "mechanisms", "") or "").split(",") if name.strip()]
        )
        from memory.coordination_policy import DEFAULT_COORDINATION
        self.coordination = coordination or DEFAULT_COORDINATION

        # Pause/stop controls
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused initially
        self._stop_flag = False

        # Core systems
        self.bus = MessageBus()
        self.cost_tracker = CostTracker()
        self.persistent = PersistentMemory()
        self.session = SessionMemory()
        self.learning = LearningMemory(self.persistent)

        # Browser pool — each agent gets its own isolated session
        self.browser_pool: BrowserPool | None = None

        # Graph memory (FalkorDB) — cross-session relational learning
        self.graph: GraphMemory | None = None

        # LLM configuration
        config = llm_config or LLMConfigMap()

        # Create Director (with fallback to worker LLM if primary fails at call time)
        director_llm = None if dry_run else create_llm_for_agent("director", config)
        fallback_llm = None if dry_run else create_llm_for_agent("scanner", config)
        self.director = Director(
            self.bus, llm=director_llm, dry_run=dry_run,
            fallback_llm=fallback_llm,
        )

        # Create Workers — use tool-using workers when LLM supports tool calling
        self._create_workers(config, dry_run)

    def _create_workers(self, config: LLMConfigMap, dry_run: bool) -> None:
        """Create workers — prefer tool-using variants when LLM supports it.

        Browser sessions and help callbacks are wired later in run() once the
        browser pool is started and the bridge is available.
        """
        # Probe whether the worker LLM supports tool calling
        worker_llm_probe = None if dry_run else create_llm_for_agent("recon", config)
        use_tool_workers = (
            worker_llm_probe is not None and worker_llm_probe.supports_tool_use
        )
        if self.workflow.worker_mode == "deterministic":
            use_tool_workers = False
        elif self.workflow.worker_mode == "tool" and not dry_run and not use_tool_workers:
            raise ValueError("workflow.worker_mode='tool' requires a tool-capable recon provider")
        self._use_tool_workers = use_tool_workers
        self.scope_pdf_only: bool = False  # --scope-pdf: load from PDFs only, skip browser scraping
        cli_mode = self.settings.cli_tool_mode

        if use_tool_workers:
            logger.info("Worker LLM supports tool calling — using adaptive tool workers")
            if cli_mode:
                logger.info("CLI tool mode enabled — using single 'run' tool to reduce token usage")
            from agents.tool_workers import (
                AnalyzeToolWorker,
                DiscoverToolWorker,
                ReconToolWorker,
                ScanToolWorker,
            )

            self.discover = DiscoverToolWorker(
                self.bus,
                llm=None if dry_run else create_llm_for_agent("discover", config),
                cost_tracker=self.cost_tracker,
                cli_mode=cli_mode,
            )
            self.discover.persistent_store = self.persistent
            # Reuse the probe LLM for recon (avoid creating a duplicate)
            self.recon = ReconToolWorker(
                self.bus,
                llm=worker_llm_probe,
                cli_mode=cli_mode,
                cost_tracker=self.cost_tracker,
            )
            self.scanner = ScanToolWorker(
                self.bus,
                llm=None if dry_run else create_llm_for_agent("scanner", config),
                max_concurrency=self.settings.max_concurrency,
                cli_mode=cli_mode,
                cost_tracker=self.cost_tracker,
            )
            analyzer_llm = None if dry_run else create_llm_for_agent("analyzer", config)
            analysis_llm = None if dry_run else self._get_analysis_llm()
            self.analyzer = AnalyzeToolWorker(
                self.bus,
                llm=analyzer_llm,
                analysis_llm=analysis_llm,
                cli_mode=cli_mode,
                cost_tracker=self.cost_tracker,
            )
        else:
            if not dry_run:
                logger.info("Worker LLM does not support tool calling — using deterministic workers")
            self.discover = DiscoverWorker(
                self.bus,
                llm=None if dry_run else create_llm_for_agent("discover", config),
                cost_tracker=self.cost_tracker,
            )
            self.recon = ReconWorker(
                self.bus,
                llm=worker_llm_probe,
            )
            self.scanner = ScanWorker(
                self.bus,
                llm=None if dry_run else create_llm_for_agent("scanner", config),
                max_concurrency=self.settings.max_concurrency,
            )
            analyzer_reflection_llm = None if dry_run else create_llm_for_agent("analyzer", config)
            analysis_llm = None if dry_run else self._get_analysis_llm()
            self.analyzer = AnalyzeWorker(
                self.bus,
                llm=analyzer_reflection_llm,
                analysis_llm=analysis_llm,
            )

        # Reporter stays deterministic (it doesn't benefit from tool-use)
        self.reporter = ReportWorker(
            self.bus,
            llm=None if dry_run else create_llm_for_agent("reporter", config),
        )

    def _get_analysis_llm(self):
        """Get the LLM for the underlying AnalyzerAgent (vuln analysis)."""
        try:
            return get_provider()
        except Exception:
            logger.warning("Could not create analysis LLM provider")
            return None

    # ------------------------------------------------------------------
    # Web dashboard controls
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Pause pipeline at the next phase boundary."""
        self._pause_event.clear()
        logger.info("Pipeline pause requested")

    def resume(self) -> None:
        """Resume a paused pipeline."""
        self._pause_event.set()
        logger.info("Pipeline resumed")

    def stop(self) -> None:
        """Request a graceful pipeline stop at the next phase boundary."""
        self._stop_flag = True
        self._pause_event.set()  # unblock if paused
        logger.info("Pipeline stop requested")

    async def _check_pause_stop(self) -> bool:
        """Wait if paused, return False if stop was requested."""
        if self._stop_flag:
            return False
        await self._pause_event.wait()
        return not self._stop_flag

    def _emit_phase(self, phase: str, program: str = "") -> None:
        """Broadcast a phase change via the MessageBus and bridge."""
        from agents.messaging import AgentMessage

        self.bus.send(AgentMessage(
            sender="pipeline",
            recipient="all",
            msg_type="phase_change",
            content=f"Phase '{phase}' started" + (f" for {program}" if program else ""),
            data={"phase": phase, "program": program},
        ))
        if self.web_bridge:
            self.web_bridge.set_phase(phase, program)

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    async def _start_graph(self) -> None:
        """Start the FalkorDB graph memory if enabled."""
        if not self.settings.falkordb_enabled:
            logger.info("Graph memory disabled (falkordb_enabled=False)")
            return

        graph = GraphMemory(
            host=self.settings.falkordb_host,
            port=self.settings.falkordb_port,
        )
        if not await graph.ensure_container():
            logger.info("FalkorDB container unavailable — continuing without graph memory")
            return
        if not await graph.connect():
            logger.info("FalkorDB connection failed — continuing without graph memory")
            return

        await graph.init_indices()
        self.graph = graph

        # Wire dashboard event callback
        if self.web_bridge:
            graph._event_callback = self.web_bridge.on_graph_op

        logger.info("Graph memory started (FalkorDB)")

    async def _stop_graph(self) -> None:
        """Close the graph connection (container stays running)."""
        if self.graph:
            await self.graph.close()
            self.graph = None

    async def _start_browser(self) -> None:
        """Start the browser pool and wire per-agent sessions into workers."""
        from config import get_settings
        if not get_settings().enable_browser:
            logger.info("Browser pool disabled (--no-browser) — saving ~0.5-1GB RAM")
            self.browser_pool = None
            return
        try:
            self.browser_pool = BrowserPool()
            await self.browser_pool.start(headless=True)
            logger.info("Browser pool started")
        except Exception:
            logger.warning("Could not start browser pool (playwright not installed?)", exc_info=True)
            self.browser_pool = None
            return

        # Wire help callback from bridge (if available)
        help_cb = self.web_bridge.request_human_help if self.web_bridge else None

        # Only tool workers accept browser sessions
        if self._use_tool_workers:
            from agents.tools import (
                create_browser_tools,
                create_graph_memory_tools,
                create_help_tool,
                create_session_tools,
            )

            # Wire session memory (chatroom) into all tool workers
            session_tools = create_session_tools(self.session)
            all_tool_agents = [self.discover, self.recon, self.scanner, self.analyzer]
            for agent in all_tool_agents:
                agent.tools.merge(session_tools)

            # Wire graph memory tools into tool workers
            if self.graph:
                graph_tools = create_graph_memory_tools(self.graph)
                for agent in all_tool_agents:
                    agent.tools.merge(graph_tools)

            agents_needing_browser = [
                ("discover", self.discover),
                ("recon", self.recon),
                ("scanner", self.scanner),
            ]
            for agent_name, agent in agents_needing_browser:
                session = await self.browser_pool.create_session(agent_name)
                browser_tools = create_browser_tools(session)
                agent.tools.merge(browser_tools)

            # Wire help tool into all tool workers
            if help_cb:
                help_tools = create_help_tool(help_cb)
                for _, agent in agents_needing_browser:
                    agent.tools.merge(help_tools)
                # Analyzer too
                self.analyzer.tools.merge(create_help_tool(help_cb))

        # Wire help callback on all agents (for request_human_help() method)
        if help_cb:
            for agent in [self.discover, self.recon, self.scanner, self.analyzer]:
                agent._help_callback = help_cb

    async def _stop_browser(self) -> None:
        """Shut down the browser pool."""
        if self.browser_pool:
            await self.browser_pool.stop()
            self.browser_pool = None

    async def run(
        self,
        top_n: int = 5,
        min_bounty: float = 50.0,
        budget: float | None = None,
        targets: list[str] | None = None,
        program_url: str | None = None,
        resume_checkpoint: Any = None,
    ) -> None:
        """Run the full Director-driven pipeline.

        If resume_checkpoint is provided (a PipelineCheckpoint), resumes from
        that checkpoint instead of running from scratch.
        """
        budget = budget or self.settings.session_budget
        self.effective_budget = budget  # expose for dashboard

        # Wire budget and pause event into tool workers
        if self._use_tool_workers:
            for agent in [self.discover, self.recon, self.scanner, self.analyzer]:
                agent.session_budget = budget
                agent._pause_event = self._pause_event

        self._direct_targets = targets
        self._direct_program_url = program_url

        if resume_checkpoint:
            logger.info(
                "Resuming pipeline from checkpoint (scan=%d, budget $%.2f)",
                resume_checkpoint.scan_id, budget,
            )
        else:
            logger.info(
                "Starting agent pipeline (top %d, budget $%.2f, dry_run=%s, auto=%s)",
                top_n, budget, self.dry_run, self.auto_mode,
            )

        # Start graph memory + browser pool + wire sessions into agents
        await self._start_graph()
        await self._start_browser()

        try:
            if resume_checkpoint and hasattr(self, "_resume_from_checkpoint"):
                await self._resume_from_checkpoint(resume_checkpoint, budget)
            else:
                await self._run_pipeline(top_n, min_bounty, budget)
        finally:
            print("  Cleaning up...")
            await self._stop_browser()
            await self._stop_graph()
            print("  Done.")

    async def _run_pipeline(
        self,
        top_n: int,
        min_bounty: float,
        budget: float,
    ) -> None:
        """Inner pipeline logic, separated for clean browser lifecycle."""
        # Show learning insights
        learning_summary = self.learning.summary()
        if "No historical data" not in learning_summary:
            logger.info("\n%s", learning_summary)

        # ── Resolve targets (pluggable: direct / bug-bounty discovery / ──
        #    any other TargetProvider a profile chose) ──────────────────
        selected = await self.target_provider.get_targets(self, top_n, min_bounty)
        if selected is None:
            return

        # ── Scope enrichment (browser-based) ──────────────────────────
        if self.browser_pool and self._use_tool_workers:
            await self._enrich_scope(selected)

        # ── Phase 2-5: Per-program pipeline ────────────────────────────
        all_findings: list[VulnFinding] = []

        for program in selected:
            if not await self._check_pause_stop():
                logger.info("Pipeline stopped by user request.")
                break

            if not self.cost_tracker.check_budget(getattr(self, "effective_budget", None) or budget):
                logger.warning(
                    "Budget exceeded ($%.2f/$%.2f). Stopping.",
                    self.cost_tracker.get_total(), budget,
                )
                break

            logger.info("\n=== Processing: %s ===", program.name)

            # Filter domains via learning memory
            domains_to_scan = []
            for domain in program.all_domains:
                should_scan, reason = self.learning.should_scan_target(domain)
                if should_scan:
                    domains_to_scan.append(domain)
                else:
                    logger.info("Skipping %s: %s", domain, reason)
                    self.session.mark_skipped(domain, reason)

            if not domains_to_scan:
                logger.info("All domains for %s skipped", program.name)
                continue

            # Wire scope enforcement into lab mode workers
            if self._use_tool_workers:
                all_targets = set(domains_to_scan)
                for agent in [self.recon, self.scanner]:
                    agent._allowed_domains = all_targets

            findings = await self._process_program(
                program.name, domains_to_scan, budget
            )
            all_findings.extend(findings)

            self.persistent.update_program_stats(
                program.url, findings=len(findings)
            )

        # ── Summary ────────────────────────────────────────────────────
        self.session.cost_snapshot = self.cost_tracker.get_total()
        logger.info("\n=== Pipeline Complete ===")
        logger.info("Total findings: %d", len(all_findings))
        logger.info("Total cost: $%.2f", self.cost_tracker.get_total())
        logger.info("Session: %s", self.session.summary())

        self._print_conversation_log()

    # ------------------------------------------------------------------
    # Scope enrichment
    # ------------------------------------------------------------------

    async def _enrich_scope(self, programs: list) -> None:
        """Browse program pages to read detailed scope rules."""
        pdf_only = getattr(self, "scope_pdf_only", False)
        mode_label = "PDF-only" if pdf_only else "browser + PDF"
        logger.info("── Scope enrichment (%s): reading program pages ──", mode_label)

        discover_session = None
        logged_in_platforms: set[str] = set()

        if not pdf_only:
            discover_session = await self.browser_pool.get_session("discover")
            if not discover_session:
                logger.warning("No browser session — falling back to PDF-only scope enrichment")
                pdf_only = True

            # Login to Bugcrowd if credentials are configured
            if discover_session:
                bc_email = self.settings.bugcrowd_email
                bc_password = self.settings.bugcrowd_password
                if bc_email and bc_password:
                    try:
                        await self._bugcrowd_login(discover_session, bc_email, bc_password)
                        logged_in_platforms.add("bugcrowd")
                    except Exception:
                        logger.warning("Bugcrowd login failed — will try unauthenticated", exc_info=True)

        for program in programs:
            if not program.url:
                continue
            try:
                text = ""

                # Check for a local PDF first (user-saved program scope)
                pdf_text = self._load_scope_pdf(program)
                if pdf_text:
                    text = pdf_text
                    logger.info("Loaded scope from PDF for %s (%d chars)", program.name, len(text))
                elif not pdf_only and discover_session:
                    platform = program.platform.lower()
                    if platform == "hackerone":
                        text = await self._enrich_hackerone(discover_session, program)
                    elif platform == "bugcrowd":
                        text = await self._enrich_bugcrowd(
                            discover_session, program,
                            authenticated="bugcrowd" in logged_in_platforms,
                        )
                    else:
                        text = await self._enrich_generic(discover_session, program)

                if not text or len(text) < 100:
                    logger.info("No scope content for %s (%d chars)", program.name, len(text))
                    continue

                program.scope_raw_text = text[:8000]
                self.session.scope_rules[program.url] = program.scope_raw_text

                # Parse structured fields from the page text
                self._parse_scope_text(program, text)

                logger.info(
                    "Read scope for %s (%d chars, %d domains, %d exclusions, %d excluded vuln types)",
                    program.name, len(program.scope_raw_text),
                    len(program.scope_domains), len(program.out_of_scope),
                    len(program.excluded_vuln_types),
                )
            except Exception:
                logger.debug("Could not read scope for %s", program.name, exc_info=True)

    @staticmethod
    def _load_scope_pdf(program) -> str:
        """Load scope from a user-saved PDF in data/program_scopes/.

        Matches PDFs by slug derived from program name or URL handle.
        E.g. 'T-Mobile' → t-mobile.pdf, 'OpenSea Managed Bug Bounty Program' → opensea.pdf
        Naming convention: use the program handle/slug as filename.
        """
        import os
        import re

        scope_dir = os.path.join("data", "program_scopes")
        if not os.path.isdir(scope_dir):
            return ""

        # Generate candidate slugs from program URL and name
        candidates = set()
        # From URL: https://bugcrowd.com/engagements/t-mobile → t-mobile
        if program.url:
            handle = program.url.rstrip("/").split("/")[-1]
            # Strip query params
            handle = handle.split("?")[0]
            if handle:
                candidates.add(handle.lower())
        # From name: "T-Mobile" → "t-mobile", "OpenSea Managed Bug Bounty Program" → "opensea-managed-bug-bounty-program"
        if program.name:
            slug = re.sub(r"[^a-z0-9]+", "-", program.name.lower()).strip("-")
            candidates.add(slug)
            # Also try just the first word (e.g. "opensea", "tesla")
            first_word = slug.split("-")[0]
            if len(first_word) >= 3:
                candidates.add(first_word)

        # Check for matching PDFs
        for candidate in candidates:
            pdf_path = os.path.join(scope_dir, f"{candidate}.pdf")
            if os.path.isfile(pdf_path):
                try:
                    import fitz  # PyMuPDF
                    doc = fitz.open(pdf_path)
                    text_parts = []
                    for page in doc:
                        text_parts.append(page.get_text())
                    doc.close()
                    return "\n".join(text_parts)
                except ImportError:
                    logger.warning("PyMuPDF (fitz) not installed — cannot read scope PDF %s", pdf_path)
                    return ""
                except Exception:
                    logger.debug("Failed to read scope PDF %s", pdf_path, exc_info=True)
                    return ""

        return ""

    async def _bugcrowd_login(self, session, email: str, password: str) -> None:
        """Login to Bugcrowd via headless browser."""
        logger.info("Logging in to Bugcrowd...")
        await session.navigate("https://bugcrowd.com/user/sign_in")
        await asyncio.sleep(2)

        await session.type_text('input[name="user[email]"]', email)
        await session.type_text('input[name="user[password]"]', password)
        await session.click('input[type="submit"], button[type="submit"]')
        await asyncio.sleep(3)

        # Verify login succeeded
        text = await session.get_text()
        if "sign in" in text.lower()[:500] and len(text) < 2000:
            raise RuntimeError("Bugcrowd login failed — still on sign in page")
        logger.info("Bugcrowd login successful")

    async def _enrich_hackerone(self, session, program) -> str:
        """Scrape HackerOne program scope (public, needs JS rendering)."""
        # HackerOne scope is at /<handle>?type=team or the program URL directly
        url = program.url
        if "?" not in url:
            url += "?type=team"

        await session.navigate(url)
        await asyncio.sleep(3)  # HackerOne is heavy on JS
        text = await session.get_text()

        # Try clicking into the policy/scope sections
        for tab in ("Policy", "Scope", "Targets", "In Scope", "Structured scope"):
            try:
                await session.click(f"text={tab}")
                await asyncio.sleep(1)
                extra = await session.get_text()
                if len(extra) > len(text):
                    text = extra
            except Exception:
                pass

        return text

    async def _enrich_bugcrowd(self, session, program, authenticated: bool = False) -> str:
        """Scrape Bugcrowd program scope (needs login + JS for full data)."""
        await session.navigate(program.url)
        await asyncio.sleep(3)  # wait for XHR data to load

        text = await session.get_text()

        if not authenticated and len(text) < 1500:
            logger.info("Bugcrowd %s — likely needs login (%d chars)", program.name, len(text))
            return ""

        # Bugcrowd loads scope data via XHR — click through tabs to trigger it
        for tab in ("Scope", "Target Groups", "Targets", "Brief", "Rewards"):
            try:
                await session.click(f"text={tab}")
                await asyncio.sleep(2)  # wait for XHR
                extra = await session.get_text()
                text += f"\n\n--- {tab} ---\n{extra}"
            except Exception:
                pass

        return text

    async def _enrich_generic(self, session, program) -> str:
        """Generic scope enrichment for other platforms."""
        await session.navigate(program.url)
        await asyncio.sleep(2)
        text = await session.get_text()

        # Skip pages that are primarily login forms
        login_signals = ("please sign in", "please log in", "you must log in",
                         "authentication required", "sign in to continue")
        text_lower = text.lower()
        if any(sig in text_lower for sig in login_signals) and len(text) < 1500:
            logger.info("Login wall at %s (%d chars)", program.name, len(text))
            return ""

        for tab in ("Scope", "Targets", "In Scope"):
            try:
                await session.click(f"text={tab}")
                extra = await session.get_text()
                text += f"\n\n--- {tab} ---\n{extra}"
            except Exception:
                pass

        return text

    @staticmethod
    def _parse_scope_text(program, text: str) -> None:
        """Extract structured scope data from program page text into BountyProgram fields."""
        import re

        lines = text.split("\n")
        text_lower = text.lower()

        # ── Extract in-scope domains/URLs from the page ──────────────
        # Look for domain-like patterns in scope sections
        domain_pattern = re.compile(
            r"(?:^|\s)((?:\*\.)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?:/[a-zA-Z0-9\-_/]*)?)"
        )

        # Find "focus assets", "in scope", "assets in scope" sections
        in_scope_section = False
        out_scope_section = False
        exclusion_section = False
        found_domains: list[str] = []
        found_out_of_scope: list[str] = []
        found_exclusions: list[str] = []
        scope_instructions_parts: list[str] = []

        for line in lines:
            line_stripped = line.strip()
            line_lower = line_stripped.lower()

            # Section detection
            if any(kw in line_lower for kw in (
                "focus assets in scope", "focus assets", "assets in scope",
                "in-scope", "in scope targets", "scope targets",
            )):
                in_scope_section = True
                out_scope_section = False
                exclusion_section = False
                continue
            if any(kw in line_lower for kw in (
                "out of scope", "out-of-scope", "scope exclusions",
                "exclusion notice", "not in scope", "ineligible",
                "informative reports",
            )):
                in_scope_section = False
                out_scope_section = True
                exclusion_section = False
                continue
            if any(kw in line_lower for kw in (
                "program exclusions", "excluded vulnerabilities",
                "excluded vuln", "vulnerability exclusions",
                "categories of vulnerabilities are excluded",
            )):
                in_scope_section = False
                out_scope_section = False
                exclusion_section = True
                continue
            if any(kw in line_lower for kw in (
                "program terms", "terms and conditions", "reporting process",
                "awarding process", "payment process", "top hackers",
            )):
                in_scope_section = False
                out_scope_section = False
                exclusion_section = False
                continue

            # Collect domains from in-scope section
            if in_scope_section and line_stripped:
                matches = domain_pattern.findall(line_stripped)
                for m in matches:
                    m = m.strip().rstrip(".")
                    if "." in m and len(m) > 4:
                        found_domains.append(m)

            # Collect out-of-scope entries
            if out_scope_section and line_stripped:
                # Collect domain-like entries (the actual host exclusions).
                matches = domain_pattern.findall(line_stripped)
                host_hits = [m.strip().rstrip(".") for m in matches
                             if "." in m.strip() and len(m.strip()) > 4]
                if host_hits:
                    found_out_of_scope.extend(host_hits)
                # A host-free line in this section is either a path glob
                # (e.g. "/self-service-*"), a vuln-class rule, or PDF noise
                # (page numbers like "52/66", dates, column headers). Keep the
                # first two; a vuln-class rule belongs with the vuln exclusions,
                # not the host list, so it never gets mechanically matched.
                elif line_stripped.startswith("/"):
                    found_out_of_scope.append(line_stripped)
                elif _looks_like_rule(line_stripped):
                    found_exclusions.append(line_stripped)

            # Collect excluded vulnerability types
            if exclusion_section and line_stripped:
                if len(line_stripped) > 5 and len(line_stripped) < 300:
                    found_exclusions.append(line_stripped)

        # ── Extract bounty range from page ──────────────────────────
        bounty_match = re.findall(r"\$([0-9,]+)(?:\s*[-–—]\s*\$([0-9,]+))?", text)
        bounty_values = []
        for m in bounty_match:
            try:
                bounty_values.append(float(m[0].replace(",", "")))
                if m[1]:
                    bounty_values.append(float(m[1].replace(",", "")))
            except ValueError:
                pass
        if bounty_values:
            if program.min_bounty == 0:
                program.min_bounty = min(bounty_values)
            if program.max_bounty == 0:
                program.max_bounty = max(bounty_values)

        # ── Extract response time from page ─────────────────────────
        resp_match = re.search(
            r"(?:average time to first response|response time)[:\s]*(\d+)\s*(day|hour|minute|month)",
            text_lower,
        )
        if resp_match:
            val = int(resp_match.group(1))
            unit = resp_match.group(2)
            if unit == "hour":
                program.response_time_days = max(0.5, val / 24)
            elif unit == "day":
                program.response_time_days = float(val)
            elif unit == "month":
                program.response_time_days = float(val * 30)

        # ── Merge found domains into program (don't replace existing) ─
        # For a manual/direct-targets program the user's --targets ARE the
        # authoritative scope. Page parsing is unreliable at telling in-scope
        # from out-of-scope hosts in a multi-section program page (it has, for
        # the same page, filed an in-scope host under out-of-scope and swept
        # unrelated vendor domains into scope), so we must NOT let it widen the
        # host set the swarm may reach. Rules (out_of_scope / excluded vulns /
        # bounty) are still enriched below — only the in-scope hosts are frozen.
        if getattr(program, "platform", "") == "manual":
            found_domains = []
        existing = set(program.scope_domains + program.scope_wildcards)
        for d in found_domains:
            if d.startswith("*."):
                if d not in existing:
                    program.scope_wildcards.append(d)
                    existing.add(d)
            elif d not in existing:
                program.scope_domains.append(d)
                existing.add(d)

        # ── Populate structured fields ──────────────────────────────
        if found_out_of_scope:
            program.out_of_scope = list(dict.fromkeys(found_out_of_scope))[:50]
        if found_exclusions:
            program.excluded_vuln_types = list(dict.fromkeys(found_exclusions))[:30]

        # ── Build scope instructions summary ────────────────────────
        parts = []
        if program.scope_domains or program.scope_wildcards:
            parts.append(f"In-scope targets: {', '.join(program.all_domains)}")
        if program.out_of_scope:
            parts.append(f"Out of scope: {'; '.join(program.out_of_scope[:10])}")
        if program.excluded_vuln_types:
            parts.append(f"Excluded vulns: {'; '.join(program.excluded_vuln_types[:10])}")
        if bounty_values:
            parts.append(f"Bounty range: ${min(bounty_values):.0f}-${max(bounty_values):.0f}")
        if parts:
            program.scope_instructions = "\n".join(parts)

    # ------------------------------------------------------------------
    # Per-program processing
    # ------------------------------------------------------------------

    async def _execute_lab_worker(self, worker, assignment):
        """Honor terminal pause/stop at stage boundaries and after cancellation."""
        if not await self._check_pause_stop():
            raise asyncio.CancelledError()
        result = await worker.execute(assignment)
        # ToolAgent returns a failed result on cancellation. Do not continue
        # into Director calls or subsequent stages after a user stop.
        if self._stop_flag:
            raise asyncio.CancelledError()
        return result

    async def _process_program(
        self,
        program_name: str,
        domains: list[str],
        budget: float,
    ) -> list[VulnFinding]:
        """Run recon → scan → analyze → report for a single program."""
        budget_remaining = (getattr(self, "effective_budget", None) or budget) - self.cost_tracker.get_total()
        learning_insights = self.learning.summary()

        scan_id = self.persistent.start_scan(
            target=",".join(domains),
            metadata={"domain_count": len(domains), "program": program_name},
        )

        recon_data = {}
        if "recon" in self.workflow.stages:
            # ── Recon ──────────────────────────────────────────────────────
            self._emit_phase("recon", program_name)
            logger.info("── Recon: %s ──", program_name)

            # Build scope-aware recon context
            recon_context = f"Reconnaissance on {program_name}: {', '.join(domains)}"
            program_obj = next((p for p in self.session.programs if p.name == program_name), None)
            if program_obj:
                if program_obj.out_of_scope:
                    recon_context += f"\n\nOUT OF SCOPE (skip these): {', '.join(program_obj.out_of_scope[:20])}"
                if program_obj.required_auth:
                    recon_context += f"\nREQUIRED AUTH: {program_obj.required_auth}"
                if program_obj.scope_raw_text:
                    recon_context += f"\n\nScope summary:\n{program_obj.scope_raw_text[:1500]}"

            recon_result = await self._execute_lab_worker(self.recon,
                TaskAssignment(
                    agent_name="recon",
                    task_type="recon",
                    parameters={"domains": domains},
                    context=recon_context,
                )
            )

            recon_decision = await self.director.evaluate_phase_result(
                recon_result, "recon",
                budget_remaining=budget_remaining,
                learning_insights=learning_insights,
            )

            if recon_decision.decision in ("skip", "abort"):
                logger.info("Director: %s recon — %s", recon_decision.decision, recon_decision.reasoning)
                self.persistent.complete_scan(scan_id, 0, self.cost_tracker.get_total())
                for d in domains:
                    self.session.mark_skipped(d, recon_decision.reasoning)
                return []

            recon_data = recon_result.data if isinstance(recon_result.data, dict) else {}
            self.session.recon_data.update(recon_data)

            # Tag targets with technologies
            for tech in recon_data.get("technologies", []):
                for domain in domains:
                    self.session.add_tags(domain, [tech])
                    if self.graph:
                        await self.graph.store_technology(domain, tech)

        # ── Scan ───────────────────────────────────────────────────────
        self._emit_phase("scan", program_name)
        logger.info("── Scan: %s ──", program_name)
        targets = recon_data.get("subdomains", domains)
        if len(targets) > self.workflow.max_scan_targets:
            logger.info("Limiting scan targets from %d to %d", len(targets), self.workflow.max_scan_targets)
            targets = targets[:self.workflow.max_scan_targets]

        # Build scope context for the scanner
        scan_context = f"Vulnerability scanning for {program_name}"
        program_obj = next((p for p in self.session.programs if p.name == program_name), None)
        if program_obj and program_obj.scope_raw_text:
            scope_hint = ""
            if program_obj.scope_instructions:
                scope_hint += f"\nScope rules: {program_obj.scope_instructions}"
            if program_obj.out_of_scope:
                scope_hint += f"\nOut of scope: {', '.join(program_obj.out_of_scope)}"
            if program_obj.excluded_vuln_types:
                scope_hint += f"\nExcluded vuln types: {', '.join(program_obj.excluded_vuln_types)}"
            if not scope_hint and program_obj.scope_raw_text:
                scope_hint = f"\nScope page text (first 2000 chars): {program_obj.scope_raw_text[:2000]}"
            scan_context += scope_hint

        # Inject recon notes from session memory into scanner context
        recon_notes = ""
        for key in ("recon", "recon_summary", "technologies", "interesting_endpoints"):
            note = self.session.notes.get(key)
            if note:
                recon_notes += f"\n### {key}\n{note}\n"

        if recon_notes:
            scan_context += f"\n\n=== RECON AGENT NOTES ==={recon_notes}=== END RECON NOTES ==="

        # Inject structured recon_data summary
        if recon_data:
            compact = []
            for key in ("subdomains", "technologies", "interesting_urls", "open_ports", "targets"):
                val = recon_data.get(key)
                if val:
                    if isinstance(val, list) and len(val) > 15:
                        compact.append(f"{key}: {val[:15]}... ({len(val)} total)")
                    else:
                        compact.append(f"{key}: {val}")
            if compact:
                scan_context += "\n\n=== STRUCTURED RECON DATA ===\n" + "\n".join(compact) + "\n=== END RECON DATA ==="

        # Inject graph memory context (cross-session intelligence)
        if self.graph:
            techs = recon_data.get("technologies", [])
            graph_context = await self.graph.build_scan_context(targets, techs)
            if graph_context:
                scan_context += f"\n{graph_context}"

        scan_result = await self._execute_lab_worker(self.scanner,
            TaskAssignment(
                agent_name="scanner",
                task_type="scan",
                parameters={"targets": targets, "recon_data": recon_data},
                context=scan_context,
            )
        )

        scan_decision = await self.director.evaluate_phase_result(
            scan_result, "scan",
            budget_remaining=(getattr(self, "effective_budget", None) or budget) - self.cost_tracker.get_total(),
            learning_insights=learning_insights,
        )

        for domain in domains:
            self.session.mark_scanned(domain)

        findings = scan_result.data if isinstance(scan_result.data, list) else []
        self.session.raw_findings.extend(findings)

        if scan_decision.decision in ("skip", "abort") or not findings:
            if not findings:
                logger.info("No findings from scanning phase")
            self.persistent.complete_scan(scan_id, 0, self.cost_tracker.get_total())
            return []

        analyzed_findings = findings
        if "analyze" in self.workflow.stages:
            # ── Analyze ────────────────────────────────────────────────────
            self._emit_phase("analyze", program_name)
            logger.info("── Analyze: %s ──", program_name)
            analyze_result = await self._execute_lab_worker(self.analyzer,
                TaskAssignment(
                    agent_name="analyzer",
                    task_type="analyze",
                    parameters={"findings": findings},
                    context=f"AI analysis of {len(findings)} findings from {program_name}",
                )
            )

            analyze_decision = await self.director.evaluate_phase_result(
                analyze_result, "analyze",
                budget_remaining=(getattr(self, "effective_budget", None) or budget) - self.cost_tracker.get_total(),
            )

            analyzed_findings = analyze_result.data if isinstance(analyze_result.data, list) else []

        self.session.analyzed_findings.extend(analyzed_findings)

        # Persist findings
        for f in analyzed_findings:
            finding_data = f.model_dump(mode="json")
            self.persistent.save_finding(scan_id, finding_data)
            if self.graph:
                await self.graph.store_finding(finding_data, scan_id=scan_id)

        # Store per-domain scan metadata in graph
        if self.graph:
            per_domain_costs = self.cost_tracker.get_per_target_costs()
            for domain in domains:
                domain_findings = [f for f in analyzed_findings if domain in f.target]
                domain_cost = per_domain_costs.get(domain, 0.0)
                await self.graph.store_scan(scan_id, domain, len(domain_findings), domain_cost)

        if not analyzed_findings:
            self.persistent.complete_scan(scan_id, 0, self.cost_tracker.get_total())
            return []

        # Human checkpoint before reporting
        if "report" in self.workflow.stages and not await self._checkpoint("analyze", analyzed_findings):
            self.persistent.complete_scan(
                scan_id, len(analyzed_findings), self.cost_tracker.get_total()
            )
            return analyzed_findings

        if "report" in self.workflow.stages:
            # ── Report ─────────────────────────────────────────────────────
            self._emit_phase("report", program_name)
            logger.info("── Report: %s ──", program_name)
            report_result = await self._execute_lab_worker(self.reporter,
                TaskAssignment(
                    agent_name="reporter",
                    task_type="report",
                    parameters={
                        "findings": analyzed_findings,
                        "estimated_costs": self.cost_tracker.get_per_target_costs(),
                    },
                    context=f"Generate reports for {len(analyzed_findings)} findings from {program_name}",
                )
            )

            await self.director.evaluate_phase_result(report_result, "report")

        self.persistent.complete_scan(
            scan_id, len(analyzed_findings), self.cost_tracker.get_total()
        )
        return analyzed_findings

    # ------------------------------------------------------------------
    # Data coercion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_programs(data: Any) -> list:
        """Best-effort coercion of discover results to BountyProgram objects."""
        from models.program import BountyProgram

        if isinstance(data, list):
            programs = []
            for item in data:
                if isinstance(item, BountyProgram):
                    programs.append(item)
                elif isinstance(item, dict) and "name" in item:
                    try:
                        programs.append(BountyProgram(**item))
                    except Exception:
                        logger.debug("Could not coerce dict to BountyProgram: %s", item)
                else:
                    logger.debug("Skipping non-program item: %s", type(item))
            if programs:
                return programs
        logger.warning("Could not coerce discover data to programs (type=%s)", type(data).__name__)
        return []

    @staticmethod
    def _build_program_from_targets(
        targets: list[str],
        program_url: str | None = None,
    ):
        """Create a synthetic BountyProgram from user-provided target domains."""
        from models.program import BountyProgram

        # Separate wildcards from concrete domains
        domains = [t for t in targets if not t.startswith("*.")]
        wildcards = [t for t in targets if t.startswith("*.")]

        # Derive a program name from the first non-wildcard domain
        name = domains[0] if domains else (wildcards[0].lstrip("*.") if wildcards else "direct-targets")

        return BountyProgram(
            name=f"Direct: {name}",
            platform="manual",
            url=program_url or f"https://{name}",
            scope_domains=domains,
            scope_wildcards=wildcards,
            active=True,
        )

    # ------------------------------------------------------------------
    # Human checkpoints
    # ------------------------------------------------------------------

    async def _checkpoint(self, phase: str, data: Any) -> bool:
        """Pause for human review if not in auto mode and phase is a pause point.

        Returns True to continue, False to abort.
        When a web bridge is attached, uses non-blocking Future-based checkpoints.
        Otherwise falls back to terminal input().
        """
        if self.auto_mode:
            return True

        # If specific pause points configured, only pause at those
        if self.pause_points and phase not in self.pause_points:
            return True

        terminal = getattr(self, "_terminal_controller", None)
        if terminal:
            return await terminal.checkpoint(phase, data)

        # --- Web dashboard path (non-blocking) ---
        if self.web_bridge:
            action = await self.web_bridge.request_checkpoint(
                phase, data, self.cost_tracker.get_total()
            )
            if action in ("abort", "stop"):
                logger.info("Human aborted pipeline at checkpoint '%s' (web)", phase)
                return False
            if action == "skip":
                logger.info("Human skipped phase '%s' (web)", phase)
                return False
            return True

        # --- Terminal path (blocking, wrapped in to_thread) ---
        print(f"\n{'='*60}")
        print(f"  CHECKPOINT: {phase}")
        print(f"{'='*60}")

        if phase == "discover":
            programs = data
            print(f"\n  Selected {len(programs)} programs:")
            for i, p in enumerate(programs, 1):
                print(f"    {i}. {p.name} ({p.platform}) — ROI={p.roi_score:.2f}")

        elif phase == "analyze":
            findings = data
            print(f"\n  {len(findings)} findings ready for reporting:")
            for f in findings[:10]:
                print(
                    f"    [{f.effective_severity.value.upper()}] {f.title} "
                    f"(bounty=${f.ai_bounty_estimate:.0f})"
                )
            if len(findings) > 10:
                print(f"    ... and {len(findings) - 10} more")

        # Show recent director reasoning
        decisions = self.bus.get_log(agent="director", msg_type="decision")
        if decisions:
            latest = decisions[-1]
            print(f"\n  Director's latest reasoning:")
            print(f"    {latest.content[:300]}")

        print(f"\n  Cost so far: ${self.cost_tracker.get_total():.2f}")
        print()

        try:
            response = await asyncio.to_thread(input, "  Continue? [Y/n/q] ")
            response = response.strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if response in ("q", "quit", "abort"):
            logger.info("Human aborted pipeline at checkpoint '%s'", phase)
            return False
        if response in ("n", "no", "skip"):
            logger.info("Human skipped phase '%s'", phase)
            return False
        return True

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def _print_conversation_log(self) -> None:
        """Print the full agent conversation log and append to log file."""
        log = self.bus.format_log()
        if log and log != "(no messages)":
            header = f"\n{'='*60}\n  AGENT CONVERSATION LOG\n{'='*60}\n"
            print(header)
            print(log)
            print()

            # Also write to log file
            try:
                from main import _log_file_path
                if _log_file_path:
                    with open(_log_file_path, "a", encoding="utf-8") as f:
                        f.write(header + "\n" + log + "\n")
            except Exception:
                pass

    def show_costs(self) -> str:
        return self.cost_tracker.summary()

    def show_learning(self) -> str:
        return self.learning.summary()
