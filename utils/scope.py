"""Scope matching — one function, used everywhere a host is checked against
a program's allowed-domains list.

Scope entries come straight from platform scrapers (platforms/hackerone.py
etc.) as either a bare domain ("example.com") or a wildcard asset exactly as
the platform's API returns it ("*.example.com") — see
BountyProgram.all_domains (models/program.py). A wildcard keeps its literal
"*." prefix in that list; this function is the one place that strips it
before matching, so every subdomain the wildcard actually covers is
recognized as in-scope.
"""
from __future__ import annotations


def host_in_scope(host: str, allowed_domains) -> bool:
    """True if `host` is covered by any entry in `allowed_domains`.

    A bare entry ("example.com") matches itself and any subdomain. A
    wildcard entry ("*.example.com") matches ONLY subdomains of example.com
    — never the bare apex itself, unless a separate bare entry also lists
    it. Programs commonly scope it exactly this way on purpose (e.g. the
    wildcard is eligible for bounty, the apex marketing site is not), so
    collapsing the two would silently waive a real scope restriction.
    """
    host = host.lower()
    for d in allowed_domains:
        d = d.lower()
        if d.startswith("*."):
            apex = d[2:]
            if host.endswith("." + apex):
                return True
        elif host == d or host.endswith("." + d):
            return True
    return False
