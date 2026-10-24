"""YesWeHack platform integration via bounty-targets-data.

Uses the hourly-updated scope dump from
https://github.com/arkadiyt/bounty-targets-data
which provides full in-scope/out-of-scope targets and bounty ranges.
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
    "/main/data/yeswehack_data.json"
)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class YesWeHack(PlatformBase):
    """Fetches bug bounty programs from YesWeHack via bounty-targets-data."""

    platform_name: str = "yeswehack"

    async def fetch_programs(self) -> list[BountyProgram]:
        programs: list[BountyProgram] = []

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    DATA_URL,
                    headers={"User-Agent": BROWSER_UA},
                )
                response.raise_for_status()
                entries = response.json()

            if not isinstance(entries, list):
                logger.warning("YesWeHack data is not a list")
                return []

            for entry in entries:
                program = self._parse_program(entry)
                if program is not None:
                    programs.append(program)

        except httpx.HTTPStatusError as exc:
            logger.error("YesWeHack data HTTP error %s", exc.response.status_code)
        except httpx.RequestError as exc:
            logger.error("YesWeHack data request failed: %s", exc)
        except Exception:
            logger.exception("Unexpected error fetching YesWeHack data")

        logger.info("Fetched %d programs from YesWeHack", len(programs))
        return programs

    def _parse_program(self, entry: dict) -> BountyProgram | None:
        try:
            name = entry.get("name", "")
            if not name:
                return None

            if entry.get("disabled"):
                return None

            program_id = entry.get("id", "")
            url = f"https://yeswehack.com/programs/{program_id}" if program_id else ""

            min_bounty = float(entry.get("min_bounty") or 0)
            max_bounty = float(entry.get("max_bounty") or 0)

            # Skip programs with no bounty
            if max_bounty <= 0 and min_bounty <= 0:
                return None

            targets = entry.get("targets") or {}
            in_scope = targets.get("in_scope") or []
            out_of_scope_entries = targets.get("out_of_scope") or []

            scope_domains, scope_wildcards = self._extract_scopes(in_scope)
            out_of_scope = self._extract_out_of_scope(out_of_scope_entries)

            # Build scope summary
            scope_lines = []
            if in_scope:
                scope_lines.append("In-scope targets:")
                for t in in_scope[:30]:
                    target = t.get("target") or ""
                    ttype = t.get("type") or ""
                    scope_lines.append(f"  [{ttype}] {target}")
            if out_of_scope_entries:
                scope_lines.append("Out of scope:")
                for t in out_of_scope_entries[:20]:
                    target = t.get("target") or ""
                    scope_lines.append(f"  {target}")

            return BountyProgram(
                name=name,
                platform=self.platform_name,
                url=url,
                scope_domains=scope_domains,
                scope_wildcards=scope_wildcards,
                min_bounty=min_bounty,
                max_bounty=max_bounty,
                out_of_scope=out_of_scope,
                scope_raw_text="\n".join(scope_lines) if scope_lines else "",
                active=True,
            )
        except Exception:
            logger.exception("Failed to parse YesWeHack entry: %s", entry.get("name", "<unknown>"))
            return None

    @staticmethod
    def _extract_scopes(in_scope: list[dict]) -> tuple[list[str], list[str]]:
        """Extract domain and wildcard scope entries from in_scope targets."""
        scope_domains: list[str] = []
        scope_wildcards: list[str] = []

        for target in in_scope:
            ttype = (target.get("type") or "").lower()

            # Only extract domains from web-relevant target types
            if ttype not in ("web-application", "api", "url", ""):
                continue

            uri = target.get("target") or ""
            if not uri:
                continue

            domain = _extract_domain(uri)
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
            uri = target.get("target") or ""
            if uri:
                result.append(uri)
        return result


def _extract_domain(uri: str) -> str:
    """Extract a domain from a URI string."""
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
