"""DNS resolution helpers."""

import logging
from typing import Optional

import dns.resolver
import dns.asyncresolver
import dns.exception

logger = logging.getLogger(__name__)


async def resolve_cname(domain: str) -> Optional[str]:
    """Resolve CNAME record for a domain. Returns the CNAME target or None."""
    try:
        resolver = dns.asyncresolver.Resolver()
        resolver.timeout = 5
        resolver.lifetime = 10
        answers = await resolver.resolve(domain, "CNAME")
        for rdata in answers:
            return str(rdata.target).rstrip(".")
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.exception.Timeout):
        return None
    except Exception as e:
        logger.debug(f"CNAME lookup failed for {domain}: {e}")
        return None


async def resolve_a(domain: str) -> list[str]:
    """Resolve A records for a domain."""
    try:
        resolver = dns.asyncresolver.Resolver()
        resolver.timeout = 5
        resolver.lifetime = 10
        answers = await resolver.resolve(domain, "A")
        return [str(rdata.address) for rdata in answers]
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.exception.Timeout):
        return []
    except Exception as e:
        logger.debug(f"A lookup failed for {domain}: {e}")
        return []


async def check_domain_exists(domain: str) -> bool:
    """Check if a domain resolves to any record."""
    addrs = await resolve_a(domain)
    if addrs:
        return True
    cname = await resolve_cname(domain)
    return cname is not None
