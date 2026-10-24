"""Report generator — creates submission-ready markdown reports."""

import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template

from config import get_settings
from models.finding import VulnFinding
from models.report import BountyReport

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = """# {{ title }}
{% if verification_banner %}
> {{ verification_banner }}
{% endif %}
**Severity:** {{ severity }}
**Target:** {{ finding.target }}
**Type:** {{ finding.vuln_type.value }}{% if finding.owasp %} · **OWASP:** {{ finding.owasp.value }}{% endif %}{% if finding.cwe_id %} · **CWE-{{ finding.cwe_id }}**{% endif %}
**Verification:** {{ verification_status }}
**Date:** {{ created_at }}
**Estimated Bounty:** ${{ "%.2f"|format(estimated_bounty) }}

---

## Description

{{ description }}

## Steps to Reproduce

{% for step in steps_to_reproduce %}
{{ loop.index }}. {{ step }}
{% endfor %}

## Evidence

```
{{ finding.evidence }}
```

## Impact

{{ impact }}

## Remediation

{{ remediation }}

{% if references %}
## References

{% for ref in references %}
- {{ ref }}
{% endfor %}
{% endif %}

---

*ROI: Estimated cost ${{ "%.2f"|format(estimated_cost) }}, Expected bounty ${{ "%.2f"|format(estimated_bounty) }}, ROI {{ "%.0f"|format(roi_ratio) }}%*
"""


class ReporterAgent:
    """Generates submission-ready reports from analyzed findings."""

    def __init__(self):
        settings = get_settings()
        self.output_dir = settings.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Try to load custom template, fall back to default
        template_dir = Path("templates")
        if template_dir.exists() and (template_dir / "report.md").exists():
            env = Environment(loader=FileSystemLoader(str(template_dir)))
            self.template = env.get_template("report.md")
        else:
            self.template = Environment().from_string(DEFAULT_TEMPLATE)

    async def generate_reports(self, findings: list[VulnFinding],
                                estimated_costs: dict | None = None) -> list[BountyReport]:
        """Generate reports, gated by verification status.

        - DISPROVED findings are skipped entirely (no report written).
        - VERIFIED findings get clean reports in the output dir.
        - UNVERIFIED / INCONCLUSIVE findings are routed to a ``needs-review/``
          subdir with a warning banner — they need human review before
          submission, so they don't get mixed in with the confirmed ones.
        Verified findings are reported first.
        """
        from models.finding import VerificationStatus
        needs_review_dir = self.output_dir / "needs-review"

        def _status(f: VulnFinding) -> str:
            vs = f.verification_status
            return vs.value if hasattr(vs, "value") else str(vs)

        skipped = 0
        reports = []
        # Verified first, then the rest (excluding disproved)
        ordered = sorted(
            findings,
            key=lambda f: 0 if _status(f) == VerificationStatus.VERIFIED.value else 1,
        )
        for finding in ordered:
            status = _status(finding)
            if status == VerificationStatus.DISPROVED.value:
                skipped += 1
                logger.info("Skipping disproved finding: %s", finding.title or finding.target)
                continue

            report = self._build_report(finding, estimated_costs)
            reports.append(report)

            verified = status == VerificationStatus.VERIFIED.value
            dest_dir = self.output_dir if verified else needs_review_dir
            report.rendered_markdown = self._render(report)
            filepath = self._save_report(report, dest_dir)
            logger.info(f"Report saved [{status}]: {filepath}")

        if skipped:
            logger.info("Suppressed %d disproved finding(s) from reports", skipped)
        return reports

    def _build_report(self, finding: VulnFinding, costs: dict | None = None) -> BountyReport:
        """Build a BountyReport from a VulnFinding."""
        bounty = finding.ai_bounty_estimate or finding.estimated_bounty or self._estimate_bounty(finding)
        cost = costs.get(finding.target, 0.0) if costs else 0.0
        roi = ((bounty - cost) / cost * 100) if cost > 0 else 0.0

        return BountyReport(
            title=finding.title or f"{finding.vuln_type.value} on {finding.target}",
            severity=finding.effective_severity,
            finding=finding,
            description=finding.description,
            steps_to_reproduce=self._generate_steps(finding),
            impact=self._generate_impact(finding),
            remediation=self._generate_remediation(finding),
            references=self._get_references(finding),
            estimated_bounty=bounty,
            estimated_cost=cost,
            roi_ratio=roi,
        )

    def _generate_steps(self, finding: VulnFinding) -> list[str]:
        """Generate steps to reproduce from finding data."""
        steps = []
        if finding.url:
            steps.append(f"Navigate to: {finding.url}")

        type_steps = {
            "subdomain_takeover": [
                f"Observe that {finding.target} has a dangling CNAME record",
                "Verify the target service shows an error/unclaimed page",
                "Register the resource on the target service to claim the subdomain",
            ],
            "info_disclosure": [
                f"Send a GET request to {finding.url}",
                "Observe that sensitive information is returned in the response",
            ],
            "cors_misconfiguration": [
                f"Send a request to {finding.url} with Origin header set to an evil domain",
                "Observe that the response reflects the origin in Access-Control-Allow-Origin",
                "This allows cross-origin data theft from authenticated sessions",
            ],
            "missing_security_headers": [
                f"Send a GET request to {finding.url}",
                "Inspect the response headers",
                "Observe the absence of critical security headers",
            ],
            "open_redirect": [
                f"Navigate to {finding.url}",
                "Observe that the browser redirects to the attacker-controlled domain",
            ],
            "memory_corruption": [
                "Run the target binary with the provided crash input",
                "Observe the crash with ASAN/debugger attached",
            ],
        }
        steps.extend(type_steps.get(finding.vuln_type.value, []))
        return steps

    # Severity multipliers applied to a vuln-class median bounty (or used with
    # the severity floor below when the class has no baseline data).
    _SEVERITY_MULT = {"critical": 4.0, "high": 2.0, "medium": 1.0, "low": 0.4, "info": 0.1}
    # Fallback dollar floors per severity for vuln types with no ROI baseline.
    _SEVERITY_FLOOR = {"critical": 2000, "high": 800, "medium": 300, "low": 75, "info": 0}

    def _estimate_bounty(self, finding: VulnFinding) -> float:
        """Rough bounty estimate so reports carry a real prioritization signal.

        Uses the ROI model's per-class median bounty when available, scaled by
        the finding's effective severity; otherwise a severity-based floor.
        Heuristic only — meant for ranking, not accuracy.
        """
        sev = finding.effective_severity.value
        mult = self._SEVERITY_MULT.get(sev, 1.0)
        try:
            from cost.roi_model import VULN_CLASS_METRICS
            metrics = VULN_CLASS_METRICS.get(finding.vuln_type)
        except Exception:
            metrics = None
        if metrics:
            return round(metrics.median_bounty * mult, -1)
        return float(self._SEVERITY_FLOOR.get(sev, 0))

    def _generate_impact(self, finding: VulnFinding) -> str:
        """Generate impact statement.

        Prefer the author-written impact (filled at submit_finding time), then
        the analyzer's exploitability assessment, then a per-type fallback.
        """
        if finding.impact and finding.impact.strip():
            return finding.impact.strip()
        if finding.ai_exploitability:
            return finding.ai_exploitability

        impacts = {
            "subdomain_takeover": (
                "An attacker can claim the abandoned subdomain and serve malicious content "
                "under the organization's domain, enabling phishing, cookie theft, and "
                "reputation damage."
            ),
            "info_disclosure": (
                "Sensitive information is exposed to unauthenticated users, potentially "
                "revealing source code, credentials, or internal configuration that aids "
                "further attacks."
            ),
            "cors_misconfiguration": (
                "An attacker can read sensitive data from authenticated API endpoints "
                "by exploiting the permissive CORS policy, leading to data theft."
            ),
            "missing_security_headers": (
                "Missing security headers leave the application vulnerable to "
                "clickjacking, MIME sniffing, and XSS attacks."
            ),
            "open_redirect": (
                "An attacker can craft a link that appears to be from the legitimate "
                "domain but redirects victims to a phishing site, enabling credential theft."
            ),
            "memory_corruption": (
                "A memory corruption vulnerability that may be exploitable for arbitrary "
                "code execution, depending on the specific crash conditions."
            ),
        }
        return impacts.get(finding.vuln_type.value, "Security impact to be determined.")

    def _generate_remediation(self, finding: VulnFinding) -> str:
        """Generate remediation advice."""
        remediation = {
            "subdomain_takeover": "Remove the dangling DNS record or reclaim the resource on the target service.",
            "info_disclosure": "Remove or restrict access to the exposed file/endpoint. Ensure sensitive files are not served by the web server.",
            "cors_misconfiguration": "Implement a strict CORS whitelist. Do not reflect arbitrary origins. Avoid using wildcard (*) with credentials.",
            "missing_security_headers": "Add the missing security headers to all HTTP responses. Use a security headers configuration appropriate for your application.",
            "open_redirect": "Validate redirect URLs against a whitelist of allowed domains. Do not use user input directly in redirect locations.",
            "memory_corruption": "Fix the root cause of the memory corruption. Use memory-safe coding practices, enable ASAN in testing, and consider using memory-safe languages.",
        }
        return remediation.get(finding.vuln_type.value, "Investigate and remediate the identified vulnerability.")

    def _get_references(self, finding: VulnFinding) -> list[str]:
        """Get relevant reference links."""
        refs = {
            "subdomain_takeover": [
                "https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/02-Configuration_and_Deployment_Management_Testing/10-Test_for_Subdomain_Takeover",
            ],
            "info_disclosure": [
                "https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/02-Configuration_and_Deployment_Management_Testing/04-Review_Old_Backup_and_Unreferenced_Files_for_Sensitive_Information",
            ],
            "cors_misconfiguration": [
                "https://portswigger.net/web-security/cors",
            ],
            "missing_security_headers": [
                "https://owasp.org/www-project-secure-headers/",
            ],
            "open_redirect": [
                "https://cheatsheetseries.owasp.org/cheatsheets/Unvalidated_Redirects_and_Forwards_Cheat_Sheet.html",
            ],
        }
        return refs.get(finding.vuln_type.value, [])

    _STATUS_BANNER = {
        "verified": "",
        "unverified": "⚠️ UNVERIFIED — not independently reproduced. Review and confirm before submitting.",
        "inconclusive": "⚠️ INCONCLUSIVE — validation was ambiguous. Manual review required before submitting.",
    }

    def _render(self, report: BountyReport) -> str:
        """Render report to markdown."""
        status = report.finding.verification_status
        status = status.value if hasattr(status, "value") else str(status)
        return self.template.render(
            title=report.title,
            severity=report.severity.value.upper(),
            finding=report.finding,
            description=report.description,
            steps_to_reproduce=report.steps_to_reproduce,
            impact=report.impact,
            remediation=report.remediation,
            references=report.references,
            created_at=report.created_at.strftime("%Y-%m-%d %H:%M"),
            estimated_bounty=report.estimated_bounty,
            estimated_cost=report.estimated_cost,
            roi_ratio=report.roi_ratio,
            verification_status=status.upper(),
            verification_banner=self._STATUS_BANNER.get(status, ""),
        )

    def _save_report(self, report: BountyReport, dest_dir: Path | None = None) -> Path:
        """Save rendered report to file."""
        dest_dir = dest_dir or self.output_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_title = "".join(c if c.isalnum() or c in "-_ " else "" for c in report.title)
        safe_title = safe_title.replace(" ", "_")[:80]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{safe_title}.md"
        filepath = dest_dir / filename
        filepath.write_text(report.rendered_markdown)
        return filepath
