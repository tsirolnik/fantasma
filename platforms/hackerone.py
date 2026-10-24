"""HackerOne platform integration via bounty-targets-data.

Uses the hourly-updated scope dump from
https://github.com/arkadiyt/bounty-targets-data
which provides complete in-scope/out-of-scope targets, response times,
and bounty data — more complete than the public GraphQL API which misses
WILDCARD asset types and doesn't include out-of-scope data.
"""

import logging
import re
from urllib.parse import urlparse

import httpx

from models.program import BountyProgram
from platforms.base import PlatformBase

logger = logging.getLogger(__name__)

DATA_URL = (
    "https://raw.githubusercontent.com/arkadiyt/bounty-targets-data"
    "/main/data/hackerone_data.json"
)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Asset types that represent scannable web targets
_WEB_ASSET_TYPES = {"URL", "DOMAIN", "WILDCARD"}


class HackerOne(PlatformBase):
    """Fetches bug bounty programs from HackerOne via bounty-targets-data."""

    platform_name: str = "hackerone"

    async def fetch_programs(self) -> list[BountyProgram]:
        programs: list[BountyProgram] = []

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.get(
                    DATA_URL,
                    headers={"User-Agent": BROWSER_UA},
                )
                response.raise_for_status()
                entries = response.json()

            if not isinstance(entries, list):
                logger.warning("HackerOne data is not a list")
                return []

            for entry in entries:
                program = self._parse_program(entry)
                if program is not None:
                    programs.append(program)

        except httpx.HTTPStatusError as exc:
            logger.error("HackerOne data HTTP error %s", exc.response.status_code)
        except httpx.RequestError as exc:
            logger.error("HackerOne data request failed: %s", exc)
        except Exception:
            logger.exception("Unexpected error fetching HackerOne data")

        logger.info("Fetched %d programs from HackerOne", len(programs))
        return programs

    def _parse_program(self, entry: dict) -> BountyProgram | None:
        try:
            name = entry.get("name", "")
            if not name:
                return None

            if not entry.get("offers_bounties"):
                return None

            if entry.get("submission_state") != "open":
                return None

            handle = entry.get("handle", "")
            url = entry.get("url") or f"https://hackerone.com/{handle}"

            targets = entry.get("targets") or {}
            in_scope = targets.get("in_scope") or []
            out_of_scope_entries = targets.get("out_of_scope") or []

            scope_domains, scope_wildcards = self._extract_scopes(in_scope)
            out_of_scope = self._extract_out_of_scope(out_of_scope_entries)

            # Bounty range — bounty-targets-data doesn't have explicit amounts
            # for H1, so estimate from max_severity of scope targets + program signals.
            # Scope enrichment phase will read the real numbers from the program page.
            min_bounty, max_bounty = self._estimate_bounty(entry, in_scope)

            # Response time from efficiency percentage
            efficiency = entry.get("response_efficiency_percentage") or 0
            response_time_days = max(1.0, 30.0 * (1 - efficiency / 100))

            # Build scope summary
            scope_lines = []
            if in_scope:
                scope_lines.append("In-scope targets:")
                for t in in_scope[:30]:
                    identifier = t.get("asset_identifier") or ""
                    atype = t.get("asset_type") or ""
                    eligible = "bounty" if t.get("eligible_for_bounty") else "no bounty"
                    severity = t.get("max_severity") or ""
                    instruction = t.get("instruction") or ""
                    line = f"  [{atype}] {identifier} ({eligible}, {severity})"
                    if instruction:
                        line += f" — {instruction[:100]}"
                    scope_lines.append(line)
            if out_of_scope_entries:
                scope_lines.append("Out of scope:")
                for t in out_of_scope_entries[:20]:
                    identifier = t.get("asset_identifier") or ""
                    scope_lines.append(f"  {identifier}")

            return BountyProgram(
                name=name,
                platform=self.platform_name,
                url=url,
                scope_domains=scope_domains,
                scope_wildcards=scope_wildcards,
                min_bounty=min_bounty,
                max_bounty=max_bounty,
                out_of_scope=out_of_scope,
                response_time_days=response_time_days,
                scope_raw_text="\n".join(scope_lines) if scope_lines else "",
                active=True,
            )
        except Exception:
            logger.exception("Failed to parse HackerOne entry: %s", entry.get("name", "<unknown>"))
            return None

    @staticmethod
    def _estimate_bounty(entry: dict, in_scope: list[dict]) -> tuple[float, float]:
        """Estimate bounty range from scope severity + program signals.

        bounty-targets-data doesn't include explicit bounty amounts for H1.
        We estimate based on highest max_severity in scope + program quality signals.
        The scope enrichment phase will read real numbers from the program page.
        """
        # Find highest severity among bounty-eligible targets
        severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "none": 0}
        max_sev = 0
        for t in in_scope:
            if t.get("eligible_for_bounty"):
                sev = severity_rank.get(t.get("max_severity", "none"), 0)
                max_sev = max(max_sev, sev)

        if max_sev == 0:
            return 0.0, 0.0

        # Base estimates by severity (conservative)
        bounty_by_sev = {1: (50, 500), 2: (100, 2000), 3: (500, 5000), 4: (1000, 10000)}
        min_bounty, max_bounty = bounty_by_sev.get(max_sev, (50, 500))

        # Boost for managed programs (typically higher payouts)
        if entry.get("managed_program"):
            max_bounty = int(max_bounty * 1.5)

        return float(min_bounty), float(max_bounty)

    @staticmethod
    def _extract_scopes(in_scope: list[dict]) -> tuple[list[str], list[str]]:
        """Extract domain and wildcard scope entries from in_scope targets."""
        scope_domains: list[str] = []
        scope_wildcards: list[str] = []

        for target in in_scope:
            asset_type = target.get("asset_type") or ""
            identifier = target.get("asset_identifier") or ""
            eligible = target.get("eligible_for_bounty", False)

            if not identifier or not eligible:
                continue

            if asset_type not in _WEB_ASSET_TYPES:
                continue

            domain = _extract_domain(identifier)
            if not domain:
                continue

            if domain.startswith("*."):
                scope_wildcards.append(domain)
            else:
                scope_domains.append(domain)

        return scope_domains, scope_wildcards

    @staticmethod
    def _extract_out_of_scope(out_of_scope: list[dict]) -> list[str]:
        """Extract out-of-scope entries as strings."""
        result = []
        for target in out_of_scope:
            identifier = target.get("asset_identifier") or ""
            if identifier:
                result.append(identifier)
        return result


def _extract_domain(uri: str) -> str:
    """Extract a domain from a URI/identifier string."""
    uri = uri.strip()
    if not uri:
        return ""

    if uri.startswith("*."):
        return uri

    if "://" in uri:
        try:
            parsed = urlparse(uri)
            host = parsed.hostname or ""
            return host
        except Exception:
            return ""

    if "/" in uri:
        uri = uri.split("/")[0]

    if re.match(r"^[\w*][\w.-]+\.[a-zA-Z]{2,}$", uri):
        return uri

    return ""
