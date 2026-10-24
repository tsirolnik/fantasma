"""Web crawler — discovers endpoints, forms, links, and JS files on a target."""

import logging
import re
from urllib.parse import urljoin, urlparse

from utils.http import fetch

logger = logging.getLogger(__name__)

# Max pages to crawl per target to stay within reasonable bounds
MAX_CRAWL_PAGES = 30
MAX_CRAWL_DEPTH = 3


async def crawl_target(
    target: str,
    max_pages: int = MAX_CRAWL_PAGES,
    max_depth: int = MAX_CRAWL_DEPTH,
) -> dict:
    """Crawl a target and return discovered assets.

    Returns a dict with:
        - urls: list of discovered URLs
        - forms: list of {action, method, inputs} dicts
        - js_files: list of JS file URLs
        - api_endpoints: list of URLs that look like API endpoints
        - emails: list of email addresses found
        - comments: list of HTML comments found
    """
    base_url = f"https://{target}" if not target.startswith("http") else target
    base_domain = urlparse(base_url).netloc

    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(base_url, 0)]

    all_urls: set[str] = set()
    forms: list[dict] = []
    js_files: set[str] = set()
    api_endpoints: set[str] = set()
    emails: set[str] = set()
    comments: list[str] = []

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)

        # Normalize URL
        parsed = urlparse(url)
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if normalized in visited:
            continue

        # Stay within the same domain
        if parsed.netloc and parsed.netloc != base_domain:
            continue

        visited.add(normalized)
        response = await fetch(url)
        if response is None or response.status_code != 200:
            continue

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            continue

        html = response.text
        all_urls.add(url)

        # Extract links
        for match in re.finditer(r'(?:href|src|action)=["\']([^"\']+)["\']', html, re.I):
            link = match.group(1)
            absolute = urljoin(url, link)
            link_parsed = urlparse(absolute)

            # Collect JS files
            if link.endswith(".js") or ".js?" in link:
                js_files.add(absolute)

            # Collect API endpoints
            if any(seg in link_parsed.path.lower() for seg in
                   ("/api/", "/v1/", "/v2/", "/v3/", "/graphql", "/rest/", "/json", ".json")):
                api_endpoints.add(absolute)

            # Queue for crawling if same domain and within depth
            if (link_parsed.netloc == base_domain or not link_parsed.netloc) and depth < max_depth:
                # Skip non-page resources
                if not any(link.endswith(ext) for ext in
                           (".css", ".png", ".jpg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf")):
                    queue.append((absolute, depth + 1))

            all_urls.add(absolute)

        # Extract forms
        for form_match in re.finditer(
            r"<form\s([^>]*)>(.*?)</form>", html, re.I | re.DOTALL
        ):
            attrs = form_match.group(1)
            body = form_match.group(2)

            action_m = re.search(r'action=["\']([^"\']*)["\']', attrs, re.I)
            method_m = re.search(r'method=["\']([^"\']*)["\']', attrs, re.I)

            action = urljoin(url, action_m.group(1)) if action_m else url
            method = method_m.group(1).upper() if method_m else "GET"

            inputs = []
            for inp_match in re.finditer(
                r'<(?:input|textarea|select)\s([^>]*)/?>', body, re.I
            ):
                inp_attrs = inp_match.group(1)
                name_m = re.search(r'name=["\']([^"\']*)["\']', inp_attrs, re.I)
                type_m = re.search(r'type=["\']([^"\']*)["\']', inp_attrs, re.I)
                if name_m:
                    inputs.append({
                        "name": name_m.group(1),
                        "type": type_m.group(1) if type_m else "text",
                    })

            forms.append({"action": action, "method": method, "inputs": inputs})

        # Extract emails
        for email in re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", html):
            emails.add(email)

        # Extract HTML comments (may contain TODOs, debug info, credentials)
        for comment in re.findall(r"<!--(.*?)-->", html, re.DOTALL):
            comment = comment.strip()
            if len(comment) > 10 and len(comment) < 500:  # Skip trivial/huge comments
                comments.append(comment[:200])

    return {
        "urls": sorted(all_urls),
        "forms": forms,
        "js_files": sorted(js_files),
        "api_endpoints": sorted(api_endpoints),
        "emails": sorted(emails),
        "comments": comments[:20],  # Cap comments
        "pages_crawled": len(visited),
    }
