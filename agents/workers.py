"""Worker agents — wrap existing agents with LLM-powered reflection and communication."""

from __future__ import annotations

import logging
from typing import Any

from agents.base import BaseAgent
from agents.messaging import MessageBus, TaskAssignment
from agents.program_finder import ProgramFinder
from agents.recon import ReconAgent
from agents.scanner import ScannerAgent
from agents.analyzer import AnalyzerAgent
from agents.reporter import ReporterAgent
from cost.tracker import CostTracker
from llm.base import LLMProvider
from models.finding import VulnFinding

logger = logging.getLogger(__name__)


class DiscoverWorker(BaseAgent):
    """Wraps ProgramFinder with LLM-powered reflection."""

    name = "discover"
    capability = "program discovery and ROI ranking"

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        super().__init__(bus, llm)
        self.cost_tracker = cost_tracker or CostTracker()

    async def run(self, assignment: TaskAssignment) -> list:
        top_n = assignment.parameters.get("top_n", 10)
        min_bounty = assignment.parameters.get("min_bounty", 50.0)
        max_response_days = assignment.parameters.get("max_response_days", 30.0)

        finder = ProgramFinder(
            self.cost_tracker,
            min_bounty=min_bounty,
            max_response_days=max_response_days,
        )
        programs = await finder.discover(top_n=top_n)

        if not programs:
            self.raise_concern(
                "No programs found matching the criteria. "
                "Consider lowering min_bounty or increasing max_response_days.",
                priority="critical",
            )
        elif all(p.competition_score > 0.8 for p in programs):
            self.raise_concern(
                f"All {len(programs)} programs have high competition scores (>0.8). "
                "It may be difficult to find unpatched vulnerabilities. "
                "Consider targeting newer or niche programs.",
                priority="warning",
            )

        return programs

    def _build_reflect_prompt(self, result: Any, assignment: TaskAssignment) -> str:
        programs = result or []
        if not programs:
            return (
                "You are the discover agent. You found zero programs. "
                "What might have gone wrong? Any recommendations?"
            )

        summaries = []
        for p in programs[:10]:
            summaries.append(
                f"- {p.name} ({p.platform}): ${p.min_bounty}-${p.max_bounty}, "
                f"ROI={p.roi_score:.2f}, competition={p.competition_score:.2f}, "
                f"response_time={p.response_time_days}d, scope={len(p.all_domains)} domains"
            )

        return (
            f"You are the discover agent. You just discovered {len(programs)} programs.\n\n"
            f"Programs:\n" + "\n".join(summaries) + "\n\n"
            "Analyze: Which look most promising? Any red flags (high competition, "
            "low bounties, slow response times)? What's your recommendation for "
            "which programs to target first and why?"
        )

    def summarize(self, result: Any) -> str:
        if not result:
            return "No programs found."
        programs = result
        top = programs[0]
        return (
            f"Found {len(programs)} programs. "
            f"Top pick: {top.name} (ROI={top.roi_score:.2f}, "
            f"${top.min_bounty}-${top.max_bounty})."
        )


class ReconWorker(BaseAgent):
    """Wraps ReconAgent with LLM-powered reflection."""

    name = "recon"
    capability = "subdomain enumeration, tech fingerprinting, and reconnaissance"

    async def run(self, assignment: TaskAssignment) -> dict:
        domains = assignment.parameters.get("domains", [])
        if not domains:
            self.raise_concern(
                "No domains provided for recon.", priority="critical"
            )
            return {"subdomains": [], "technologies": [], "open_ports": {}, "targets": []}

        recon = ReconAgent()
        recon_data = await recon.run(domains)

        subs = recon_data.get("subdomains", [])
        techs = recon_data.get("technologies", [])

        if not subs or subs == domains:
            self.raise_concern(
                f"Found no additional subdomains for {', '.join(domains)}. "
                "The target may have very limited attack surface.",
                priority="warning",
            )

        if techs:
            self.observe(
                f"Detected technologies: {', '.join(techs[:15])}. "
                "Scanner should prioritize relevant checks.",
                data={"technologies": techs},
            )

        return recon_data

    def _build_reflect_prompt(self, result: Any, assignment: TaskAssignment) -> str:
        data = result or {}
        subs = data.get("subdomains", [])
        techs = data.get("technologies", [])
        ports = data.get("open_ports", {})
        domains = assignment.parameters.get("domains", [])

        return (
            f"You are the recon agent. You enumerated {len(subs)} subdomains "
            f"and detected {len(techs)} technologies for {', '.join(domains)}.\n\n"
            f"Subdomains (sample): {', '.join(subs[:20])}\n"
            f"Technologies: {', '.join(techs[:20])}\n"
            f"Open ports: {ports}\n\n"
            "What's the attack surface like? Any interesting findings "
            "(dev/staging environments, old software, exposed admin panels)? "
            "Concerns about target accessibility (WAFs, CDNs)?"
        )

    def summarize(self, result: Any) -> str:
        if not result:
            return "Recon produced no data."
        data = result
        subs = len(data.get("subdomains", []))
        techs = len(data.get("technologies", []))
        return f"Enumerated {subs} subdomains, detected {techs} technologies."


class ScanWorker(BaseAgent):
    """Wraps ScannerAgent with LLM-powered reflection and ReconWorker consultation."""

    name = "scanner"
    capability = "vulnerability scanning across multiple scanner types"

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        max_concurrency: int = 10,
    ) -> None:
        super().__init__(bus, llm)
        self.max_concurrency = max_concurrency

    async def run(self, assignment: TaskAssignment) -> list[VulnFinding]:
        targets = assignment.parameters.get("targets", [])
        recon_data = assignment.parameters.get("recon_data")

        if not targets:
            self.raise_concern("No targets provided for scanning.", priority="critical")
            return []

        # Consult recon worker about tech stack
        tech_info = await self.consult(
            "recon",
            f"What specific technologies did you detect on these targets: "
            f"{', '.join(targets[:5])}? I want to prioritize the right scanners.",
        )
        if tech_info:
            self.observe(
                f"Recon reports tech stack: {tech_info[:200]}",
            )

        scanner = ScannerAgent(max_concurrency=self.max_concurrency)
        findings = await scanner.scan(targets, recon_data)

        # Check for signs of blocking
        if not findings and len(targets) > 3:
            self.raise_concern(
                f"Zero findings across {len(targets)} targets. "
                "This could indicate aggressive WAF/rate-limiting blocking our scans, "
                "or the targets may genuinely have good security posture. "
                "Recommend manual verification before moving on.",
                priority="warning",
            )

        return findings

    def _build_reflect_prompt(self, result: Any, assignment: TaskAssignment) -> str:
        findings = result or []
        targets = assignment.parameters.get("targets", [])

        if not findings:
            return (
                f"You are the scanner agent. You scanned {len(targets)} targets "
                "but found zero findings. Assess: Was this expected? Any signs of blocking?"
            )

        severity_counts: dict[str, int] = {}
        for f in findings:
            sev = f.effective_severity.value
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        finding_summaries = []
        for f in findings[:10]:
            finding_summaries.append(
                f"- [{f.effective_severity.value.upper()}] {f.title} on {f.target}"
            )

        return (
            f"You are the scanner agent. You found {len(findings)} findings "
            f"on {len(targets)} targets.\n\n"
            f"Severity breakdown: {severity_counts}\n"
            f"Top findings:\n" + "\n".join(finding_summaries) + "\n\n"
            "Assess quality: How many look like real vulns vs noise? "
            "Any critical findings worth escalating immediately? "
            "Any concerns about scan coverage?"
        )

    def summarize(self, result: Any) -> str:
        if not result:
            return "No findings from scanning."
        findings = result
        severity_counts: dict[str, int] = {}
        for f in findings:
            sev = f.effective_severity.value
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
        return f"Found {len(findings)} findings: {severity_counts}."


class AnalyzeWorker(BaseAgent):
    """Wraps AnalyzerAgent with LLM-powered reflection."""

    name = "analyzer"
    capability = "AI-powered vulnerability analysis and false-positive filtering"

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        analysis_llm: LLMProvider | None = None,
        confidence_threshold: float = 0.3,
    ) -> None:
        super().__init__(bus, llm)
        # The analysis LLM is used by the underlying AnalyzerAgent
        # The worker's own LLM is for reflection/communication
        self.analysis_llm = analysis_llm
        self.confidence_threshold = confidence_threshold

    async def run(self, assignment: TaskAssignment) -> list[VulnFinding]:
        findings = assignment.parameters.get("findings", [])
        if not findings:
            self.observe("No findings to analyze.")
            return []

        if self.analysis_llm is None:
            self.raise_concern(
                f"No analysis LLM configured. Returning {len(findings)} findings "
                "unfiltered. AI analysis requires an LLM provider.",
                priority="warning",
            )
            return findings

        analyzer = AnalyzerAgent(
            self.analysis_llm,
            confidence_threshold=self.confidence_threshold,
        )
        analyzed = await analyzer.analyze(findings)

        filtered_count = len(findings) - len(analyzed)
        if filtered_count == len(findings):
            self.raise_concern(
                f"All {len(findings)} findings were filtered as likely false positives. "
                "Either the scanner produced low-quality results or the threshold is too strict.",
                priority="warning",
            )

        return analyzed

    def _build_reflect_prompt(self, result: Any, assignment: TaskAssignment) -> str:
        analyzed = result or []
        original_count = len(assignment.parameters.get("findings", []))

        if not analyzed:
            return (
                f"You are the analyzer agent. All {original_count} findings were "
                "filtered. What does this tell us about the scan quality?"
            )

        total_bounty = sum(f.ai_bounty_estimate for f in analyzed)
        high_confidence = [f for f in analyzed if f.ai_false_positive_likelihood < 0.3]

        finding_summaries = []
        for f in analyzed[:8]:
            finding_summaries.append(
                f"- [{f.effective_severity.value.upper()}] {f.title}: "
                f"FP={f.ai_false_positive_likelihood:.1%}, "
                f"bounty=${f.ai_bounty_estimate:.0f}"
            )

        return (
            f"You are the analyzer agent. You analyzed {original_count} findings, "
            f"{len(analyzed)} survived filtering.\n\n"
            f"High-confidence findings: {len(high_confidence)}\n"
            f"Estimated total bounty: ${total_bounty:.0f}\n\n"
            f"Findings:\n" + "\n".join(finding_summaries) + "\n\n"
            "What's the overall quality? Estimated total bounty potential? "
            "Any findings that need manual review before submission?"
        )

    def summarize(self, result: Any) -> str:
        if not result:
            return "All findings filtered or none to analyze."
        analyzed = result
        total_bounty = sum(f.ai_bounty_estimate for f in analyzed)
        return (
            f"{len(analyzed)} findings survived analysis. "
            f"Estimated bounty: ${total_bounty:.0f}."
        )


class ReportWorker(BaseAgent):
    """Wraps ReporterAgent with LLM-powered reflection."""

    name = "reporter"
    capability = "generating submission-ready vulnerability reports"

    async def run(self, assignment: TaskAssignment) -> list:
        findings = assignment.parameters.get("findings", [])
        estimated_costs = assignment.parameters.get("estimated_costs")

        if not findings:
            self.observe("No findings to report on.")
            return []

        reporter = ReporterAgent()
        reports = await reporter.generate_reports(findings, estimated_costs)
        return reports

    def _build_reflect_prompt(self, result: Any, assignment: TaskAssignment) -> str:
        reports = result or []
        if not reports:
            return "You are the reporter agent. No reports generated. Was this expected?"

        report_summaries = []
        for r in reports[:10]:
            report_summaries.append(
                f"- {r.title} [{r.severity.value.upper()}]: "
                f"bounty=${r.estimated_bounty:.0f}, ROI={r.roi_ratio:.0f}%"
            )

        return (
            f"You are the reporter agent. You generated {len(reports)} reports.\n\n"
            f"Reports:\n" + "\n".join(report_summaries) + "\n\n"
            "Rate the overall submission quality. Any reports that might need "
            "human review before submission? Which reports have the best ROI?"
        )

    def summarize(self, result: Any) -> str:
        if not result:
            return "No reports generated."
        reports = result
        total_bounty = sum(r.estimated_bounty for r in reports)
        return f"Generated {len(reports)} reports. Total estimated bounty: ${total_bounty:.0f}."
