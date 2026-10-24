"""Information disclosure scanner — probes for exposed sensitive paths."""

import logging

from scanners.base import ScannerBase
from models.finding import VulnFinding, VulnType, Severity
from utils.http import fetch

logger = logging.getLogger(__name__)

SENSITIVE_PATHS = [
    {"path": "/.git/HEAD", "indicator": "ref:", "severity": Severity.HIGH,
     "title": "Exposed Git Repository", "desc": "Git repository metadata is publicly accessible."},
    {"path": "/.env", "indicator": "=", "severity": Severity.CRITICAL,
     "title": "Exposed .env File", "desc": "Environment file with potential secrets is accessible."},
    {"path": "/server-status", "indicator": "Apache Server Status", "severity": Severity.MEDIUM,
     "title": "Apache Server Status Exposed", "desc": "Apache mod_status is publicly accessible."},
    {"path": "/debug", "indicator": "Traceback", "severity": Severity.HIGH,
     "title": "Debug Endpoint Exposed", "desc": "Debug page with application internals is accessible."},
    {"path": "/api/swagger.json", "indicator": "swagger", "severity": Severity.LOW,
     "title": "Swagger API Documentation Exposed", "desc": "API documentation is publicly accessible."},
    {"path": "/.well-known/security.txt", "indicator": "Contact:", "severity": Severity.INFO,
     "title": "Security.txt Found", "desc": "Security contact information is available."},
    {"path": "/robots.txt", "indicator": "Disallow", "severity": Severity.INFO,
     "title": "Robots.txt with Disallow Entries", "desc": "Robots.txt reveals hidden paths."},
    {"path": "/wp-admin/", "indicator": "wp-login", "severity": Severity.INFO,
     "title": "WordPress Admin Panel", "desc": "WordPress admin login page is accessible."},
    {"path": "/phpinfo.php", "indicator": "phpinfo()", "severity": Severity.MEDIUM,
     "title": "PHP Info Page Exposed", "desc": "PHP configuration details are publicly accessible."},
    {"path": "/.DS_Store", "indicator": "\x00\x00\x00\x01Bud1", "severity": Severity.LOW,
     "title": "DS_Store File Exposed", "desc": "macOS directory metadata file is accessible."},
    {"path": "/actuator/health", "indicator": "status", "severity": Severity.MEDIUM,
     "title": "Spring Boot Actuator Exposed", "desc": "Spring Boot actuator endpoints are accessible."},
    {"path": "/graphql", "indicator": "query", "severity": Severity.LOW,
     "title": "GraphQL Endpoint Found", "desc": "GraphQL endpoint is publicly accessible."},
]


class InfoDisclosureScanner(ScannerBase):
    scanner_name = "info_disclosure"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        findings = []
        base_url = f"https://{target}" if not target.startswith("http") else target

        for check in SENSITIVE_PATHS:
            url = f"{base_url}{check['path']}"
            response = await fetch(url)
            if response is None:
                continue

            if response.status_code == 200 and check["indicator"].lower() in response.text.lower():
                # Skip info-level findings for robots.txt and security.txt
                if check["severity"] == Severity.INFO and check["path"] in (
                    "/robots.txt", "/.well-known/security.txt"
                ):
                    logger.debug(f"Found {check['path']} on {target} (info only)")
                    continue

                snippet = response.text[:500]
                findings.append(VulnFinding(
                    target=target,
                    vuln_type=VulnType.INFO_DISCLOSURE,
                    severity=check["severity"],
                    title=f"{check['title']} on {target}",
                    description=check["desc"],
                    evidence=f"URL: {url}\nResponse snippet:\n{snippet}",
                    url=url,
                    reproducible=True,
                    confidence_score=0.9,
                    estimated_bounty=200.0 if check["severity"].value in ("high", "critical") else 50.0,
                ))

        return findings
