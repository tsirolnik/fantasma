"""Scanner orchestrator — runs all vulnerability scanners concurrently."""

import asyncio
import logging

from models.finding import VulnFinding
from scanners.subdomain_takeover import SubdomainTakeoverScanner
from scanners.info_disclosure import InfoDisclosureScanner
from scanners.cors_misconfig import CORSMisconfigScanner
from scanners.security_headers import SecurityHeadersScanner
from scanners.open_redirect import OpenRedirectScanner

logger = logging.getLogger(__name__)


class ScannerAgent:
    """Coordinates all vulnerability scanners."""

    def __init__(self, max_concurrency: int = 10):
        self.max_concurrency = max_concurrency
        self.scanners = [
            SubdomainTakeoverScanner(),
            InfoDisclosureScanner(),
            CORSMisconfigScanner(),
            SecurityHeadersScanner(),
            OpenRedirectScanner(),
        ]

    async def scan(self, targets: list[str], recon_data: dict | None = None) -> list[VulnFinding]:
        """Run all scanners against all targets concurrently."""
        logger.info(f"Scanning {len(targets)} targets with {len(self.scanners)} scanners")
        sem = asyncio.Semaphore(self.max_concurrency)
        all_findings: list[VulnFinding] = []

        async def run_scanner(scanner, target):
            async with sem:
                try:
                    findings = await scanner.scan(target, recon_data)
                    return findings
                except Exception as e:
                    logger.error(f"{scanner.scanner_name} failed on {target}: {e}")
                    return []

        tasks = []
        for target in targets:
            for scanner in self.scanners:
                tasks.append(run_scanner(scanner, target))

        results = await asyncio.gather(*tasks)
        for findings in results:
            all_findings.extend(findings)

        # Deduplicate by (target, vuln_type, url)
        all_findings = self._deduplicate(all_findings)

        # Sort by severity
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        all_findings.sort(key=lambda f: severity_order.get(f.severity.value, 5))

        logger.info(f"Scan complete: {len(all_findings)} unique findings")
        return all_findings

    def _deduplicate(self, findings: list[VulnFinding]) -> list[VulnFinding]:
        """Remove duplicate findings based on target + vuln_type + url."""
        seen = set()
        unique = []
        for f in findings:
            key = (f.target, f.vuln_type, f.url)
            if key not in seen:
                seen.add(key)
                unique.append(f)
        return unique
