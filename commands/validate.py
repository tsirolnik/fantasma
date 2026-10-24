from __future__ import annotations

from commands.base import Arg, register

_LLM_CHOICES = ["claude", "gemini", "ollama", "openai", "vertex"]

ARGS = [
    Arg(("--scan",), dict(default=None, metavar="SCAN_ID", help="Scan ID to validate findings from (default: latest)")),
    Arg(("--budget",), dict(type=float, default=5.0, help="Budget for validation run in USD (default: $5)")),
    Arg(("--director-llm",), dict(choices=_LLM_CHOICES, default=None, help="LLM provider for the Director (oversees validator)")),
    Arg(("--director-model",), dict(default=None, help="Model override for Director")),
    Arg(("--worker-llm",), dict(choices=_LLM_CHOICES, default=None, help="LLM provider for the validator agent")),
    Arg(("--worker-model",), dict(default=None, help="Model override for validator")),
    Arg(("--web",), dict(action="store_true", help="Launch a real-time web dashboard")),
    Arg(("--web-port",), dict(type=int, default=None, help="Port for the web dashboard (default: 8080)")),
]


@register("validate", "Re-validate findings from a past scan", args=ARGS)
async def run(args, orchestrator) -> None:
    import json
    import logging

    from memory.store import PersistentMemory
    from memory.shared import SharedMemory
    from models.finding import VulnFinding, VerificationStatus
    from agents.team import AgentTeam
    from agents.validator import ValidatorAgent
    from agents.director import Director
    from agents.messaging import MessageBus
    from agents.llm_config import LLMConfigMap, AgentLLMConfig, create_llm_for_agent
    from cost.tracker import CostTracker
    from utils.browser import BrowserPool
    from config import get_settings, override_settings

    store = PersistentMemory()
    cost_tracker = CostTracker()
    settings = get_settings()

    # ── Load findings from past scan ──────────────────────────────
    scan_id_arg = getattr(args, "scan", None)
    if scan_id_arg:
        try:
            scan_id_arg = int(scan_id_arg)
        except ValueError:
            print(f"Invalid scan ID: {scan_id_arg}")
            return
        rows = store.conn.execute(
            "SELECT * FROM findings WHERE scan_id=?", (scan_id_arg,)
        ).fetchall()
        if rows:
            print(f"Scan #{scan_id_arg}: {len(rows)} findings")
    else:
        row = store.conn.execute(
            "SELECT id, target, findings_count FROM scans "
            "WHERE findings_count > 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("No past scans with findings found.")
            store.close()
            return
        scan_id_arg = row["id"]
        print(f"Using latest scan #{scan_id_arg} ({row['target']}, {row['findings_count']} findings)")
        rows = store.conn.execute(
            "SELECT * FROM findings WHERE scan_id=?", (scan_id_arg,)
        ).fetchall()

    if not rows:
        print(f"No findings found for scan #{scan_id_arg}")
        store.close()
        return

    shared_memory = SharedMemory()
    loaded = 0
    targets = set()
    for r in rows:
        try:
            data = json.loads(r["data"]) if r["data"] else {}
            finding = VulnFinding(**data)
            finding.id = r["id"]
            finding.verification_status = VerificationStatus.UNVERIFIED
            finding.verification_details = ""
            shared_memory._findings.append(finding)
            shared_memory._finding_reporters.append("past-scan")
            targets.add(finding.target)
            loaded += 1
        except Exception as e:
            logging.debug("Skipping finding #%s: %s", r["id"], e)

    all_domains = sorted(targets)
    for domain in all_domains:
        await shared_memory.post(
            "pipeline", "discovery", "target",
            f"target:{domain}", json.dumps({"domain": domain}),
        )

    print(f"\nLoaded {loaded} findings for validation:\n")
    for f in shared_memory._findings:
        print(f"  #{f.id} [{f.severity.value.upper()}] {f.vuln_type.value} — {f.target}")
        if f.title:
            print(f"     {f.title}")
    print()

    # ── LLM config ───────────────────────────────────────────────
    llm_config = LLMConfigMap()
    if getattr(args, "director_llm", None) or getattr(args, "director_model", None):
        llm_config.director = AgentLLMConfig(
            provider=args.director_llm or llm_config.director.provider,
            model=getattr(args, "director_model", None) or "",
        )
    if getattr(args, "worker_llm", None) or getattr(args, "worker_model", None):
        llm_config.default_worker = AgentLLMConfig(
            provider=args.worker_llm or llm_config.default_worker.provider,
            model=getattr(args, "worker_model", None) or "",
        )

    # ── Director + Team of 1 validator ────────────────────────────
    bus = MessageBus()
    budget = getattr(args, "budget", 5.0)

    director_llm = create_llm_for_agent("director", llm_config)
    director = Director(bus=bus, llm=director_llm) if director_llm else None

    browser_pool = None
    try:
        browser_pool = BrowserPool()
        await browser_pool.start()
    except Exception:
        logging.debug("Browser not available — validator will use HTTP tools only")

    team = AgentTeam(
        team_id="validate",
        num_agents=0,
        director=director,
        bus=bus,
        cost_tracker=cost_tracker,
        persistent=store,
        llm_config=llm_config,
        dry_run=False,
        settings=settings,
    )
    team.shared_memory = shared_memory
    team.browser_pool = browser_pool
    team._all_domains = all_domains
    team.effective_budget = budget

    worker_llm = create_llm_for_agent("validator", llm_config)
    if not worker_llm:
        print("ERROR: Could not create LLM. Check your provider config.")
        store.close()
        return

    browser_session = None
    if browser_pool:
        try:
            browser_session = await browser_pool.create_session("validator")
        except Exception:
            pass

    validator = ValidatorAgent(
        agent_id="validator",
        bus=bus,
        llm=worker_llm,
        shared_memory=shared_memory,
        browser_session=browser_session,
        cost_tracker=cost_tracker,
    )
    validator.allow_self_conclude = True
    validator.session_budget = budget
    validator._allowed_domains = targets
    if director and director.llm:
        validator.compress_llm = director.llm

    team.pentesters = [validator]

    use_web = getattr(args, "web", False)
    web_port = getattr(args, "web_port", None)
    if web_port is not None:
        override_settings(web_port=web_port)

    web_ui = None
    if use_web:
        from web import WebUI
        from types import SimpleNamespace

        team.validator = None
        team._group_mode = False
        team._team_terminated = team._terminated_agents
        team.session = SimpleNamespace(programs=[], raw_findings=[], analyzed_findings=[])
        team._save_checkpoint_now = lambda phase: None

        web_ui = WebUI(team)
        team.web_bridge = web_ui.bridge

        bridge = web_ui.bridge
        aid = validator.agent_id
        validator.on_tool_start = lambda name, tool, inp, _id=aid: bridge.on_tool_start(_id, tool, inp)
        validator.on_tool_call = lambda tc, _id=aid: bridge.on_tool_call(_id, tc)
        validator.on_llm_start = lambda name, model, msgs, tools, _id=aid: bridge.on_llm_start(_id, model, msgs, tools)
        validator.on_llm_call = lambda name, model, msgs, tools, dur, reasoning="", _id=aid: bridge.on_llm_call(_id, model, msgs, tools, dur, reasoning)
        validator.on_llm_exchange = lambda name, model, msgs, resp, dur, _id=aid: bridge.on_llm_exchange(_id, model, msgs, resp, dur)
        validator.on_agent_done = lambda name, iters, calls, dur, reason, _id=aid: bridge.on_agent_done(_id, iters, calls, dur, reason)

    findings_list = "\n".join(
        f"  #{f.id} [{f.severity.value.upper()}] {f.vuln_type.value} on {f.target}"
        + (f" — {f.title}" if f.title else "")
        for f in shared_memory._findings
    )

    assignment_context = (
        f"You are validating {loaded} findings from a past scan.\n\n"
        f"Findings to verify:\n{findings_list}\n\n"
        "For EACH finding:\n"
        "1. Read the finding details via read_findings(status='unverified')\n"
        "2. Attempt to reproduce the vulnerability using attack tools\n"
        "3. Call verify_finding() with your verdict (verified/inconclusive)\n"
        "\nWork through them one by one. You may conclude when all are checked."
    )

    print(f"Starting validation (budget: ${budget:.2f})...")
    print(f"Pentester verifies findings, Director oversees.\n")

    if web_ui:
        await web_ui.start()

    try:
        await team.run(
            targets=all_domains,
            assignment_context=assignment_context,
            budget=budget,
        )
    finally:
        if web_ui:
            await web_ui.shutdown()
        if browser_pool:
            await browser_pool.stop()

    # ── Persist verification results back to SQLite ──────────────
    for f in shared_memory._findings:
        if f.id > 0 and f.verification_status.value != "unverified":
            try:
                store.conn.execute(
                    "UPDATE findings SET data=? WHERE id=?",
                    (json.dumps(f.model_dump(mode="json")), f.id),
                )
            except Exception:
                logging.debug("Failed to persist verification for finding #%s", f.id)
    store.conn.commit()
    store.close()

    # ── Print results ─────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("VALIDATION RESULTS")
    print("=" * 60)
    verified = disproved = inconclusive = unverified_count = 0
    for f in shared_memory._findings:
        vs = f.verification_status.value
        icon = {"verified": "✅", "disproved": "❌", "inconclusive": "🤷", "unverified": "❓"}.get(vs, "?")
        print(f"\n{icon} #{f.id} [{vs.upper()}] {f.vuln_type.value} — {f.target}")
        if f.title:
            print(f"   {f.title}")
        if f.verification_details:
            print(f"   Details: {f.verification_details[:200]}")
        if vs == "verified":
            verified += 1
        elif vs == "disproved":
            disproved += 1
        elif vs == "inconclusive":
            inconclusive += 1
        else:
            unverified_count += 1

    print(f"\n{'=' * 60}")
    print(f"Total: {loaded} | Verified: {verified} | Disproved: {disproved} | Inconclusive: {inconclusive} | Unchecked: {unverified_count}")
    print(f"Cost: ${cost_tracker.get_total():.2f}")
    print(f"{'=' * 60}")
