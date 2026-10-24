"""Missing security headers scanner."""

import logging

from scanners.base import ScannerBase
from models.finding import VulnFinding, VulnType, Severity
from utils.http import fetch

logger = logging.getLogger(__name__)

REQUIRED_HEADERS = [
    {
        "header": "strict-transport-security",
        "name": "Strict-Transport-Security (HSTS)",
        "severity": Severity.LOW,
        "desc": "HSTS is not set, allowing potential downgrade attacks.",
    },
    {
        "header": "content-security-policy",
        "name": "Content-Security-Policy",
        "severity": Severity.MEDIUM,
        "desc": "CSP is not set, increasing risk of XSS attacks.",
    },
    {
        "header": "x-frame-options",
        "name": "X-Frame-Options",
        "severity": Severity.LOW,
        "desc": "X-Frame-Options is not set, allowing potential clickjacking.",
    },
    {
        "header": "x-content-type-options",
        "name": "X-Content-Type-Options",
        "severity": Severity.LOW,
        "desc": "X-Content-Type-Options is not set, allowing MIME type sniffing.",
    },
    {
        "header": "x-xss-protection",
        "name": "X-XSS-Protection",
        "severity": Severity.INFO,
        "desc": "X-XSS-Protection is not set (legacy but still useful for older browsers).",
    },
    {
        "header": "permissions-policy",
        "name": "Permissions-Policy",
        "severity": Severity.INFO,
        "desc": "Permissions-Policy is not set, browser features are not restricted.",
    },
]


class SecurityHeadersScanner(ScannerBase):
    scanner_name = "security_headers"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        base_url = f"https://{target}" if not target.startswith("http") else target
        response = await fetch(base_url)
        if response is None:
            return []

        findings = []
        missing = []

        for check in REQUIRED_HEADERS:
            if check["header"] not in response.headers:
                if check["severity"] in (Severity.INFO,):
                    continue  # Skip info-level missing headers
                missing.append(check)

        if not missing:
            return []

        # Report missing security headers as a single finding
        header_names = [h["name"] for h in missing]
        worst_severity = max(missing, key=lambda h: list(Severity).index(h["severity"]))["severity"]
        details = "\n".join(f"- {h['name']}: {h['desc']}" for h in missing)

        findings.append(VulnFinding(
            target=target,
            vuln_type=VulnType.MISSING_HEADERS,
            severity=worst_severity,
            title=f"Missing Security Headers on {target}",
            description=(
                f"The following security headers are missing:\n{details}"
            ),
            evidence=(
                f"URL: {base_url}\n"
                f"Missing headers: {', '.join(header_names)}\n"
                f"Present headers: {dict(response.headers)}"
            ),
            url=base_url,
            reproducible=True,
            confidence_score=0.95,
            estimated_bounty=50.0,
        ))

        return findings
