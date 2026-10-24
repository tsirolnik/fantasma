"""Async HTTP client with retries and rate limiting."""

import logging
from urllib.parse import urlparse

import httpx

from config import get_settings
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; SecurityResearchBot/1.0; +https://github.com/tsirolnik/fantasma)"

_rate_limiter: RateLimiter | None = None


def _get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(get_settings().rate_limit_rps)
    return _rate_limiter


async def fetch(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    follow_redirects: bool = True,
    max_retries: int = 2,
    timeout: float | None = None,
) -> httpx.Response | None:
    """Make an HTTP request with rate limiting and retries.

    Returns the Response or None on failure.
    """
    settings = get_settings()
    timeout = timeout or settings.scan_timeout
    domain = urlparse(url).hostname or ""
    limiter = _get_rate_limiter()

    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)

    for attempt in range(max_retries + 1):
        await limiter.acquire(domain)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=follow_redirects,
                verify=True,
            ) as client:
                response = await client.request(method, url, headers=request_headers)
                return response
        except httpx.TimeoutException:
            logger.debug(f"Timeout on {url} (attempt {attempt + 1}/{max_retries + 1})")
        except httpx.ConnectError:
            logger.debug(f"Connection error on {url} (attempt {attempt + 1}/{max_retries + 1})")
        except Exception as e:
            logger.debug(f"HTTP error on {url}: {e}")
            break

    return None


async def fetch_json(url: str, headers: dict | None = None) -> dict | list | None:
    """Fetch URL and parse as JSON. Returns None on failure."""
    response = await fetch(url, headers=headers)
    if response and response.status_code == 200:
        try:
            return response.json()
        except Exception:
            logger.debug(f"Failed to parse JSON from {url}")
    return None
