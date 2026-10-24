"""Tool-using worker implementations.

Each worker defines a system prompt and tool set. When an LLM with tool-call
support is available, the ToolAgent base class drives an autonomous loop.
Otherwise, each worker falls back to its deterministic ``run()`` path that
mirrors the existing hardcoded workers.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.messaging import MessageBus, TaskAssignment
from agents.tool_agent import ToolAgent, ToolCall
from agents.tools import (
    ToolRegistry,
    create_analysis_tools,
    create_browser_tools,
    create_discovery_tools,
    create_help_tool,
    create_recon_tools,
    create_scanner_tools,
)
from cost.tracker import CostTracker
from llm.base import LLMProvider
from models.finding import VulnFinding
from models.program import BountyProgram

logger = logging.getLogger(__name__)


class ReconToolWorker(ToolAgent):
    """Adaptive reconnaissance agent with tool-use."""

    name = "recon"
    capability = "adaptive reconnaissance"
    system_prompt = (
        "You are a security researcher on a bug bounty team. Your role is RECON — "
        "you map the attack surface so your teammates (scanner, analyzer) can find "
        "exploitable security vulnerabilities that earn bounties.\n\n"
        "You are NOT looking for software bugs or QA issues. You are looking for "
        "SECURITY weaknesses: exposed services, forgotten subdomains, staging "
        "environments, admin panels, technology stacks with known CVEs, "
        "infrastructure misconfigurations.\n\n"
        "You have tools for subdomain enumeration, DNS resolution, tech detection, "
        "HTTP probing, and a headless browser for JS-rendered pages.\n\n"
        "Strategy:\n"
        "- Start with certificate transparency (crtsh_lookup) and DNS bruteforce\n"
        "- Fingerprint technologies on discovered subdomains\n"
        "- Look for juicy targets: dev/staging servers, admin panels, API endpoints, "
        "forgotten services — these are where real vulnerabilities hide\n"
        "- If you find WordPress, probe /wp-admin, /wp-login.php, /wp-json/\n"
        "- Check open ports on important targets — non-standard ports often "
        "expose internal services\n"
        "- Use browser_navigate for JS-rendered pages that httpx can't read\n"
        "- Use browser_evaluate to run JS and extract structured data from pages\n"
        "- Think like a hacker doing reconnaissance before an authorized pentest\n"
        "- If you hit a CAPTCHA, bot detection, or login wall, use request_help "
        "to ask the human operator for assistance\n"
        "- When done, use write_session_memory to leave a summary for the scanner "
        "(technologies, interesting endpoints, admin panels found)\n"
        "- Report your complete findings when done\n\n"
        "Output your final findings as JSON:\n"
        '{"subdomains": [...], "technologies": [...], "open_ports": {...}, '
        '"interesting_urls": [...], "targets": [...]}'
    )

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        browser_session=None,
        help_callback=None,
        **kwargs: Any,
    ) -> None:
        tools = create_recon_tools()
        if browser_session:
            tools.merge(create_browser_tools(browser_session))
        if help_callback:
            tools.merge(create_help_tool(help_callback))
        super().__init__(bus, llm=llm, tools=tools, **kwargs)

    async def run(self, assignment: TaskAssignment) -> Any:
        """Deterministic fallback: mirrors existing ReconWorker."""
        from agents.recon import ReconAgent

        domains = assignment.parameters.get("domains", [])
        if not domains:
            return {"subdomains": [], "technologies": [], "open_ports": {}, "targets": []}
        recon = ReconAgent()
        return await recon.run(domains)

    def summarize(self, result: Any) -> str:
        if not result:
            return "Recon produced no data."
        if isinstance(result, dict):
            subs = len(result.get("subdomains", []))
            techs = len(result.get("technologies", []))
            return f"Enumerated {subs} subdomains, detected {techs} technologies."
        return str(result)[:200]


class ScanToolWorker(ToolAgent):
    """Adaptive vulnerability scanning agent with tool-use."""

    name = "scanner"
    capability = "adaptive vulnerability scanning"
    system_prompt = (
        "You are a security researcher / ethical hacker on an authorized bug bounty team. "
        "Your job is to find REAL, EXPLOITABLE SECURITY VULNERABILITIES that can be "
        "reported to bug bounty programs for cash bounties.\n\n"
        "You are NOT looking for software bugs, broken links, typos, or QA issues. "
        "You are hunting for SECURITY ISSUES: data leaks, authentication bypasses, "
        "injection flaws, exposed secrets, SSRF, open redirects, misconfigured CORS, "
        "exposed admin panels, leaked API keys — anything that compromises "
        "confidentiality, integrity, or availability.\n\n"
        "Think like a penetration tester with authorized access. Be methodical, "
        "creative, and thorough. Real bounties come from findings that other "
        "researchers miss.\n\n"
        "You have tools — granular primitives for precise, surgical vulnerability hunting.\n\n"
        "PAGE ANALYSIS:\n"
        "- http_probe: Fetch URLs with full control over method/headers\n"
        "- extract_links, extract_forms, extract_js_urls, extract_html_comments, extract_headers\n\n"
        "TARGETED TESTING:\n"
        "- check_sensitive_path: Check ONE path (e.g. /.env) with indicator\n"
        "- test_ssrf: Test a parameter for SSRF (with custom payload)\n"
        "- test_header_injection: Test CRLF injection on a parameter\n"
        "- fuzz_parameter: Fuzz a single param with injection payloads\n"
        "- regex_scan_url: Custom regex on any URL's content\n\n"
        "DOCKER TOOLS (via run_docker_tool with image='bugbounty-tools:latest'):\n"
        "- command='nmap -sV -sC target' — port/service scan\n"
        "- command='nuclei -u https://target -severity medium,high,critical -jsonl -silent' — 8000+ vuln templates\n"
        "- command='ffuf -u https://target/FUZZ -w /usr/share/wordlists/dirb/common.txt -json -s' — dir fuzzing\n"
        "- command='katana -u https://target -d 3 -jc -jsonl -silent' — web crawling\n"
        "- command='nikto -h https://target -Format json' — web server scanner\n"
        "- command='sqlmap -u \"https://target/page?id=1\" --batch' — SQL injection\n"
        "- command='wpscan --url https://target --enumerate vp,vt,u' — WordPress scanner\n"
        "- command='dig +short target A' — DNS lookup\n"
        "- command='curl -I https://target' — HTTP headers\n"
        "- command='gau target' — fetch known URLs from Wayback/CommonCrawl/OTX\n"
        "- command='dalfox url https://target/page?q=test' — XSS scanner\n"
        "- command='amass enum -d target -passive' — subdomain enumeration\n"
        "- command='arjun -u https://target/endpoint' — hidden parameter discovery\n"
        "- command='commix -u \"https://target/page?cmd=test\" --batch' — command injection\n"
        "- Any Docker image: kali, alpine, debian, etc.\n\n"
        "BATCH SCANNERS:\n"
        "- scan_js_secrets, scan_sensitive_paths, run_nuclei_templates_builtin\n"
        "- scan_subdomain_takeover, scan_cors, scan_open_redirect\n\n"
        "BROWSER (for JS-rendered pages):\n"
        "- browser_navigate, browser_get_text, browser_click, browser_type\n"
        "- browser_get_links, browser_screenshot, browser_evaluate, browser_back\n"
        "- request_help: Ask the human operator when stuck (CAPTCHA, login, etc.)\n\n"
        "SHARED MEMORY (coordination with other agents):\n"
        "- Recon data is already injected in RECON AGENT NOTES above — don't waste a tool call reading it\n"
        "- write_session_memory: Leave notes for the analyzer (what you tested, evidence quality)\n"
        "- read_session_memory: Only if you need to check notes from other agents\n\n"
        "Strategy — think like a hacker, not a script:\n"
        "0. FIRST: Review the RECON AGENT NOTES above — don't repeat work already done by recon\n"
        "1. Fingerprint: extract_headers + run_docker_tool (nmap, openssl) to understand the target\n"
        "2. Map: extract_links + extract_forms + extract_js_urls OR run_docker_tool (katana)\n"
        "3. Hunt secrets: scan_js_secrets + regex_scan_url on discovered JS\n"
        "4. Check exposure: check_sensitive_path for tech-relevant paths\n"
        "5. Test injection: fuzz_parameter on forms/params you found\n"
        "6. Deep scan: run_docker_tool (nuclei + ffuf) for thorough coverage\n"
        "7. Verify: use http_probe / run_docker_tool (curl) to confirm and build evidence\n"
        "8. IMPORTANT: Only report findings with security impact. Missing headers "
        "alone are informational. Focus on issues that could let an attacker "
        "steal data, gain access, or cause damage.\n\n"
        "Output your findings as JSON:\n"
        '[{"target": "...", "vuln_type": "...", "severity": "...", '
        '"title": "...", "description": "...", "evidence": "...", '
        '"url": "...", "confidence_score": 0.0}, ...]'
    )

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        max_concurrency: int = 10,
        browser_session=None,
        help_callback=None,
        **kwargs: Any,
    ) -> None:
        tools = create_scanner_tools()
        if browser_session:
            tools.merge(create_browser_tools(browser_session))
        if help_callback:
            tools.merge(create_help_tool(help_callback))
        super().__init__(bus, llm=llm, tools=tools, **kwargs)
        self.max_concurrency = max_concurrency

    async def run(self, assignment: TaskAssignment) -> Any:
        """Deterministic fallback: mirrors existing ScanWorker."""
        from agents.scanner import ScannerAgent

        targets = assignment.parameters.get("targets", [])
        recon_data = assignment.parameters.get("recon_data")
        if not targets:
            return []
        scanner = ScannerAgent(max_concurrency=self.max_concurrency)
        return await scanner.scan(targets, recon_data)

    def _parse_final_answer(self, text: str) -> Any:
        """Parse scanner output. If we get a list of dicts, convert to VulnFinding."""
        parsed = super()._parse_final_answer(text)
        if isinstance(parsed, list):
            findings = []
            for item in parsed:
                if isinstance(item, dict) and "target" in item and "vuln_type" in item:
                    try:
                        findings.append(VulnFinding(**item))
                    except Exception:
                        findings.append(item)
                else:
                    findings.append(item)
            return findings
        return parsed

    def summarize(self, result: Any) -> str:
        if not result:
            return "No findings from scanning."
        if isinstance(result, list):
            return f"Found {len(result)} findings."
        return str(result)[:200]


class DiscoverToolWorker(ToolAgent):
    """Adaptive program discovery agent with tool-use."""

    name = "discover"
    capability = "adaptive program discovery"
    system_prompt = (
        "You are a security researcher on a bug bounty team. Your role is TARGET SELECTION — "
        "you find the best bug bounty programs to hack for maximum payout.\n\n"
        "You are NOT looking for programs with bugs or quality issues. You are looking for "
        "programs where your team can find SECURITY VULNERABILITIES (XSS, SSRF, auth bypass, "
        "exposed secrets, etc.) and get PAID bounties.\n\n"
        "You have tools to fetch programs from HackerOne, Bugcrowd, and "
        "Intigriti, plus scan history checking.\n\n"
        "Strategy:\n"
        "- Fetch programs from all three platforms\n"
        "- Check scan history to avoid recently-scanned targets\n"
        "- Rank by ROI: high bounties + large scope + fast response = good targets\n"
        "- CRITICAL: Only recommend programs that have actual web domains in their scope_domains "
        "list. Programs with zero domains cannot be scanned — skip them.\n"
        "- Avoid programs with tiny scope or where we've already exhausted the attack surface\n"
        "- Prefer programs with many web domains (large web apps, APIs, SaaS platforms)\n"
        "- When done, use write_session_memory to leave your picks and reasoning for "
        "the recon agent\n\n"
        "Output your recommendations as JSON:\n"
        '[{"name": "...", "platform": "...", "url": "...", "min_bounty": 0, '
        '"max_bounty": 0, "scope_domains": [...], "reasoning": "..."}, ...]'
    )

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        cost_tracker: CostTracker | None = None,
        browser_session=None,
        help_callback=None,
        **kwargs: Any,
    ) -> None:
        tools = create_discovery_tools()
        if browser_session:
            tools.merge(create_browser_tools(browser_session))
        if help_callback:
            tools.merge(create_help_tool(help_callback))
        super().__init__(bus, llm=llm, tools=tools, **kwargs)
        self.cost_tracker = cost_tracker or CostTracker()
        self._fetched_programs: list[BountyProgram] = []

        # Hook into tool calls to accumulate raw programs
        self.on_tool_call = self._accumulate_programs

    def _accumulate_programs(self, tc: ToolCall) -> None:
        """Collect BountyProgram objects from discovery tool results."""
        if tc.error or tc.output is None:
            return
        if not tc.tool_name.startswith("fetch_"):
            return

        output = tc.output
        if not isinstance(output, dict):
            return

        # Prefer full BountyProgram objects (preserved by _build_fetch_result)
        full = output.pop("_full_programs", None)
        if isinstance(full, list):
            for item in full:
                if isinstance(item, BountyProgram):
                    self._fetched_programs.append(item)
            return

        # Fallback: reconstruct from slimmed dicts
        items = output.get("programs", [])
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and "name" in item and "platform" in item:
                try:
                    self._fetched_programs.append(BountyProgram(**item))
                except Exception:
                    pass

    async def execute(self, assignment: TaskAssignment):
        """Override to ensure all platforms are fetched and programs are ranked."""
        self._fetched_programs = []

        # Pre-fetch ALL platforms deterministically — don't rely on the LLM
        # to call all 4 fetch tools (it often only calls 1-2).
        import asyncio as _asyncio
        from platforms.hackerone import HackerOne
        from platforms.bugcrowd import Bugcrowd
        from platforms.intigriti import Intigriti
        from platforms.yeswehack import YesWeHack

        logger.info("Pre-fetching programs from all platforms...")
        tasks = [
            HackerOne().fetch_programs(),
            Bugcrowd().fetch_programs(),
            Intigriti().fetch_programs(),
            YesWeHack().fetch_programs(),
        ]
        results = await _asyncio.gather(*tasks, return_exceptions=True)
        platform_names = ["hackerone", "bugcrowd", "intigriti", "yeswehack"]
        for name, result in zip(platform_names, results):
            if isinstance(result, Exception):
                logger.error("Pre-fetch %s failed: %s", name, result)
                continue
            with_domains = [p for p in result if p.all_domains]
            self._fetched_programs.extend(with_domains)
            logger.info("Pre-fetched %d programs from %s (%d with domains)", len(result), name, len(with_domains))

        logger.info("Total pre-fetched: %d programs with domains", len(self._fetched_programs))

        # Skip the LLM agentic loop — just score and return.
        # The director will do the final selection with LLM reasoning.
        programs = self._parse_final_answer("")
        if isinstance(programs, list) and programs:
            from agents.base import TaskResult
            return TaskResult(
                agent_name=self.name,
                task_type=assignment.task_type,
                data=programs,
                reflection=f"Fetched and ROI-ranked {len(programs)} programs from all 4 platforms.",
            )

        # Fallback: run the LLM loop if pre-fetch somehow failed
        return await super().execute(assignment)

    def _parse_final_answer(self, text: str) -> Any:
        """Return accumulated BountyProgram objects, scored and ranked by ROI."""
        programs = self._fetched_programs
        if not programs:
            # Fallback: try to parse JSON from LLM text into BountyProgram objects
            parsed = super()._parse_final_answer(text)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "name" in item:
                        try:
                            programs.append(BountyProgram(**item))
                        except Exception:
                            pass
        if not programs:
            return text

        # Score and rank all accumulated programs using the ROI model
        from cost.roi_model import ROIModel
        from agents.program_finder import ProgramFinder, RECENCY_WINDOW_HOURS

        roi = ROIModel()
        persistent = getattr(self, "persistent_store", None)

        # Compute recency penalties
        recency: dict[str, float] = {}
        if persistent:
            finder = ProgramFinder(self.cost_tracker, persistent_store=persistent)
            recency = finder._get_recency_penalties()

        # Filter: require domains and min bounty
        programs = [p for p in programs if p.all_domains and p.max_bounty >= 50]

        for p in programs:
            penalty = recency.get(p.url, 0.0)
            roi.score_program(p, self.cost_tracker, recency_penalty=penalty)

        programs.sort(key=lambda p: p.roi_score, reverse=True)

        # Log top picks for debugging
        for i, p in enumerate(programs[:10], 1):
            summary = roi.format_roi_summary(p)
            penalty = recency.get(p.url, 0.0)
            tag = f" [recency={penalty:.1f}]" if penalty > 0 else ""
            logger.info("ROI rank #%d: %s (%s) - %s%s", i, p.name, p.platform, summary, tag)

        return programs

    async def run(self, assignment: TaskAssignment) -> Any:
        """Deterministic fallback: mirrors existing DiscoverWorker."""
        from agents.program_finder import ProgramFinder

        top_n = assignment.parameters.get("top_n", 10)
        min_bounty = assignment.parameters.get("min_bounty", 50.0)
        persistent = getattr(self, "persistent_store", None)
        finder = ProgramFinder(self.cost_tracker, min_bounty=min_bounty, persistent_store=persistent)
        return await finder.discover(top_n=top_n)

    def summarize(self, result: Any) -> str:
        if not result:
            return "No programs found."
        if isinstance(result, list):
            return f"Found {len(result)} programs."
        return str(result)[:200]


class AnalyzeToolWorker(ToolAgent):
    """Adaptive analysis agent with memory tools."""

    name = "analyzer"
    capability = "adaptive vulnerability analysis"
    system_prompt = (
        "You are a senior security researcher on a bug bounty team. Your role is "
        "TRIAGE — you evaluate vulnerability findings to decide which ones are worth "
        "reporting to bug bounty programs.\n\n"
        "You are NOT triaging software bugs. You are triaging SECURITY VULNERABILITIES. "
        "A valid finding must have SECURITY IMPACT: it allows an attacker to steal data, "
        "gain unauthorized access, escalate privileges, or cause damage. Missing headers "
        "without exploit context, informational disclosures with no impact, or theoretical "
        "risks are LOW priority.\n\n"
        "You have tools to query past findings, target history, and best "
        "vulnerability types.\n\n"
        "SHARED MEMORY (coordination with other agents):\n"
        "- read_session_memory: Check scanner notes (what was tested, evidence quality)\n"
        "- write_session_memory: Leave final analysis notes for the reporter\n\n"
        "Strategy:\n"
        "- FIRST: read_session_memory('scanner') to understand what was tested and how\n"
        "- Check each finding against past findings for duplicates — don't waste time "
        "re-reporting known issues\n"
        "- Review target history to understand what we've already found there\n"
        "- Consider which vuln types have historically earned bounties\n"
        "- Assess each finding's exploitability and real-world security impact\n"
        "- Filter out noise: focus on findings a bug bounty program would actually pay for\n\n"
        "Output your analysis as JSON:\n"
        '{"findings": [...], "duplicates_found": 0, "recommendations": "..."}'
    )

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        analysis_llm: LLMProvider | None = None,
        confidence_threshold: float = 0.3,
        help_callback=None,
        **kwargs: Any,
    ) -> None:
        tools = create_analysis_tools()
        if help_callback:
            tools.merge(create_help_tool(help_callback))
        super().__init__(bus, llm=llm, tools=tools, **kwargs)
        self.analysis_llm = analysis_llm
        self.confidence_threshold = confidence_threshold

    async def run(self, assignment: TaskAssignment) -> Any:
        """Deterministic fallback: mirrors existing AnalyzeWorker."""
        from agents.analyzer import AnalyzerAgent

        findings = assignment.parameters.get("findings", [])
        if not findings:
            return []
        if self.analysis_llm is None:
            return findings
        analyzer = AnalyzerAgent(self.analysis_llm, confidence_threshold=self.confidence_threshold)
        return await analyzer.analyze(findings)

    def summarize(self, result: Any) -> str:
        if not result:
            return "All findings filtered or none to analyze."
        if isinstance(result, list):
            return f"{len(result)} findings survived analysis."
        if isinstance(result, dict):
            findings = result.get("findings", [])
            return f"{len(findings)} findings survived analysis."
        return str(result)[:200]
