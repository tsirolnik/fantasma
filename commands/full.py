"""Resolve a profile and launch interactive, swarm, or explicit sequential lab work."""
from __future__ import annotations

from config import override_settings
from commands.base import Arg, register

_LLM_CHOICES = ["claude", "gemini", "ollama", "openai", "vertex"]

ARGS = [
    Arg(("--diagnostics",), dict(action="store_true", help="Show interactive console diagnostics (also enabled by global --verbose)")),
    Arg(("--interaction",), dict(choices=["batch", "terminal", "web"], default=None,
                                 help="Override terminal/batch interaction; legacy web enables the dashboard")),
    Arg(("--top",), dict(type=int, default=5, help="Number of top programs to target")),
    Arg(("--min-bounty",), dict(type=float, default=50.0, help="Minimum bounty threshold in USD")),
    Arg(("--budget",), dict(type=float, default=None, help="Maximum session budget in USD")),
    Arg(("--auto",), dict(action="store_true", help="Run fully unattended (no human checkpoints)")),
    Arg(("--dry-run",), dict(action="store_true", help="Run without LLM calls (tests message flow)")),
    Arg(("--director-llm",), dict(choices=_LLM_CHOICES, default=None, help="LLM provider for the Director agent")),
    Arg(("--director-model",), dict(default=None, help="Model override for Director")),
    Arg(("--worker-llm",), dict(choices=_LLM_CHOICES, default=None, help="Default LLM provider for worker agents")),
    Arg(("--worker-model",), dict(default=None, help="Model override for workers")),
    Arg(("--analyzer-llm",), dict(choices=_LLM_CHOICES, default=None,
                                   help="LLM provider for the analyzer agent (overrides --worker-llm)")),
    Arg(("--analyzer-model",), dict(default=None, help="Model override for analyzer")),
    Arg(("--pause-at",), dict(default=None, help="Comma-separated phases to pause at (e.g. discover,analyze)")),
    Arg(("--cli-tools",), dict(action="store_true",
                                help="Use CLI-style single 'run' tool to reduce context token usage (~45K tokens saved per run)")),
    Arg(("--mode",), dict(choices=["pentester", "interactive", "swarm", "lab"], default=None,
                           help="Execution engine: pentester (interactive is a compatibility alias), or swarm (N pen-testers across multiple "
                                "competing teams — set --num-teams > 1 for that, not a separate mode "
                                "value) or 'lab' (sequential specialized agents)")),
    Arg(("--swarm-agents",), dict(type=int, default=None,
                                   help="Number of concurrent pen-tester agents per team (swarm mode, default: 3, "
                                        "or the --profile's [topology].agents_per_team)")),
    Arg(("--num-teams",), dict(type=int, default=None,
                                help="Number of competing teams — multi-team competition is just this > 1 on the "
                                     "same engine, not a separate mode (default: 1, or the --profile's "
                                     "[topology].num_teams)")),
    Arg(("--mechanisms",), dict(type=str, default=None,
                                 help="Comma-separated game-theory mechanisms, additive on top of the base scoring "
                                      "(e.g. 'first_finder_bonus,duplicate_escalation'); default: none (unmodified "
                                      "baseline). Overrides the --profile's mechanisms if both are given.")),
    Arg(("--profile",), dict(type=str, default=None,
                              help="Run configuration: methodology, target source, scoring, mechanisms, topology. "
                                   "Built-in name ('interactive', 'lab', 'bugbounty', 'redteam') or a path to a .toml file "
                                   "(see profiles/bugbounty.toml for the schema). Default: interactive profile, "
                                   "selected from profiles/interactive.toml. Explicit CLI flags (--mode, --mechanisms, etc.) override the profile.")),
    Arg(("--ollama-per-agent",), dict(action="store_true",
                                       help="Spawn a separate Ollama instance per swarm agent (for models that don't support OLLAMA_NUM_PARALLEL)")),
    Arg(("--domain-triage",), dict(action="store_true",
                                    help="Enable smart domain triage — Director fingerprints and ranks domains before agents start")),
    Arg(("--no-auth-testing",), dict(action="store_true", help="Disable authenticated testing (on by default)")),
    Arg(("--no-browser",), dict(action="store_true",
                                 help="Disable the Playwright browser pool to cut ~0.5-1GB RAM (agents keep HTTP/docker/code tools)")),
    Arg(("--compact",), dict(action="store_true", help="Use compact system prompt (~60%% smaller) to save tokens")),
    Arg(("--scope-pdf-only",), dict(action="store_true",
                                     help="Load program scope from PDFs in data/program_scopes/ only — skip browser scraping")),
    Arg(("--keep-recent",), dict(type=int, default=None, help="Number of recent message pairs to keep in agent context (default 10)")),
    Arg(("--max-tokens",), dict(type=int, default=None,
                                 help="Max tokens the LLM may generate per call (default 4096; bump for reasoning models like GLM)")),
    Arg(("--generous-context",), dict(action="store_true",
                                       help="Hold several times more verbatim agent history/tool-results before "
                                            "compressing. Safe ONLY for a REMOTE API provider (Novita/OpenAI-"
                                            "compatible/Claude/Gemini/Vertex) with a large context window — a LOCAL "
                                            "Ollama model shares host RAM/VRAM with the agent process and can OOM "
                                            "under this. Must be set explicitly; never inferred from the provider.")),
    Arg(("--targets",), dict(default=None, help="Comma-separated target domains (skips discovery, e.g. 'example.com,api.example.com')")),
    Arg(("--program-url",), dict(default=None, help="Bug bounty program URL (used with --targets for scope enrichment)")),
    Arg(("--web",), dict(action="store_true", help="Launch a real-time web dashboard at http://127.0.0.1:8080")),
    Arg(("--web-port",), dict(type=int, default=None, help="Port for the web dashboard (default: 8080)")),
    Arg(("--resume",), dict(action="store_true", help="Resume from the most recent checkpoint")),
    Arg(("--resume-scan",), dict(default=None, metavar="NAME_OR_ID",
                                  help="Resume a specific run by name (e.g. bold-falcon-42) or scan ID")),
    Arg(("--list-checkpoints",), dict(action="store_true", help="Show available checkpoints and exit")),
]


def _attach_tool_hooks(pipeline, bridge) -> None:
    """Wire the bridge's on_tool_call and on_llm_call hooks onto each ToolAgent worker."""
    from agents.tool_agent import ToolAgent

    for name in ("discover", "recon", "scanner", "analyzer"):
        worker = getattr(pipeline, name, None)
        if isinstance(worker, ToolAgent):
            agent_name = name
            existing_hook = worker.on_tool_call
            if existing_hook is not None:
                worker.on_tool_call = lambda tc, n=agent_name, prev=existing_hook: (prev(tc), bridge.on_tool_call(n, tc))
            else:
                worker.on_tool_call = lambda tc, n=agent_name: bridge.on_tool_call(n, tc)
            worker.on_llm_call = lambda n, model, msgs, tools, dur, reasoning="", agent=agent_name: bridge.on_llm_call(agent, model, msgs, tools, dur, reasoning)


@register("full", "Run the full pipeline", args=ARGS)
async def run(args, orchestrator) -> None:
    from agents.mcp_runtime import MCPRuntime
    async with MCPRuntime() as mcp_runtime:
        await _run(args, orchestrator, mcp_runtime)


async def _run(args, orchestrator, mcp_runtime) -> None:
    import json
    from datetime import datetime as _dt

    # ── Checkpoint listing ────────────────────────────────────────
    if getattr(args, "list_checkpoints", False):
        from memory.checkpoint import list_checkpoints
        from memory.store import PersistentMemory
        store = PersistentMemory()
        checkpoints = list_checkpoints(store)
        store.close()
        if not checkpoints:
            print("No checkpoints available.")
        else:
            print(f"\nAvailable checkpoints ({len(checkpoints)}):\n")
            for cp in checkpoints:
                ts = _dt.fromtimestamp(cp["created_at"]).strftime("%Y-%m-%d %H:%M") if cp.get("created_at") else cp.get("updated_at", "?")
                name = cp.get("run_name", "")
                label = name or f"Scan #{cp['scan_id']}"
                print(
                    f"  {label} | {ts} | "
                    f"{cp['pipeline_mode']} | phase={cp['phase']} | "
                    f"{cp['domains']} domains | {cp['findings']} findings | "
                    f"${cp['cost_spent']:.2f} spent"
                )
            print(f"\nResume with: python main.py full --resume-scan <name>")
        return

    from agents.pipeline import AgentPipeline
    from agents.llm_config import apply_cli_llm_overrides

    pause_points = []
    if getattr(args, "pause_at", None):
        pause_points = [p.strip() for p in args.pause_at.split(",")]

    if getattr(args, "cli_tools", False):
        override_settings(cli_tool_mode=True)
    if getattr(args, "domain_triage", False):
        override_settings(domain_triage=True)
    if getattr(args, "no_auth_testing", False):
        override_settings(auth_testing=False)
    if getattr(args, "no_browser", False):
        override_settings(enable_browser=False)
    if getattr(args, "compact", False):
        override_settings(compact_prompt=True)

    use_web = getattr(args, "web", False)
    web_port = getattr(args, "web_port", None)
    mode = getattr(args, "mode", None)

    if web_port is not None:
        override_settings(web_port=web_port)

    if getattr(args, "max_tokens", None) is not None:
        override_settings(max_response_tokens=args.max_tokens)

    if getattr(args, "keep_recent", None) is not None:
        override_settings(keep_recent=args.keep_recent)

    if getattr(args, "generous_context", False):
        override_settings(generous_context=True)

    # --profile: load methodology/target-provider/scoring/mechanisms/topology
    # as one bundle. Explicit CLI flags below still override it — a profile
    # sets DEFAULTS, not a hard lock.
    profile = None
    profile_name = getattr(args, "profile", None)
    if profile_name is None:
        profile_name = "interactive" if mode in (None, "interactive", "pentester") else mode
        if profile_name == "swarm":
            profile_name = "bugbounty"
    if profile_name:
        from profiles.loader import load_profile
        profile = load_profile(profile_name)
        if mode is None:
            mode = profile.topology.mode
    llm_config = apply_cli_llm_overrides(profile.llm, args)
    from utils.console_logging import configure_console_logging
    diagnostics = getattr(args, "diagnostics", False) or getattr(args, "verbose", False) or profile.interaction.diagnostics
    configure_console_logging(quiet=mode in ("pentester", "interactive") and not diagnostics,
                              debug=bool(diagnostics))

    if profile.mcp and mode == "lab" and profile.workflow.worker_mode != "tool":
        raise ValueError("Lab MCP requires workflow.worker_mode = tool")
    if profile.mcp and not getattr(args, "dry_run", False):
        await mcp_runtime.connect(profile.mcp)
        print(f"MCP: loaded {len(mcp_runtime.tools)} tools from {len(profile.mcp)} servers")

    # CLI flags win when explicitly given; otherwise fall back to the
    # profile's topology. "group mode" is just num_teams > 1 on the same
    # engine — there is no separate group-specific code path. The bare
    # default (no profile, no --num-teams) is 1: multi-team competition is
    # opt-in, never a side effect of an unrelated default.
    swarm_agents = args.swarm_agents if args.swarm_agents is not None else (
        profile.topology.agents_per_team if profile else 3)
    num_teams = args.num_teams if args.num_teams is not None else (
        profile.topology.num_teams if profile else 1)

    mechanisms_spec: list | None = None
    if getattr(args, "mechanisms", None) is not None:
        from memory.mechanisms import MECHANISM_REGISTRY
        requested = [n.strip() for n in args.mechanisms.split(",") if n.strip()]
        unknown = [n for n in requested if n not in MECHANISM_REGISTRY]
        if unknown:
            raise ValueError(f"Unknown --mechanisms: {unknown}. Known: {sorted(MECHANISM_REGISTRY)}")
        mechanisms_spec = requested
        override_settings(mechanisms=args.mechanisms)
    elif profile is not None:
        mechanisms_spec = profile.mechanisms

    if getattr(args, "ollama_per_agent", False):
        override_settings(ollama_per_agent=True)

    if mode == "interactive":  # compatibility alias
        mode = "pentester"
    interaction_mode = getattr(args, "interaction", None) or profile.interaction.mode
    use_web = use_web or profile.dashboard.enabled or interaction_mode == "web"
    if interaction_mode == "web":  # legacy alias, no longer a mutually exclusive interface
        interaction_mode = "terminal" if mode == "pentester" else "batch"
    args.web = use_web
    if mode == "pentester":
        if interaction_mode != "terminal":
            raise ValueError("Pentester sessions currently require terminal interaction")
        if getattr(args, "auto", False) or getattr(args, "resume", False) or getattr(args, "resume_scan", None):
            raise ValueError("Interactive mode uses the terminal; --auto and checkpoint resume require an explicit batch profile")
        if num_teams != 1 or swarm_agents != 1:
            raise ValueError("Interactive mode currently supports one pentester; use a swarm profile for teams")
        from agents.interactive import run_interactive
        await run_interactive(profile, llm_config, args, mechanisms_spec)
        return

    if mode == "swarm":
        from agents.swarm_pipeline import SwarmPipeline
        pipeline = SwarmPipeline(
            num_agents=swarm_agents,
            swarm_timeout_minutes=30,
            llm_config=llm_config,
            dry_run=getattr(args, "dry_run", False),
            auto_mode=getattr(args, "auto", False),
            pause_points=pause_points or None,
            web_bridge=None,
            target_provider=profile.target_provider if profile else None,
            methodology=profile.methodology if profile else None,
            scoring=profile.scoring if profile else None,
            mechanisms=mechanisms_spec,
            coordination=profile.coordination if profile else None,
        )
        # Multi-team competition is just num_teams > 1 on this SAME engine —
        # there is no separate "group mode" to select.
        pipeline._group_mode = num_teams > 1
        pipeline._num_teams = num_teams
    else:
        pipeline = AgentPipeline(
            llm_config=llm_config,
            dry_run=getattr(args, "dry_run", False),
            auto_mode=getattr(args, "auto", False),
            pause_points=pause_points or None,
            web_bridge=None,
            target_provider=profile.target_provider if profile else None,
            workflow=profile.workflow if profile else None,
        )

    if getattr(args, "scope_pdf_only", False):
        pipeline.scope_pdf_only = True

    if getattr(args, "keep_recent", None) is not None:
        pipeline._keep_recent = args.keep_recent

    direct_targets = None
    if getattr(args, "targets", None):
        direct_targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    program_url = getattr(args, "program_url", None)

    run_kwargs = dict(
        top_n=args.top,
        min_bounty=args.min_bounty,
        budget=args.budget,
        targets=direct_targets,
        program_url=program_url,
    )

    # ── Resume from checkpoint ────────────────────────────────
    if getattr(args, "resume", False) or getattr(args, "resume_scan", None) is not None:
        from memory.checkpoint import load_checkpoint, list_checkpoints
        resume_val = getattr(args, "resume_scan", None)
        scan_id = None
        run_name = None
        if resume_val is not None:
            try:
                scan_id = int(resume_val)
            except ValueError:
                run_name = resume_val
        checkpoint = load_checkpoint(pipeline.persistent, scan_id=scan_id, run_name=run_name)
        if checkpoint is None:
            label = run_name or (f"scan #{scan_id}" if scan_id else "latest")
            print(f"No checkpoint found ({label}).")
            available = list_checkpoints(pipeline.persistent)
            if available:
                print(f"\nAvailable checkpoints:")
                for cp in available[:5]:
                    ts = _dt.fromtimestamp(cp["created_at"]).strftime("%Y-%m-%d %H:%M") if cp.get("created_at") else "?"
                    name = cp.get("run_name", "")
                    label = name if name else f"Scan #{cp['scan_id']}"
                    print(f"  {label} | {ts} | {cp['domains']} domains | {cp['findings']} findings | ${cp['cost_spent']:.2f}")
                print(f"\nResume with: python main.py full --resume-scan <name>")
            return
        from agents.swarm_pipeline import SwarmPipeline as _SwarmCheck
        if checkpoint.pipeline_mode == "swarm" and not isinstance(pipeline, _SwarmCheck):
            pipeline = _SwarmCheck(
                num_agents=checkpoint.num_agents,
                swarm_timeout_minutes=checkpoint.swarm_timeout_minutes,
                llm_config=llm_config,
                dry_run=getattr(args, "dry_run", False),
                auto_mode=getattr(args, "auto", False),
                pause_points=pause_points or None,
                web_bridge=None,
            )
        run_kwargs["resume_checkpoint"] = checkpoint
        cp_label = checkpoint.run_name or f"scan #{checkpoint.scan_id}"
        print(
            f"Resuming {cp_label}: "
            f"{len(checkpoint.all_domains)} domains, "
            f"{len(checkpoint.shared_memory.get('findings', []))} findings, "
            f"${checkpoint.cost_spent:.2f} spent"
        )

    pipeline.interaction_mode = interaction_mode
    web_ui = None
    try:
        if use_web:
            from web import WebUI
            web_ui = WebUI(pipeline)
            pipeline.web_bridge = web_ui.bridge
            _attach_tool_hooks(pipeline, web_ui.bridge)
            await web_ui.start()
        if interaction_mode == "terminal":
            from agents.pipeline_console import PipelineConsole
            from agents.interactive import terminal_lines
            await PipelineConsole(pipeline, run_kwargs).run(terminal_lines())
        else:
            await pipeline.run(**run_kwargs)
    finally:
        if web_ui is not None:
            await web_ui.shutdown()
    print(f"\nCosts:\n{pipeline.show_costs()}")
