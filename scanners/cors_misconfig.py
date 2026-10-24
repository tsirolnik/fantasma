"""CORS misconfiguration scanner."""

import logging

from scanners.base import ScannerBase
from models.finding import VulnFinding, VulnType, Severity
from utils.http import fetch

logger = logging.getLogger(__name__)

EVIL_ORIGINS = [
    "https://evil.com",
    "null",
    "https://{target}.evil.com",  # Subdomain reflection
]


class CORSMisconfigScanner(ScannerBase):
    scanner_name = "cors_misconfig"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        findings = []
        base_url = f"https://{target}" if not target.startswith("http") else target

        # Test common API-like paths
        paths = ["/", "/api", "/api/v1"]
        if recon_data and "api_paths" in recon_data:
            paths.extend(recon_data["api_paths"])

        for path in paths:
            url = f"{base_url}{path}"
            for origin_template in EVIL_ORIGINS:
                origin = origin_template.replace("{target}", target)
                finding = await self._test_cors(url, origin, target)
                if finding:
                    findings.append(finding)
                    break  # One finding per path is enough

        return findings

    async def _test_cors(self, url: str, origin: str, target: str) -> VulnFinding | None:
        headers = {"Origin": origin}
        response = await fetch(url, headers=headers)
        if response is None:
            return None

        acao = response.headers.get("access-control-allow-origin", "")
        acac = response.headers.get("access-control-allow-credentials", "")

        if not acao:
            return None

        is_reflected = acao == origin and origin != f"https://{target}"
        is_wildcard_with_creds = acao == "*" and acac.lower() == "true"
        is_null = acao == "null" and origin == "null"

        if is_reflected or is_wildcard_with_creds or is_null:
            severity = Severity.HIGH if acac.lower() == "true" else Severity.MEDIUM
            desc_parts = []
            if is_reflected:
                desc_parts.append(f"reflects arbitrary origin ({origin})")
            if is_wildcard_with_creds:
                desc_parts.append("allows wildcard with credentials")
            if is_null:
                desc_parts.append("allows null origin")

            return VulnFinding(
                target=target,
                vuln_type=VulnType.CORS_MISCONFIG,
                severity=severity,
                title=f"CORS Misconfiguration on {url}",
                description=(
                    f"The server at {url} has a CORS misconfiguration: "
                    f"{', '.join(desc_parts)}. "
                    f"Access-Control-Allow-Credentials: {acac}."
                ),
                evidence=(
                    f"Request Origin: {origin}\n"
                    f"Access-Control-Allow-Origin: {acao}\n"
                    f"Access-Control-Allow-Credentials: {acac}"
                ),
                url=url,
                reproducible=True,
                confidence_score=0.75,
                estimated_bounty=300.0 if severity == Severity.HIGH else 150.0,
            )

        return None
