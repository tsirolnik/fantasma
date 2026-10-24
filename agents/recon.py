"""Recon agent — subdomain enumeration, tech fingerprinting, port checking."""

import asyncio
import logging

import httpx

from utils.dns import resolve_a, check_domain_exists
from utils.http import fetch, fetch_json
from scanners.tech_fingerprint import TechFingerprintScanner

logger = logging.getLogger(__name__)

COMMON_SUBDOMAINS = [
    "www", "mail", "ftp", "admin", "api", "dev", "staging", "test", "beta",
    "app", "m", "mobile", "portal", "blog", "shop", "store", "cdn", "media",
    "assets", "static", "img", "images", "docs", "wiki", "help", "support",
    "status", "monitor", "dashboard", "internal", "vpn", "remote", "git",
    "jenkins", "ci", "jira", "confluence", "grafana", "kibana", "elastic",
]

COMMON_PORTS = [80, 443, 8080, 8443]


class ReconAgent:
    """Performs passive reconnaissance on target domains."""

    async def run(self, domains: list[str]) -> dict:
        """Run recon on a list of domains. Returns structured recon data."""
        recon_data = {
            "subdomains": [],
            "technologies": [],
            "open_ports": {},
            "targets": [],
        }

        for domain in domains:
            domain = domain.lstrip("*.")  # Strip wildcard prefix
            logger.info(f"Running recon on {domain}")

            # Run subdomain enumeration and tech fingerprinting concurrently
            sub_task = self._enumerate_subdomains(domain)
            tech_task = self._fingerprint_tech(domain)
            port_task = self._check_ports(domain)

            subs, techs, ports = await asyncio.gather(sub_task, tech_task, port_task)

            recon_data["subdomains"].extend(subs)
            recon_data["technologies"].extend(techs)
            recon_data["open_ports"][domain] = ports
            recon_data["targets"].append(domain)

        # Deduplicate subdomains
        recon_data["subdomains"] = list(set(recon_data["subdomains"]))
        recon_data["technologies"] = list(set(recon_data["technologies"]))

        logger.info(
            f"Recon complete: {len(recon_data['subdomains'])} subdomains, "
            f"{len(recon_data['technologies'])} technologies"
        )
        return recon_data

    async def _enumerate_subdomains(self, domain: str) -> list[str]:
        """Enumerate subdomains using crt.sh and DNS brute-force."""
        subdomains = set()
        subdomains.add(domain)

        # Certificate Transparency via crt.sh
        ct_subs = await self._crtsh_lookup(domain)
        subdomains.update(ct_subs)

        # DNS brute-force common prefixes
        dns_subs = await self._dns_bruteforce(domain)
        subdomains.update(dns_subs)

        logger.info(f"Found {len(subdomains)} subdomains for {domain}")
        return list(subdomains)

    async def _crtsh_lookup(self, domain: str) -> list[str]:
        """Query crt.sh Certificate Transparency logs."""
        subdomains = set()
        url = f"https://crt.sh/?q=%.{domain}&output=json"
        try:
            data = await fetch_json(url)
            if isinstance(data, list):
                for entry in data:
                    name = entry.get("name_value", "")
                    for sub in name.split("\n"):
                        sub = sub.strip().lstrip("*.")
                        if sub.endswith(domain) and sub:
                            subdomains.add(sub)
        except Exception as e:
            logger.debug(f"crt.sh lookup failed for {domain}: {e}")

        return list(subdomains)

    async def _dns_bruteforce(self, domain: str) -> list[str]:
        """Brute-force common subdomain prefixes."""
        found = []

        async def check(prefix: str):
            sub = f"{prefix}.{domain}"
            if await check_domain_exists(sub):
                found.append(sub)

        # Limit concurrency
        sem = asyncio.Semaphore(20)

        async def bounded_check(prefix: str):
            async with sem:
                await check(prefix)

        await asyncio.gather(*[bounded_check(p) for p in COMMON_SUBDOMAINS])
        return found

    async def _fingerprint_tech(self, domain: str) -> list[str]:
        """Detect technologies on the target."""
        recon_data: dict = {}
        scanner = TechFingerprintScanner()
        await scanner.scan(domain, recon_data)
        return recon_data.get("technologies", [])

    async def _check_ports(self, domain: str) -> list[int]:
        """Check which common web ports are open."""
        open_ports = []

        async def check_port(port: int):
            try:
                url = f"{'https' if port in (443, 8443) else 'http'}://{domain}:{port}/"
                response = await fetch(url, timeout=5)
                if response is not None:
                    open_ports.append(port)
            except Exception:
                pass

        await asyncio.gather(*[check_port(p) for p in COMMON_PORTS])
        return sorted(open_ports)
