"""Subdomain takeover detection via dangling CNAME records."""

import logging

from scanners.base import ScannerBase
from models.finding import VulnFinding, VulnType, Severity
from utils.dns import resolve_cname
from utils.http import fetch

logger = logging.getLogger(__name__)

# Services known to be vulnerable to subdomain takeover
FINGERPRINTS = {
    "s3.amazonaws.com": {
        "service": "AWS S3",
        "indicators": ["NoSuchBucket", "The specified bucket does not exist"],
    },
    "herokuapp.com": {
        "service": "Heroku",
        "indicators": ["No such app", "no-such-app"],
    },
    "github.io": {
        "service": "GitHub Pages",
        "indicators": ["There isn't a GitHub Pages site here"],
    },
    "azurewebsites.net": {
        "service": "Azure",
        "indicators": ["404 Web Site not found"],
    },
    "cloudfront.net": {
        "service": "AWS CloudFront",
        "indicators": ["Bad request", "ERROR: The request could not be satisfied"],
    },
    "pantheonsite.io": {
        "service": "Pantheon",
        "indicators": ["404 error unknown site"],
    },
    "zendesk.com": {
        "service": "Zendesk",
        "indicators": ["Help Center Closed"],
    },
    "ghost.io": {
        "service": "Ghost",
        "indicators": ["Site not found"],
    },
}


class SubdomainTakeoverScanner(ScannerBase):
    scanner_name = "subdomain_takeover"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        findings = []
        subdomains = recon_data.get("subdomains", [target]) if recon_data else [target]

        for subdomain in subdomains:
            finding = await self._check_subdomain(subdomain)
            if finding:
                findings.append(finding)

        return findings

    async def _check_subdomain(self, subdomain: str) -> VulnFinding | None:
        cname = await resolve_cname(subdomain)
        if not cname:
            return None

        for pattern, info in FINGERPRINTS.items():
            if pattern in cname:
                logger.info(f"Potential takeover: {subdomain} -> {cname} ({info['service']})")
                response = await fetch(f"https://{subdomain}", follow_redirects=False)
                if response is None:
                    response = await fetch(f"http://{subdomain}", follow_redirects=False)

                body = response.text if response else ""
                for indicator in info["indicators"]:
                    if indicator.lower() in body.lower():
                        return VulnFinding(
                            target=subdomain,
                            vuln_type=VulnType.SUBDOMAIN_TAKEOVER,
                            severity=Severity.HIGH,
                            title=f"Subdomain Takeover via {info['service']}",
                            description=(
                                f"{subdomain} has a dangling CNAME to {cname} "
                                f"({info['service']}). The target service returns an "
                                f"error indicating it can be claimed."
                            ),
                            evidence=f"CNAME: {cname}\nHTTP Response: {indicator}",
                            url=f"https://{subdomain}",
                            reproducible=True,
                            confidence_score=0.85,
                            estimated_bounty=500.0,
                        )
        return None
