"""Open redirect scanner — tests common redirect parameters."""

import logging
from urllib.parse import urlencode

from scanners.base import ScannerBase
from models.finding import VulnFinding, VulnType, Severity
from utils.http import fetch

logger = logging.getLogger(__name__)

REDIRECT_PARAMS = ["url", "redirect", "next", "return", "returnTo", "return_url",
                    "redirect_uri", "redirect_url", "goto", "destination", "redir",
                    "continue", "target", "forward", "out", "view", "ref"]

EVIL_URL = "https://evil.com/pwned"


class OpenRedirectScanner(ScannerBase):
    scanner_name = "open_redirect"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        findings = []
        base_url = f"https://{target}" if not target.startswith("http") else target

        # Collect paths that commonly have redirects
        paths = ["/login", "/logout", "/auth", "/signin", "/signup", "/redirect", "/"]
        if recon_data and "redirect_paths" in recon_data:
            paths.extend(recon_data["redirect_paths"])

        for path in paths:
            for param in REDIRECT_PARAMS:
                finding = await self._test_redirect(base_url, path, param, target)
                if finding:
                    findings.append(finding)
                    break  # One finding per path is sufficient

        return findings

    async def _test_redirect(
        self, base_url: str, path: str, param: str, target: str
    ) -> VulnFinding | None:
        test_url = f"{base_url}{path}?{urlencode({param: EVIL_URL})}"
        response = await fetch(test_url, follow_redirects=False)
        if response is None:
            return None

        location = response.headers.get("location", "")
        is_redirect = response.status_code in (301, 302, 303, 307, 308)

        if is_redirect and "evil.com" in location:
            return VulnFinding(
                target=target,
                vuln_type=VulnType.OPEN_REDIRECT,
                severity=Severity.MEDIUM,
                title=f"Open Redirect via {param} parameter on {path}",
                description=(
                    f"The endpoint {base_url}{path} redirects to an external domain "
                    f"when the '{param}' parameter is set to an attacker-controlled URL."
                ),
                evidence=(
                    f"Request: GET {test_url}\n"
                    f"Response Status: {response.status_code}\n"
                    f"Location Header: {location}"
                ),
                url=test_url,
                reproducible=True,
                confidence_score=0.85,
                estimated_bounty=200.0,
            )

        return None
