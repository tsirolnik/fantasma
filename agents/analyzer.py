"""AI-powered vulnerability analysis agent."""

import logging

from llm.base import LLMProvider
from models.finding import VulnFinding, Severity

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """You are an expert security researcher analyzing a potential vulnerability finding from an automated scanner.

**Finding:**
- Target: {target}
- Type: {vuln_type}
- Scanner Severity: {severity}
- Title: {title}
- Description: {description}
- Evidence: {evidence}
- URL: {url}
- Confidence: {confidence}

Please analyze this finding and provide:
1. **Exploitability**: How easily can this be exploited? What's the real-world impact?
2. **False Positive Assessment**: Likelihood this is a false positive (0.0-1.0)
3. **Severity**: Your assessment (critical/high/medium/low/info)
4. **Bounty Estimate**: Expected bounty in USD based on typical bug bounty payouts
5. **Recommendation**: Should this be submitted? Brief justification.

Respond in this exact format:
EXPLOITABILITY: <your assessment>
FALSE_POSITIVE: <0.0-1.0>
SEVERITY: <critical|high|medium|low|info>
BOUNTY_ESTIMATE: <number>
RECOMMENDATION: <submit|skip> - <justification>"""


class AnalyzerAgent:
    """Analyzes raw findings with AI to filter false positives and rank by value."""

    def __init__(self, llm: LLMProvider, confidence_threshold: float = 0.3):
        self.llm = llm
        self.confidence_threshold = confidence_threshold

    async def analyze(self, findings: list[VulnFinding]) -> list[VulnFinding]:
        """Analyze findings with AI, filter false positives, rank by bounty value."""
        if not findings:
            return []

        logger.info(f"Analyzing {len(findings)} findings with AI...")
        analyzed = []

        for finding in findings:
            try:
                result = await self._analyze_finding(finding)
                analyzed.append(result)
            except Exception as e:
                logger.error(f"Failed to analyze finding {finding.title}: {e}")
                analyzed.append(finding)  # Keep unanalyzed

        # Filter by false positive threshold
        filtered = [
            f for f in analyzed
            if f.ai_false_positive_likelihood < (1.0 - self.confidence_threshold)
        ]

        logger.info(
            f"Analysis complete: {len(filtered)} findings passed "
            f"(filtered {len(analyzed) - len(filtered)} likely false positives)"
        )

        # Sort by estimated bounty value descending
        filtered.sort(key=lambda f: f.ai_bounty_estimate, reverse=True)
        return filtered

    async def _analyze_finding(self, finding: VulnFinding) -> VulnFinding:
        """Send a single finding to the LLM for analysis."""
        prompt = ANALYSIS_PROMPT.format(
            target=finding.target,
            vuln_type=finding.vuln_type.value,
            severity=finding.severity.value,
            title=finding.title,
            description=finding.description,
            evidence=finding.evidence[:1000],  # Truncate long evidence
            url=finding.url,
            confidence=finding.confidence_score,
        )

        response = await self.llm.analyze(prompt)
        self._parse_response(finding, response)
        return finding

    def _parse_response(self, finding: VulnFinding, response: str) -> None:
        """Parse the LLM's structured response into finding fields."""
        lines = response.strip().split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("EXPLOITABILITY:"):
                finding.ai_exploitability = line.split(":", 1)[1].strip()
            elif line.startswith("FALSE_POSITIVE:"):
                try:
                    finding.ai_false_positive_likelihood = float(line.split(":", 1)[1].strip())
                except ValueError:
                    finding.ai_false_positive_likelihood = 0.5
            elif line.startswith("SEVERITY:"):
                severity_str = line.split(":", 1)[1].strip().lower()
                severity_map = {
                    "critical": Severity.CRITICAL,
                    "high": Severity.HIGH,
                    "medium": Severity.MEDIUM,
                    "low": Severity.LOW,
                    "info": Severity.INFO,
                }
                finding.ai_severity = severity_map.get(severity_str)
            elif line.startswith("BOUNTY_ESTIMATE:"):
                try:
                    val = line.split(":", 1)[1].strip().replace("$", "").replace(",", "")
                    finding.ai_bounty_estimate = float(val)
                except ValueError:
                    finding.ai_bounty_estimate = 0.0
