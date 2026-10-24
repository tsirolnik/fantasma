"""AI-powered binary crash analysis."""

import logging

from fuzzing.crash_triage import TriagedCrash
from llm.base import LLMProvider
from models.finding import VulnFinding, VulnType, Severity

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """You are a security researcher analyzing a binary crash from fuzzing.

Crash Type: {crash_type}
Exploitability Estimate: {exploitability}
Stack Trace:
{stack_trace}

ASAN Report:
{asan_report}

Crash Input (first 200 bytes hex): {crash_input_hex}

Please analyze this crash and provide:
1. Root cause analysis
2. Exploitability assessment (is this exploitable? how?)
3. Severity rating (critical/high/medium/low)
4. Suggested bounty range
5. Steps to reproduce (concise)

Format your response as structured text with clear sections."""


class BinaryAnalyzer:
    """Uses LLM to analyze crash findings for exploitability."""

    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def analyze_crash(self, crash: TriagedCrash) -> VulnFinding:
        """Analyze a triaged crash with AI and return a VulnFinding."""
        crash_input_hex = crash.original.crash_input[:200].hex()
        stack_trace = "\n".join(crash.stack_frames) if crash.stack_frames else "No stack trace available"

        prompt = ANALYSIS_PROMPT.format(
            crash_type=crash.crash_type.value,
            exploitability=crash.exploitability,
            stack_trace=stack_trace,
            asan_report=crash.asan_report[:2000],
            crash_input_hex=crash_input_hex,
        )

        try:
            analysis = await self.llm.analyze(prompt)
        except Exception as e:
            logger.error(f"LLM analysis failed: {e}")
            analysis = "LLM analysis unavailable"

        severity_map = {
            "high": Severity.CRITICAL,
            "medium": Severity.HIGH,
            "low": Severity.MEDIUM,
        }
        severity = severity_map.get(crash.exploitability, Severity.MEDIUM)

        bounty_map = {"high": 5000.0, "medium": 2000.0, "low": 500.0}
        bounty = bounty_map.get(crash.exploitability, 1000.0)

        return VulnFinding(
            target=crash.original.crash_file,
            vuln_type=VulnType.MEMORY_CORRUPTION,
            severity=severity,
            title=f"Memory Corruption: {crash.crash_type.value}",
            description=analysis,
            evidence=(
                f"Crash Type: {crash.crash_type.value}\n"
                f"Stack Hash: {crash.stack_hash}\n"
                f"Stack Trace:\n{stack_trace}\n"
                f"Exploitability: {crash.exploitability}"
            ),
            reproducible=True,
            confidence_score=0.6,
            estimated_bounty=bounty,
        )

    async def analyze_crashes(self, crashes: list[TriagedCrash]) -> list[VulnFinding]:
        """Analyze multiple crashes."""
        findings = []
        for crash in crashes:
            finding = await self.analyze_crash(crash)
            findings.append(finding)
        return findings
