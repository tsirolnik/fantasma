"""Tool definitions and registry for agentic tool-use.

Provides the Tool dataclass, ToolRegistry for managing tools (with support
for adding tools at runtime), a @tool decorator, and pre-built tool
registries for recon, scanning, discovery, and analysis agents.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 4000  # Truncate large tool outputs to avoid context overflow


@dataclass
class Tool:
    """A single tool that an agent can invoke."""

    name: str
    description: str
    parameters: dict  # JSON Schema for the tool's input
    handler: Callable  # async function(input_dict) -> result
    category: str = "general"
    risk_level: str = "low"  # "low", "medium", "high"


class ToolRegistry:
    """Registry of tools available to an agent.

    Supports adding tools after creation via register() — useful for
    plugins, custom scanners, or runtime-configured tool sets.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Overwrites if name already exists."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it existed."""
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self, category: str | None = None) -> list[Tool]:
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        return tools

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def merge(self, other: ToolRegistry) -> None:
        """Merge all tools from another registry into this one."""
        for tool in other._tools.values():
            self.register(tool)

    def to_anthropic_schema(self) -> list[dict]:
        """Convert all tools to Anthropic API tool format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in self._tools.values()
        ]

    def to_ollama_schema(self) -> list[dict]:
        """Convert all tools to Ollama API tool format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]


def tool(
    name: str,
    description: str,
    parameters: dict,
    category: str = "general",
    risk_level: str = "low",
):
    """Decorator that wraps an async function as a Tool and registers it in a registry.

    Usage:
        registry = ToolRegistry()

        @tool("my_tool", "Does something", {...}, category="recon")
        async def my_tool(params):
            ...

        registry.register(my_tool._tool)
    """

    def decorator(func: Callable) -> Callable:
        func._tool = Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=func,
            category=category,
            risk_level=risk_level,
        )
        return func

    return decorator


def _truncate(value: Any) -> str:
    """Serialize a value to JSON string, truncating if too large."""
    text = json.dumps(value, default=str)
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "... (truncated)"
    return text


def _serialize_findings(findings: list) -> list[dict]:
    """Serialize VulnFinding list to JSON-safe dicts."""
    result = []
    for f in findings:
        if hasattr(f, "model_dump"):
            d = f.model_dump(mode="json")
        elif isinstance(f, dict):
            d = f
        else:
            d = {"value": str(f)}
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Recon tools (8)
# ---------------------------------------------------------------------------


def create_recon_tools() -> ToolRegistry:
    """Create the tool registry for reconnaissance agents."""
    registry = ToolRegistry()

    async def enumerate_subdomains(params: dict) -> Any:
        from agents.recon import ReconAgent

        agent = ReconAgent()
        subs = await agent._enumerate_subdomains(params["domain"])
        return subs

    registry.register(Tool(
        name="enumerate_subdomains",
        description="Enumerate subdomains for a domain using certificate transparency and DNS bruteforce. Returns a list of discovered subdomains.",
        parameters={
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Target domain (e.g. example.com)"}},
            "required": ["domain"],
        },
        handler=enumerate_subdomains,
        category="recon",
    ))

    async def crtsh_lookup(params: dict) -> Any:
        from agents.recon import ReconAgent

        agent = ReconAgent()
        return await agent._crtsh_lookup(params["domain"])

    registry.register(Tool(
        name="crtsh_lookup",
        description="Query crt.sh Certificate Transparency logs for subdomains of a domain.",
        parameters={
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Target domain"}},
            "required": ["domain"],
        },
        handler=crtsh_lookup,
        category="recon",
    ))

    async def dns_bruteforce(params: dict) -> Any:
        from agents.recon import ReconAgent

        agent = ReconAgent()
        return await agent._dns_bruteforce(params["domain"])

    registry.register(Tool(
        name="dns_bruteforce",
        description="Bruteforce common subdomain prefixes via DNS resolution. Returns subdomains that resolve.",
        parameters={
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Target domain"}},
            "required": ["domain"],
        },
        handler=dns_bruteforce,
        category="recon",
    ))

    async def fingerprint_tech(params: dict) -> Any:
        from agents.recon import ReconAgent

        agent = ReconAgent()
        return await agent._fingerprint_tech(params["domain"])

    registry.register(Tool(
        name="fingerprint_tech",
        description="Detect web technologies (frameworks, CMS, CDN, etc.) on a domain. Returns a list of technology names.",
        parameters={
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Target domain"}},
            "required": ["domain"],
        },
        handler=fingerprint_tech,
        category="recon",
    ))

    async def check_ports(params: dict) -> Any:
        from agents.recon import ReconAgent

        agent = ReconAgent()
        return await agent._check_ports(params["domain"])

    registry.register(Tool(
        name="check_ports",
        description="Check which common web ports (80, 443, 8080, 8443) are open on a domain.",
        parameters={
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Target domain"}},
            "required": ["domain"],
        },
        handler=check_ports,
        category="recon",
    ))

    async def http_fetch(params: dict) -> Any:
        from utils.http import fetch

        url = params["url"]
        method = params.get("method", "GET")
        headers = params.get("headers")
        response = await fetch(url, method=method, headers=headers)
        if response is None:
            return {"error": "Request failed or timed out"}
        body = response.text[:2000] if response.text else ""
        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body_preview": body,
        }

    registry.register(Tool(
        name="http_fetch",
        description="Make an HTTP request and return the status code, headers, and body preview (first 2000 chars). Use to probe specific URLs.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch"},
                "method": {"type": "string", "description": "HTTP method (GET, POST, HEAD, etc.)", "default": "GET"},
                "headers": {"type": "object", "description": "Optional custom headers", "additionalProperties": {"type": "string"}},
            },
            "required": ["url"],
        },
        handler=http_fetch,
        category="recon",
        risk_level="medium",
    ))

    async def dns_resolve(params: dict) -> Any:
        from utils.dns import resolve_a, resolve_cname

        domain = params["domain"]
        record_type = params.get("record_type", "A")
        if record_type.upper() == "CNAME":
            cname = await resolve_cname(domain)
            return {"domain": domain, "cname": cname}
        a_records = await resolve_a(domain)
        cname = await resolve_cname(domain)
        return {"domain": domain, "a_records": a_records, "cname": cname}

    registry.register(Tool(
        name="dns_resolve",
        description="Resolve DNS records for a domain. Returns A records and/or CNAME record.",
        parameters={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Domain to resolve"},
                "record_type": {"type": "string", "description": "Record type: A or CNAME", "default": "A"},
            },
            "required": ["domain"],
        },
        handler=dns_resolve,
        category="recon",
    ))

    async def check_domain(params: dict) -> Any:
        from utils.dns import check_domain_exists

        exists = await check_domain_exists(params["domain"])
        return {"domain": params["domain"], "exists": exists}

    registry.register(Tool(
        name="check_domain",
        description="Check if a domain resolves to any DNS record (exists).",
        parameters={
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Domain to check"}},
            "required": ["domain"],
        },
        handler=check_domain,
        category="recon",
    ))

    return registry


# ---------------------------------------------------------------------------
# Search tools (1) — web search via DuckDuckGo (no API key needed)
# ---------------------------------------------------------------------------


def create_search_tools() -> ToolRegistry:
    """Create web search tools for research and OSINT."""
    registry = ToolRegistry()

    async def web_search(params: dict) -> Any:
        import httpx
        import re

        query = params["query"]
        max_results = params.get("max_results", 5)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=headers,
            )
            resp.raise_for_status()
            html = resp.text

        # Parse results from DDG HTML
        results = []
        for match in re.finditer(
            r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<a class="result__snippet"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        ):
            raw_url = match.group(1)
            # Unwrap DDG redirect URLs
            if "uddg=" in raw_url:
                from urllib.parse import unquote, urlparse, parse_qs
                parsed = parse_qs(urlparse(raw_url).query)
                url = unquote(parsed.get("uddg", [raw_url])[0])
            else:
                url = raw_url
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            snippet = re.sub(r"<[^>]+>", "", match.group(3)).strip()
            if url and title:
                results.append({"url": url, "title": title, "snippet": snippet})
            if len(results) >= max_results:
                break

        if not results:
            return f"No results found for: {query}"
        return {"query": query, "results": results}

    registry.register(Tool(
        name="web_search",
        description=(
            "Search the web using DuckDuckGo. Use for OSINT, finding documentation, "
            "looking up CVEs, finding known vulnerabilities for a technology, "
            "researching target companies, or finding API documentation. "
            "Returns titles, URLs, and snippets."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results to return (default 5)", "default": 5},
            },
            "required": ["query"],
        },
        handler=web_search,
        category="recon",
        risk_level="low",
    ))

    return registry


# ---------------------------------------------------------------------------
# Scanner tools (7)
# ---------------------------------------------------------------------------


def create_scanner_tools() -> ToolRegistry:
    """Create the tool registry for vulnerability scanning agents."""
    registry = ToolRegistry()

    async def scan_subdomain_takeover(params: dict) -> Any:
        from scanners.subdomain_takeover import SubdomainTakeoverScanner

        scanner = SubdomainTakeoverScanner()
        findings = await scanner.scan(params["target"])
        return _serialize_findings(findings)

    registry.register(Tool(
        name="scan_subdomain_takeover",
        description="Scan a target for subdomain takeover vulnerabilities via dangling CNAME records. Returns list of findings.",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain or subdomain"}},
            "required": ["target"],
        },
        handler=scan_subdomain_takeover,
        category="scan",
        risk_level="medium",
    ))

    async def scan_info_disclosure(params: dict) -> Any:
        from scanners.info_disclosure import InfoDisclosureScanner

        scanner = InfoDisclosureScanner()
        findings = await scanner.scan(params["target"])
        return _serialize_findings(findings)

    registry.register(Tool(
        name="scan_info_disclosure",
        description="Scan for information disclosure (exposed .git, .env, debug endpoints, API docs, etc.).",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain"}},
            "required": ["target"],
        },
        handler=scan_info_disclosure,
        category="scan",
        risk_level="medium",
    ))

    async def scan_security_headers(params: dict) -> Any:
        from scanners.security_headers import SecurityHeadersScanner

        scanner = SecurityHeadersScanner()
        findings = await scanner.scan(params["target"])
        return _serialize_findings(findings)

    registry.register(Tool(
        name="scan_security_headers",
        description="Check for missing security headers (HSTS, CSP, X-Frame-Options, etc.).",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain"}},
            "required": ["target"],
        },
        handler=scan_security_headers,
        category="scan",
    ))

    async def scan_open_redirect(params: dict) -> Any:
        from scanners.open_redirect import OpenRedirectScanner

        scanner = OpenRedirectScanner()
        findings = await scanner.scan(params["target"])
        return _serialize_findings(findings)

    registry.register(Tool(
        name="scan_open_redirect",
        description="Test for open redirect vulnerabilities on common redirect parameters.",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain"}},
            "required": ["target"],
        },
        handler=scan_open_redirect,
        category="scan",
        risk_level="medium",
    ))

    async def scan_tech_fingerprint(params: dict) -> Any:
        from scanners.tech_fingerprint import TechFingerprintScanner

        scanner = TechFingerprintScanner()
        recon_data: dict = {}
        await scanner.scan(params["target"], recon_data)
        return recon_data.get("technologies", [])

    registry.register(Tool(
        name="scan_tech_fingerprint",
        description="Detect web technologies on a target (CMS, frameworks, CDN). Returns technology list.",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain"}},
            "required": ["target"],
        },
        handler=scan_tech_fingerprint,
        category="scan",
    ))

    async def http_probe(params: dict) -> Any:
        from utils.http import fetch

        url = params["url"]
        method = params.get("method", "GET")
        headers = params.get("headers")
        response = await fetch(url, method=method, headers=headers)
        if response is None:
            return {"error": "Request failed or timed out"}
        body = response.text[:2000] if response.text else ""
        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body_snippet": body,
        }

    registry.register(Tool(
        name="http_probe",
        description="Probe a specific URL and return status, headers, and body snippet. Use to verify findings or check specific endpoints.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to probe"},
                "method": {"type": "string", "description": "HTTP method", "default": "GET"},
                "headers": {"type": "object", "description": "Custom headers", "additionalProperties": {"type": "string"}},
            },
            "required": ["url"],
        },
        handler=http_probe,
        category="scan",
        risk_level="medium",
    ))

    # -- Granular tools for precise agent control --

    # -- Granular page analysis tools (the agent decides what to follow) --

    async def extract_links(params: dict) -> Any:
        from utils.http import fetch
        import re as _re
        from urllib.parse import urljoin

        url = params["url"]
        response = await fetch(url)
        if response is None:
            return {"error": "Failed to fetch"}
        links = set()
        for match in _re.finditer(r'(?:href|src|action)=["\']([^"\'#]+)["\']', response.text, _re.I):
            links.add(urljoin(url, match.group(1)))
        return {"url": url, "links": sorted(links)}

    registry.register(Tool(
        name="extract_links",
        description=(
            "Extract all links (href, src, action) from a page. Returns a list "
            "of absolute URLs. You decide which ones to follow or investigate."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to extract links from"}},
            "required": ["url"],
        },
        handler=extract_links,
        category="scan",
        risk_level="low",
    ))

    async def extract_forms(params: dict) -> Any:
        from utils.http import fetch
        import re as _re
        from urllib.parse import urljoin

        url = params["url"]
        response = await fetch(url)
        if response is None:
            return {"error": "Failed to fetch"}
        forms = []
        for form_match in _re.finditer(r"<form\s([^>]*)>(.*?)</form>", response.text, _re.I | _re.DOTALL):
            attrs, body = form_match.group(1), form_match.group(2)
            action_m = _re.search(r'action=["\']([^"\']*)["\']', attrs, _re.I)
            method_m = _re.search(r'method=["\']([^"\']*)["\']', attrs, _re.I)
            inputs = []
            for inp in _re.finditer(r'<(?:input|textarea|select)\s([^>]*)/?>', body, _re.I):
                name_m = _re.search(r'name=["\']([^"\']*)["\']', inp.group(1), _re.I)
                type_m = _re.search(r'type=["\']([^"\']*)["\']', inp.group(1), _re.I)
                if name_m:
                    inputs.append({"name": name_m.group(1), "type": type_m.group(1) if type_m else "text"})
            forms.append({
                "action": urljoin(url, action_m.group(1)) if action_m else url,
                "method": method_m.group(1).upper() if method_m else "GET",
                "inputs": inputs,
            })
        return {"url": url, "forms": forms}

    registry.register(Tool(
        name="extract_forms",
        description=(
            "Extract all HTML forms from a page with their action URLs, methods, "
            "and input fields (name + type). Use to find login forms, search boxes, "
            "file uploads, and other injection points."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to extract forms from"}},
            "required": ["url"],
        },
        handler=extract_forms,
        category="scan",
        risk_level="low",
    ))

    async def extract_html_comments(params: dict) -> Any:
        from utils.http import fetch
        import re as _re

        url = params["url"]
        response = await fetch(url)
        if response is None:
            return {"error": "Failed to fetch"}
        comments = []
        for c in _re.findall(r"<!--(.*?)-->", response.text, _re.DOTALL):
            c = c.strip()
            if len(c) > 5:
                comments.append(c[:300])
        return {"url": url, "comments": comments[:30]}

    registry.register(Tool(
        name="extract_html_comments",
        description=(
            "Extract HTML comments from a page. Comments often contain TODOs, "
            "debug info, internal URLs, credentials, or developer notes."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to extract comments from"}},
            "required": ["url"],
        },
        handler=extract_html_comments,
        category="scan",
        risk_level="low",
    ))

    async def extract_headers(params: dict) -> Any:
        from utils.http import fetch

        url = params["url"]
        method = params.get("method", "HEAD")
        response = await fetch(url, method=method)
        if response is None:
            return {"error": "Failed to fetch"}
        return {
            "url": url,
            "status": response.status_code,
            "headers": dict(response.headers),
        }

    registry.register(Tool(
        name="extract_headers",
        description=(
            "Get HTTP response headers for a URL. Use HEAD for efficiency. "
            "Useful for server fingerprinting, checking security headers, "
            "finding technology clues (X-Powered-By, Server, etc.)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to check"},
                "method": {"type": "string", "description": "HTTP method (HEAD or GET)", "default": "HEAD"},
            },
            "required": ["url"],
        },
        handler=extract_headers,
        category="scan",
        risk_level="low",
    ))

    # -- Docker-based tools (heavy lifting) --

    async def run_docker_tool(params: dict) -> Any:
        from utils.docker_runner import ensure_image, run_container

        image = params["image"]
        command = params.get("command", "")
        args = params.get("args", [])
        timeout = params.get("timeout", 300)
        if not await ensure_image(image):
            return {"error": f"Failed to pull image {image}. Is Docker running?"}
        if command:
            # Image with bash -c entrypoint or override
            exit_code, stdout, stderr = await run_container(image, [command], timeout=timeout)
        else:
            exit_code, stdout, stderr = await run_container(image, args, timeout=timeout)
        return {
            "exit_code": exit_code,
            "stdout": stdout[:4000],
            "stderr": stderr[:1000] if exit_code != 0 else "",
        }

    registry.register(Tool(
        name="run_docker_tool",
        description=(
            "Run any command inside a Docker container. "
            "ALWAYS use image='bugbounty-tools:latest' — it has ALL tools pre-installed. "
            "ALWAYS use the 'command' parameter (string), NOT 'args'.\n\n"
            "AVAILABLE TOOLS in bugbounty-tools:latest:\n"
            "- nmap, nuclei, ffuf, katana, nikto, sqlmap, subfinder, httpx\n"
            "- curl, dig, whois, gau, dalfox, amass, arjun, commix, wpscan\n\n"
            "EXAMPLES (image='bugbounty-tools:latest' for ALL of these):\n"
            "- command='nmap -sV -sC target.com' — port/service scan\n"
            "- command='nuclei -u https://target.com -severity medium,high,critical -jsonl -silent' — vuln scan\n"
            "- command='ffuf -u https://target.com/FUZZ -w /usr/share/wordlists/dirb/common.txt -json -s' — dir fuzzing\n"
            "- command='katana -u https://target.com -d 3 -jc -jsonl -silent' — web crawling\n"
            "- command='nikto -h https://target.com -Format json' — web server scanner\n"
            "- command='sqlmap -u \"https://target.com/page?id=1\" --batch' — SQL injection\n"
            "- command='curl -sI https://target.com' — HTTP headers\n"
            "- command='dig +short target.com A' — DNS lookup\n"
            "- command='gau target.com' — known URLs from Wayback/CommonCrawl\n"
            "- command='dalfox url https://target.com/page?q=test' — XSS scanner\n"
            "- command='arjun -u https://target.com/endpoint' — hidden param discovery\n"
            "- command='subfinder -d target.com -silent' — subdomain enumeration\n\n"
            "OTHER IMAGES (only if you need something not in bugbounty-tools):\n"
            "- python:3.12-slim — write and run custom Python scripts for advanced testing\n"
            "- kalilinux/kali-rolling — full Kali (tools need apt-get install first)\n"
            "- alpine:latest — minimal Linux for quick scripts"
        ),
        parameters={
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "Docker image to use"},
                "command": {"type": "string", "description": "Shell command (passed to bash -c / sh -c)"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Direct exec arguments (alternative to command)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 300)", "default": 300},
            },
            "required": ["image"],
        },
        handler=run_docker_tool,
        category="scan",
        risk_level="medium",
    ))

    # -- Specific bug hunting tools --

    async def test_ssrf(params: dict) -> Any:
        from utils.http import fetch

        url = params["url"]
        parameter = params["parameter"]
        payload = params.get("payload", "http://169.254.169.254/latest/meta-data/")
        if "?" in url:
            test_url = f"{url}&{parameter}={payload}"
        else:
            test_url = f"{url}?{parameter}={payload}"
        response = await fetch(test_url)
        if response is None:
            return {"error": "Request failed", "test_url": test_url}
        indicators = ["ami-id", "instance-id", "hostname", "local-ipv4", "iam"]
        ssrf_detected = any(ind in response.text.lower() for ind in indicators)
        return {
            "test_url": test_url,
            "status": response.status_code,
            "body_preview": response.text[:1000],
            "ssrf_indicators_found": ssrf_detected,
            "response_snippet": response.text[:500],
        }

    registry.register(Tool(
        name="test_ssrf",
        description=(
            "Test a URL parameter for Server-Side Request Forgery (SSRF). "
            "Injects a payload URL (default: AWS metadata) into the specified "
            "parameter and checks if the response contains internal data. "
            "Use on URL parameters that fetch/load/proxy external content."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Base URL (e.g. https://target.com/fetch)"},
                "parameter": {"type": "string", "description": "Parameter name to inject into (e.g. url, redirect, file, path)"},
                "payload": {"type": "string", "description": "SSRF payload URL (default: AWS metadata)", "default": "http://169.254.169.254/latest/meta-data/"},
            },
            "required": ["url", "parameter"],
        },
        handler=test_ssrf,
        category="scan",
        risk_level="medium",
    ))

    async def test_header_injection(params: dict) -> Any:
        from utils.http import fetch

        url = params["url"]
        parameter = params["parameter"]
        payload = params.get("payload", "test%0d%0aX-Injected:%20true")
        if "?" in url:
            test_url = f"{url}&{parameter}={payload}"
        else:
            test_url = f"{url}?{parameter}={payload}"
        response = await fetch(test_url, follow_redirects=False)
        if response is None:
            return {"error": "Request failed", "test_url": test_url}
        injected = "x-injected" in str(response.headers).lower()
        return {
            "test_url": test_url,
            "status": response.status_code,
            "headers": dict(response.headers),
            "header_injected": injected,
            "response_snippet": response.text[:500],
        }

    registry.register(Tool(
        name="test_header_injection",
        description=(
            "Test a URL parameter for CRLF / HTTP header injection. "
            "Injects a CRLF payload and checks if a custom header appears "
            "in the response. Use on parameters reflected in redirect URLs or headers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Base URL"},
                "parameter": {"type": "string", "description": "Parameter to inject into"},
                "payload": {"type": "string", "description": "Injection payload (default: CRLF + header)"},
            },
            "required": ["url", "parameter"],
        },
        handler=test_header_injection,
        category="scan",
        risk_level="medium",
    ))

    async def fuzz_parameter(params: dict) -> Any:
        from utils.http import fetch

        url = params["url"]
        parameter = params["parameter"]
        payloads = params.get("payloads", [
            "'", "\"", "<script>", "{{7*7}}", "${7*7}", "../../../etc/passwd",
            "1 OR 1=1", "1' OR '1'='1", "admin'--", "%00", "\"><img src=x onerror=alert(1)>",
            "{{constructor.constructor('return this')()}}", "`id`", "| id",
        ])
        results = []
        for payload in payloads[:20]:
            if "?" in url:
                test_url = f"{url}&{parameter}={payload}"
            else:
                test_url = f"{url}?{parameter}={payload}"
            response = await fetch(test_url)
            if response is None:
                continue
            reflected = payload in response.text
            error_indicators = ["error", "exception", "traceback", "syntax", "unexpected", "unterminated"]
            has_error = any(ind in response.text.lower() for ind in error_indicators)
            results.append({
                "payload": payload,
                "status": response.status_code,
                "reflected": reflected,
                "error_triggered": has_error,
                "body_length": len(response.text),
                "response_snippet": response.text[:500],
            })
        return {"url": url, "parameter": parameter, "results": results}

    registry.register(Tool(
        name="fuzz_parameter",
        description=(
            "Fuzz a single URL parameter with injection payloads (SQL, XSS, SSTI, "
            "path traversal, command injection). Checks if payloads are reflected "
            "or trigger errors. Provide custom payloads or use the built-in set. "
            "Use on form inputs, search parameters, API params you discovered."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Base URL"},
                "parameter": {"type": "string", "description": "Parameter name to fuzz"},
                "payloads": {"type": "array", "items": {"type": "string"}, "description": "Custom payloads (optional — has built-in defaults)"},
            },
            "required": ["url", "parameter"],
        },
        handler=fuzz_parameter,
        category="scan",
        risk_level="medium",
    ))

    async def scan_js_secrets(params: dict) -> Any:
        from scanners.secret_scanner import SecretScanner

        scanner = SecretScanner()
        findings = await scanner.scan(params["target"])
        return _serialize_findings(findings)

    registry.register(Tool(
        name="scan_js_secrets",
        description=(
            "Scan a target's JavaScript files and HTML for leaked secrets: "
            "API keys (AWS, Google, GitHub, Slack, Stripe, SendGrid), tokens, "
            "private keys, JWTs, passwords, and internal IPs. Very effective "
            "against real targets."
        ),
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain"}},
            "required": ["target"],
        },
        handler=scan_js_secrets,
        category="scan",
        risk_level="low",
    ))

    async def regex_scan_url(params: dict) -> Any:
        from utils.http import fetch
        import re as _re

        url = params["url"]
        pattern = params["pattern"]
        response = await fetch(url)
        if response is None:
            return {"error": "Failed to fetch URL"}
        matches = _re.findall(pattern, response.text[:200_000])
        return {
            "url": url,
            "pattern": pattern,
            "match_count": len(matches),
            "matches": matches[:20],  # Cap at 20 matches
        }

    registry.register(Tool(
        name="regex_scan_url",
        description=(
            "Fetch a URL and scan its content with a custom regex pattern. "
            "Use for targeted secret hunting, pattern matching, or checking "
            "specific strings in responses. Returns all matches."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch and scan"},
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
            },
            "required": ["url", "pattern"],
        },
        handler=regex_scan_url,
        category="scan",
        risk_level="low",
    ))

    async def check_sensitive_path(params: dict) -> Any:
        from scanners.sensitive_paths import is_sensitive_path_false_positive
        from utils.http import fetch

        target = params["target"]
        path = params["path"]
        indicator = params.get("indicator")
        base_url = f"https://{target}" if not target.startswith("http") else target
        url = f"{base_url}{path}"
        response = await fetch(url)
        if response is None:
            return {"url": url, "status": None, "found": False}
        found = response.status_code == 200
        if found and indicator:
            found = indicator.lower() in response.text.lower()
        body_preview = response.text[:1000] if response.text else ""
        content_type = response.headers.get("content-type", "")
        fp = is_sensitive_path_false_positive(body_preview, content_type, path) if found else False
        return {
            "url": url,
            "status": response.status_code,
            "found": found,
            "likely_false_positive": fp,
            "body_preview": body_preview,
            "content_length": len(response.content),
            "headers": dict(response.headers),
        }

    registry.register(Tool(
        name="check_sensitive_path",
        description=(
            "Check if a specific path exists on a target and optionally verify "
            "an indicator string in the response. Use to check for specific "
            "files like /.env, /.git/HEAD, /actuator/env, /graphql, etc. "
            "Returns status, body preview, and headers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Target domain"},
                "path": {"type": "string", "description": "Path to check (e.g. /.env, /.git/HEAD)"},
                "indicator": {"type": "string", "description": "Optional string that must be in the response body to confirm the finding"},
            },
            "required": ["target", "path"],
        },
        handler=check_sensitive_path,
        category="scan",
        risk_level="low",
    ))

    async def scan_sensitive_paths_full(params: dict) -> Any:
        from scanners.sensitive_paths import SensitivePathScanner

        scanner = SensitivePathScanner()
        findings = await scanner.scan(params["target"])
        return _serialize_findings(findings)

    registry.register(Tool(
        name="scan_sensitive_paths",
        description=(
            "Run a comprehensive scan for ~60 sensitive paths: .git, .env, "
            "backups, Spring Actuator, admin panels, DevOps files, debug "
            "endpoints, etc. Use when you want to brute-force all common paths."
        ),
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain"}},
            "required": ["target"],
        },
        handler=scan_sensitive_paths_full,
        category="scan",
        risk_level="medium",
    ))

    async def run_nuclei_templates(params: dict) -> Any:
        from scanners.nuclei_templates import NucleiScanner

        scanner = NucleiScanner()
        findings = await scanner.scan(params["target"])
        return _serialize_findings(findings)

    registry.register(Tool(
        name="run_nuclei_templates_builtin",
        description=(
            "Run built-in nuclei-style template checks (no Docker needed). "
            "Covers ~25 checks: Struts, Log4Shell, exposed panels, cloud misconfigs, "
            "dangerous HTTP methods, WordPress. Use run_nuclei for the full 8000+ "
            "template set instead when Docker is available."
        ),
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain"}},
            "required": ["target"],
        },
        handler=run_nuclei_templates,
        category="scan",
        risk_level="medium",
    ))

    async def extract_js_urls(params: dict) -> Any:
        from utils.http import fetch
        import re as _re
        from urllib.parse import urljoin

        url = params["url"]
        response = await fetch(url)
        if response is None:
            return {"error": "Failed to fetch"}
        # Extract script src URLs
        scripts = _re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', response.text, _re.I)
        js_urls = [urljoin(url, s) for s in scripts if s.endswith(".js") or ".js?" in s or "/js/" in s]
        # Also extract inline URLs from JS content
        endpoints = _re.findall(r'["\']((?:/api/|/v[123]/|/graphql|/rest/)[^"\']*)["\']', response.text)
        return {
            "js_files": sorted(set(js_urls)),
            "api_endpoints": sorted(set(endpoints)),
        }

    registry.register(Tool(
        name="extract_js_urls",
        description=(
            "Extract JavaScript file URLs and API endpoint references from a page. "
            "Finds <script src> tags and API paths referenced in the HTML. "
            "Use as a first step before scanning JS files for secrets."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL of the page to extract JS URLs from"},
            },
            "required": ["url"],
        },
        handler=extract_js_urls,
        category="scan",
        risk_level="low",
    ))

    async def test_aws_credentials(params: dict) -> Any:
        from utils.docker_runner import ensure_image, run_container

        access_key = params["access_key"]
        secret_key = params["secret_key"]
        region = params.get("region", "us-east-1")
        image = "bugbounty-tools:latest"

        if not await ensure_image(image):
            return {"error": f"Docker image '{image}' not available"}

        # Script validates creds and enumerates permissions
        script = f"""
import boto3, json, sys
from botocore.exceptions import ClientError, NoCredentialsError

results = {{"valid": False}}
try:
    sts = boto3.client("sts", aws_access_key_id="{access_key}",
                       aws_secret_access_key="{secret_key}", region_name="{region}")
    identity = sts.get_caller_identity()
    results["valid"] = True
    results["account"] = identity.get("Account", "")
    results["arn"] = identity.get("Arn", "")
    results["user_id"] = identity.get("UserId", "")

    # Try listing S3 buckets
    try:
        s3 = boto3.client("s3", aws_access_key_id="{access_key}",
                          aws_secret_access_key="{secret_key}", region_name="{region}")
        buckets = s3.list_buckets().get("Buckets", [])
        results["s3_buckets"] = [b["Name"] for b in buckets[:20]]
    except ClientError as e:
        results["s3_error"] = e.response["Error"]["Code"]

    # Try getting IAM user info
    try:
        iam = boto3.client("iam", aws_access_key_id="{access_key}",
                           aws_secret_access_key="{secret_key}", region_name="{region}")
        user = iam.get_user()
        results["iam_user"] = user["User"]["UserName"]
        # List attached policies
        policies = iam.list_attached_user_policies(UserName=user["User"]["UserName"])
        results["policies"] = [p["PolicyName"] for p in policies.get("AttachedPolicies", [])[:10]]
    except ClientError as e:
        results["iam_error"] = e.response["Error"]["Code"]

except ClientError as e:
    results["error"] = e.response["Error"]["Code"]
except NoCredentialsError:
    results["error"] = "invalid_credentials"
except Exception as e:
    results["error"] = str(e)[:200]

print(json.dumps(results))
"""
        exit_code, stdout, stderr = await run_container(
            image, [f"python3 -c '{script}'"], timeout=20,
        )
        try:
            return json.loads(stdout.strip())
        except Exception:
            return {"exit_code": exit_code, "stdout": stdout[:2000], "stderr": stderr[:500]}

    registry.register(Tool(
        name="test_aws_credentials",
        description=(
            "Validate leaked AWS credentials (access key + secret key). "
            "Checks if they're active via STS GetCallerIdentity, then enumerates "
            "S3 buckets and IAM policies. Use this when scan_js_secrets or "
            "check_sensitive_path finds AWS keys (AKIA...)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "access_key": {"type": "string", "description": "AWS Access Key ID (starts with AKIA)"},
                "secret_key": {"type": "string", "description": "AWS Secret Access Key"},
                "region": {"type": "string", "description": "AWS region (default: us-east-1)", "default": "us-east-1"},
            },
            "required": ["access_key", "secret_key"],
        },
        handler=test_aws_credentials,
        category="scan",
        risk_level="high",
    ))

    return registry


# ---------------------------------------------------------------------------
# Browser tools (8) — require a BrowserSession instance
# ---------------------------------------------------------------------------


def create_browser_tools(session) -> ToolRegistry:
    """Create tools that let an agent control a headless browser.

    Args:
        session: A ``BrowserSession`` instance (from ``utils.browser``).
    """
    registry = ToolRegistry()

    async def browser_navigate(params: dict) -> Any:
        url = params["url"]
        result = await session.navigate(url)
        if result.get("error"):
            return f"Navigation failed: {result['error']}"
        text = await session.get_text()
        return f"Navigated to: {result.get('title', '(no title)')}\nURL: {result['url']}\n\n{text[:3000]}"

    registry.register(Tool(
        name="browser_navigate",
        description=(
            "Navigate the browser to a URL and return the page title + visible text. "
            "Use for JS-rendered pages that plain HTTP requests can't read (e.g. "
            "Bugcrowd scope pages, SPAs, pages behind Cloudflare). The browser "
            "maintains cookies and login state across calls."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to navigate to"}},
            "required": ["url"],
        },
        handler=browser_navigate,
        category="browser",
    ))

    async def browser_get_text(params: dict) -> Any:
        selector = params.get("selector")
        text = await session.get_text(selector)
        return text[:5000] if text else "(no text found)"

    registry.register(Tool(
        name="browser_get_text",
        description=(
            "Get visible text from the current page. Optionally pass a CSS selector "
            "to get text from a specific element. Returns up to 5000 chars."
        ),
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector (optional — omit for full page)"},
            },
            "required": [],
        },
        handler=browser_get_text,
        category="browser",
    ))

    async def browser_click(params: dict) -> Any:
        selector = params["selector"]
        result = await session.click(selector)
        if not result.get("success"):
            return f"Click failed: {result.get('error', 'unknown')}"
        text = await session.get_text()
        return f"Clicked. New URL: {result['url']}\n\n{text[:2000]}"

    registry.register(Tool(
        name="browser_click",
        description=(
            "Click an element on the current page. Use CSS selectors like "
            "'button.submit', '#login', 'a[href*=scope]' or Playwright text "
            "selectors like 'text=Scope', 'text=Targets'. Returns page text after click."
        ),
        parameters={
            "type": "object",
            "properties": {"selector": {"type": "string", "description": "CSS or text selector (e.g. 'text=Scope')"}},
            "required": ["selector"],
        },
        handler=browser_click,
        category="browser",
    ))

    async def browser_type(params: dict) -> Any:
        selector = params["selector"]
        text = params["text"]
        result = await session.type_text(selector, text)
        if not result.get("success"):
            return f"Type failed: {result.get('error', 'unknown')}"
        return "Typed text successfully."

    registry.register(Tool(
        name="browser_type",
        description="Type text into an input field. Use CSS selector to identify the field.",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the input field"},
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["selector", "text"],
        },
        handler=browser_type,
        category="browser",
    ))

    async def browser_get_links(params: dict) -> Any:
        links = await session.get_links()
        return links[:200]

    registry.register(Tool(
        name="browser_get_links",
        description="Get all links on the current page as [{text, href}, ...]. Max 200 links.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=browser_get_links,
        category="browser",
    ))

    async def browser_screenshot(params: dict) -> Any:
        title = await session.get_title()
        page = await session._ensure_page()
        url = page.url if page else "unknown"
        b64 = await session.screenshot()
        # Return structured image result — tool_agent detects __image__ key
        return {
            "__image__": b64,
            "__media_type__": "image/png",
            "url": url,
            "title": title,
            "size_kb": round(len(b64) * 3 / 4 / 1024, 1),
        }

    registry.register(Tool(
        name="browser_screenshot",
        description=(
            "Take a screenshot of the current page and SEE it. "
            "Use this to understand page layout, read text, diagnose CAPTCHAs, "
            "check login forms, verify redirects, or see what the page actually looks like. "
            "The image is sent to you so you can visually inspect it."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=browser_screenshot,
        category="browser",
    ))

    async def browser_back(params: dict) -> Any:
        result = await session.back()
        return f"Went back to: {result['url']}"

    registry.register(Tool(
        name="browser_back",
        description="Go back one page in browser history.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=browser_back,
        category="browser",
    ))

    async def browser_evaluate(params: dict) -> Any:
        js = params["js"]
        result = await session.evaluate(js)
        text = json.dumps(result, default=str) if result is not None else "undefined"
        return text[:5000]

    registry.register(Tool(
        name="browser_evaluate",
        description=(
            "Execute JavaScript in the browser page and return the result. "
            "Use to extract data from DOM, read JS variables, call page APIs, "
            "or scrape structured data. Example: "
            "js=\"document.querySelectorAll('.scope-item').length\""
        ),
        parameters={
            "type": "object",
            "properties": {"js": {"type": "string", "description": "JavaScript code to evaluate"}},
            "required": ["js"],
        },
        handler=browser_evaluate,
        category="browser",
    ))

    return registry


# ---------------------------------------------------------------------------
# Human help tool — standalone, works without browser
# ---------------------------------------------------------------------------


def create_help_tool(help_callback) -> ToolRegistry:
    """Create the request_help tool that lets agents ask the human operator.

    Args:
        help_callback: async function(help_id, description, context) -> str
    """
    registry = ToolRegistry()

    async def request_help(params: dict) -> Any:
        import uuid as _uuid
        description = params["description"]
        context = params.get("context", {})
        help_id = _uuid.uuid4().hex[:12]

        if help_callback is None:
            return "(No dashboard connected — cannot request human help. Continuing without assistance.)"

        try:
            response = await help_callback(help_id, description, context)
            return f"Human responded: {response}" if response else "(Human skipped / no response)"
        except Exception as exc:
            return f"Help request failed: {exc}"

    registry.register(Tool(
        name="request_help",
        description=(
            "Ask the human operator for help when you're stuck. Use when you "
            "encounter: CAPTCHAs, login gates, bot protection, phone verification, "
            "unclear scope rules, or any blocker you can't solve autonomously. "
            "The human sees your description in the dashboard and can type a response."
        ),
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What you need help with"},
                "context": {"type": "object", "description": "Optional context (URL, error, etc.)"},
            },
            "required": ["description"],
        },
        handler=request_help,
        category="human",
        risk_level="low",
    ))

    return registry


# ---------------------------------------------------------------------------
# Discovery tools (4)
# ---------------------------------------------------------------------------


def create_discovery_tools() -> ToolRegistry:
    """Create the tool registry for program discovery agents."""
    registry = ToolRegistry()

    def _slim_programs(programs: list) -> list[dict]:
        """Pre-filter and slim down programs for the LLM.

        Removes programs with zero scannable domains and returns only
        the fields the LLM needs for ranking — keeps context small.
        Note: scope_raw_text is NOT included here (too large for LLM context).
        The full BountyProgram objects with scope_raw_text are preserved
        by _accumulate_programs and used later in the pipeline.
        """
        results = []
        for p in programs:
            if not p.all_domains:
                continue
            entry = {
                "name": p.name,
                "platform": p.platform,
                "url": p.url,
                "scope_domains": p.scope_domains[:10],
                "scope_wildcards": p.scope_wildcards[:5],
                "min_bounty": p.min_bounty,
                "max_bounty": p.max_bounty,
                "resolved_count": p.resolved_count,
                "response_time_days": p.response_time_days,
            }
            if p.out_of_scope:
                entry["out_of_scope_count"] = len(p.out_of_scope)
            results.append(entry)
        # Sort by max_bounty descending, cap at top 30
        results.sort(key=lambda x: x["max_bounty"], reverse=True)
        return results[:30]

    def _build_fetch_result(programs: list) -> dict:
        """Build a tool result with slimmed data for the LLM + full programs for accumulation."""
        with_domains = [p for p in programs if p.all_domains]
        slimmed = _slim_programs(programs)
        return {
            "total_fetched": len(programs),
            "with_domains": len(with_domains),
            "programs": slimmed,
            # Full BountyProgram objects for _accumulate_programs (not sent to LLM)
            "_full_programs": with_domains,
        }

    async def fetch_hackerone_programs(params: dict) -> Any:
        from platforms.hackerone import HackerOne
        return _build_fetch_result(await HackerOne().fetch_programs())

    registry.register(Tool(
        name="fetch_hackerone_programs",
        description="Fetch bug bounty programs from HackerOne. Returns top programs (sorted by bounty) with scannable web domains.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=fetch_hackerone_programs,
        category="discovery",
    ))

    async def fetch_bugcrowd_programs(params: dict) -> Any:
        from platforms.bugcrowd import Bugcrowd
        return _build_fetch_result(await Bugcrowd().fetch_programs())

    registry.register(Tool(
        name="fetch_bugcrowd_programs",
        description="Fetch bug bounty programs from Bugcrowd. Returns top programs with scannable web domains.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=fetch_bugcrowd_programs,
        category="discovery",
    ))

    async def fetch_intigriti_programs(params: dict) -> Any:
        from platforms.intigriti import Intigriti
        return _build_fetch_result(await Intigriti().fetch_programs())

    registry.register(Tool(
        name="fetch_intigriti_programs",
        description="Fetch bug bounty programs from Intigriti. Returns top programs with scannable web domains.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=fetch_intigriti_programs,
        category="discovery",
    ))

    async def fetch_yeswehack_programs(params: dict) -> Any:
        from platforms.yeswehack import YesWeHack
        return _build_fetch_result(await YesWeHack().fetch_programs())

    registry.register(Tool(
        name="fetch_yeswehack_programs",
        description="Fetch bug bounty programs from YesWeHack. Returns top programs with scannable web domains.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=fetch_yeswehack_programs,
        category="discovery",
    ))

    async def check_scan_history(params: dict) -> Any:
        from memory.store import PersistentMemory

        store = PersistentMemory()
        try:
            target = params.get("target")
            scans = store.get_past_scans(target=target)
            return scans
        finally:
            store.close()

    registry.register(Tool(
        name="check_scan_history",
        description="Check past scan history from the database. Optionally filter by target domain. Returns list of past scans with timestamps and finding counts.",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Optional target to filter by"}},
            "required": [],
        },
        handler=check_scan_history,
        category="discovery",
    ))

    return registry




# ---------------------------------------------------------------------------
# Analysis / memory tools (3)
# ---------------------------------------------------------------------------


def create_analysis_tools() -> ToolRegistry:
    """Create the tool registry for analysis agents."""
    registry = ToolRegistry()

    async def get_past_findings(params: dict) -> Any:
        from memory.store import PersistentMemory

        store = PersistentMemory()
        try:
            return store.get_past_findings(
                target=params.get("target"),
                vuln_type=params.get("vuln_type"),
            )
        finally:
            store.close()

    registry.register(Tool(
        name="get_past_findings",
        description="Retrieve past vulnerability findings from the database. Filter by target and/or vuln_type to check for duplicates.",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Target domain to filter"},
                "vuln_type": {"type": "string", "description": "Vulnerability type to filter (e.g. subdomain_takeover, info_disclosure)"},
            },
            "required": [],
        },
        handler=get_past_findings,
        category="analysis",
    ))

    async def get_target_history(params: dict) -> Any:
        from memory.learning import LearningMemory
        from memory.store import PersistentMemory

        store = PersistentMemory()
        try:
            learning = LearningMemory(store)
            return learning.get_target_history(params["target"])
        finally:
            store.close()

    registry.register(Tool(
        name="get_target_history",
        description="Get full historical scan data for a target: scan count, findings, bounty earned, ROI.",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Target domain"}},
            "required": ["target"],
        },
        handler=get_target_history,
        category="analysis",
    ))

    async def get_best_vuln_types(params: dict) -> Any:
        from memory.learning import LearningMemory
        from memory.store import PersistentMemory

        store = PersistentMemory()
        try:
            learning = LearningMemory(store)
            return learning.get_best_vuln_types()
        finally:
            store.close()

    registry.register(Tool(
        name="get_best_vuln_types",
        description="Get the historically most successful vulnerability types ranked by ROI (acceptance rate * bounty).",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=get_best_vuln_types,
        category="analysis",
    ))

    return registry


# ---------------------------------------------------------------------------
# Session memory tools (chatroom) — shared between all agents
# ---------------------------------------------------------------------------


def create_session_tools(session_memory) -> ToolRegistry:
    """Create read/write tools for the shared session memory.

    This acts as a "chatroom" where agents can leave notes for each
    other.  Recon can write technologies it found, scanner can read
    them before deciding what to test, etc.

    Args:
        session_memory: A ``SessionMemory`` instance shared across the
            pipeline run.
    """
    registry = ToolRegistry()

    async def read_session_memory(params: dict) -> Any:
        section = params.get("section", "")
        if section:
            # Read a specific section
            value = session_memory.notes.get(section)
            if value is not None:
                return {section: value}
            # Also check tags if the section looks like a domain
            tags = session_memory.get_tags(section)
            if tags:
                return {"tags": tags}
            return f"No notes found for '{section}'. Available sections: {list(session_memory.notes.keys())}"
        # Return everything (compact)
        result = {}
        if session_memory.notes:
            result["notes"] = session_memory.notes
        if session_memory.tags:
            result["tags"] = session_memory.tags
        if session_memory.current_target:
            result["current_target"] = session_memory.current_target
        if session_memory.targets_scanned:
            result["targets_scanned"] = session_memory.targets_scanned
        if session_memory.recon_data:
            # Compact: only keys and short values
            compact = {}
            for k, v in session_memory.recon_data.items():
                if isinstance(v, list) and len(v) > 10:
                    compact[k] = f"[{len(v)} items]: {v[:5]}..."
                else:
                    compact[k] = v
            result["recon_data"] = compact
        return result or "Session memory is empty — no notes from other agents yet."

    registry.register(Tool(
        name="read_session_memory",
        description=(
            "Read the shared session memory (chatroom). Other agents leave "
            "notes here about what they found. Read with no section to see "
            "everything, or specify a section name (e.g. 'recon', 'technologies', "
            "'scanner') to read a specific note."
        ),
        parameters={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "Optional section name to read. Leave empty to see all notes.",
                },
            },
            "required": [],
        },
        handler=read_session_memory,
        category="session",
    ))

    async def write_session_memory(params: dict) -> Any:
        section = params["section"]
        content = params["content"]
        session_memory.notes[section] = content
        return f"Wrote to session memory section '{section}' ({len(content)} chars)."

    registry.register(Tool(
        name="write_session_memory",
        description=(
            "Write a note to the shared session memory for other agents to read. "
            "Use a descriptive section name (e.g. 'recon_summary', 'technologies', "
            "'interesting_endpoints', 'scanner_findings'). Keep notes concise — "
            "other agents will read them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "Section name (e.g. 'recon_summary', 'technologies')",
                },
                "content": {
                    "type": "string",
                    "description": "The note content to store",
                },
            },
            "required": ["section", "content"],
        },
        handler=write_session_memory,
        category="session",
    ))

    return registry


# ---------------------------------------------------------------------------
# Shared memory tools (5) — for swarm agents
# ---------------------------------------------------------------------------


def create_shared_memory_tools(shared_memory, agent_id: str) -> ToolRegistry:
    """Create tools for reading/writing to shared swarm memory.

    Args:
        shared_memory: A ``SharedMemory`` instance shared across the swarm.
        agent_id: The calling agent's identifier (e.g. "pentester-0").
    """
    registry = ToolRegistry()

    async def post_discovery(params: dict) -> Any:
        category = params["category"]
        key = params["key"]
        content = params.get("content", "")
        entry_id = await shared_memory.post(agent_id, "discovery", category, key, content)
        if entry_id == -1:
            return f"Already known: {key}"
        return f"Posted discovery #{entry_id}: {key}"

    registry.register(Tool(
        name="post_discovery",
        description=(
            "Post a discovery to the shared swarm memory. Other agents will see it. "
            "Use category to classify (subdomain, technology, endpoint, vuln, note). "
            "Use a unique key for dedup (e.g. 'subdomain:api.example.com'). "
            "Returns 'Already known' if another agent posted the same key."
        ),
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Category: subdomain, technology, endpoint, vuln, note, etc."},
                "key": {"type": "string", "description": "Unique dedup key (e.g. 'subdomain:api.example.com')"},
                "content": {"type": "string", "description": "Discovery details (JSON or text)"},
            },
            "required": ["category", "key"],
        },
        handler=post_discovery,
        category="shared_memory",
    ))

    async def read_discoveries(params: dict) -> Any:
        category = params.get("category")
        since_id = int(params.get("since_id", 0))
        entries = shared_memory.read_since(entry_id=since_id, category=category)
        score_line = shared_memory.format_score_for_agent(agent_id)
        if not entries:
            msg = f"No new entries since #{since_id}" + (f" in category '{category}'" if category else "")
            return f"{msg}\n{score_line}"
        result = [
            {
                "id": e.id,
                "agent": e.agent_id,
                "type": e.entry_type,
                "category": e.category,
                "key": e.key,
                "content": e.content[:500],
            }
            for e in entries
        ]
        return {"discoveries": result, "your_score": score_line}

    registry.register(Tool(
        name="read_discoveries",
        description=(
            "Read recent discoveries from shared swarm memory. Other agents post "
            "what they find here. Use since_id to get only new entries (incremental). "
            "Use category to filter (subdomain, technology, endpoint, vuln, note)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category (optional)"},
                "since_id": {"type": "integer", "description": "Only return entries newer than this ID (default 0 = all)", "default": 0},
            },
            "required": [],
        },
        handler=read_discoveries,
        category="shared_memory",
    ))

    async def read_findings(params: dict) -> Any:
        status_filter = params.get("status")
        all_pairs = shared_memory.get_findings_with_reporters()
        if status_filter:
            pairs = [
                (f, r) for f, r in all_pairs
                if (f.verification_status.value if hasattr(f.verification_status, "value") else str(f.verification_status)) == status_filter
            ]
        else:
            pairs = all_pairs
        # Status counts across ALL findings (not just filtered)
        all_findings = shared_memory.get_findings()
        counts: dict[str, int] = {"verified": 0, "disproved": 0, "inconclusive": 0, "unverified": 0}
        for f in all_findings:
            vs = f.verification_status.value if hasattr(f.verification_status, "value") else str(f.verification_status)
            counts[vs] = counts.get(vs, 0) + 1
        header = f"Findings: {len(all_findings)} total — verified={counts['verified']}, inconclusive={counts['inconclusive']}, unverified={counts['unverified']}"
        if not pairs:
            filt = f" (filter: status={status_filter})" if status_filter else ""
            return f"{header}\nNo findings{filt}."
        items = []
        for f, reporter in pairs:
            vs = f.verification_status.value if hasattr(f.verification_status, "value") else str(f.verification_status)
            vt = f.vuln_type.value if hasattr(f.vuln_type, "value") else str(f.vuln_type)
            sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
            items.append({
                "id": f.id,
                "target": f.target,
                "vuln_type": vt,
                "severity": sev,
                "verification_status": vs,
                "title": f.title,
                "evidence_snippet": (f.evidence[:200] + "...") if len(f.evidence) > 200 else f.evidence,
                "poc_snippet": (f.poc[:200] + "...") if len(f.poc) > 200 else f.poc,
                "reported_by": reporter,
            })
        return {"summary": header, "findings": items}

    registry.register(Tool(
        name="read_findings",
        description=(
            "Read vulnerability findings from shared memory. Unlike read_discoveries "
            "(which reads the feed/intel stream), this returns actual structured "
            "vulnerability findings submitted by pentesters. Use status to filter: "
            "'unverified', 'verified', 'inconclusive', or omit for all findings. "
            "Shows a status breakdown at the top."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by verification status: 'unverified', 'verified', 'inconclusive'. Omit for all.",
                    "enum": ["unverified", "verified", "inconclusive"],
                },
            },
            "required": [],
        },
        handler=read_findings,
        category="shared_memory",
    ))

    async def claim_target(params: dict) -> Any:
        key = params["key"]
        success, extra = await shared_memory.claim(agent_id, key)
        suffix = f" {extra}" if extra else ""
        if success:
            return f"Claimed: {key} — it's yours exclusively.{suffix}"
        owner = shared_memory.is_claimed(key)
        return f"Already claimed by {owner}. Pick a different target.{suffix}"

    registry.register(Tool(
        name="claim_target",
        description=(
            "Claim exclusive access to a target so other agents don't work on it. "
            "Use before starting intensive scanning on a target. "
            "Key format: 'scan:<domain>' (e.g. 'scan:api.example.com'). "
            "Returns success or tells you who already claimed it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Claim key (e.g. 'scan:api.example.com')"},
            },
            "required": ["key"],
        },
        handler=claim_target,
        category="shared_memory",
    ))

    async def check_claimed(params: dict) -> Any:
        key = params["key"]
        owner = shared_memory.is_claimed(key)
        if owner:
            return f"Claimed by {owner}"
        return f"Not claimed: {key} is available"

    registry.register(Tool(
        name="check_claimed",
        description="Check if a target is already claimed by another agent.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Claim key to check"},
            },
            "required": ["key"],
        },
        handler=check_claimed,
        category="shared_memory",
    ))

    async def submit_finding(params: dict) -> Any:
        from models.finding import VulnFinding as _VF
        finding_data = params.get("finding_json") or params
        if isinstance(finding_data, str):
            finding_data = json.loads(finding_data)
        # Remove the wrapper key if present
        if "finding_json" in finding_data and isinstance(finding_data["finding_json"], (dict, str)):
            raw = finding_data["finding_json"]
            finding_data = json.loads(raw) if isinstance(raw, str) else raw
        try:
            finding = _VF(**finding_data)
        except Exception as exc:
            return f"Invalid finding data: {exc}"
        accepted, reason = await shared_memory.submit_finding(agent_id, finding)
        score_line = shared_memory.format_score_for_agent(agent_id)
        vstatus = finding.verification_status.value if hasattr(finding.verification_status, "value") else str(finding.verification_status)
        if reason:
            return f"REJECTED: {reason} {score_line}"
        if accepted:
            return (
                f"Finding submitted: {finding.title or finding.vuln_type.value} on {finding.target} "
                f"[{vstatus}] {score_line}"
            )
        return f"Duplicate finding (already reported): {finding.vuln_type.value} on {finding.target} {score_line}"

    registry.register(Tool(
        name="submit_finding",
        description=(
            "Submit a vulnerability finding to the director for review. Automatically "
            "deduplicated by (target, OWASP category, url). REQUIRES a POC in the evidence "
            "field: actual HTTP request/response, curl command, or code snippet that "
            "proves the vulnerability. Findings without a real POC are REJECTED. "
            "Provide: target, vuln_type, severity, title, description, evidence (with POC), url. "
            "ALSO provide 'impact': a concrete statement of what an attacker gains and why it "
            "matters (this goes straight into the report — do not leave it generic). "
            "Pick the MOST SPECIFIC vuln_type — do NOT default to 'attack_chain'. Use "
            "'attack_chain' only for a multi-step exploit, and even then set vuln_type to the "
            "PRIMARY class (e.g. sql_injection) and pass 'composed_of' for the steps."
        ),
        parameters={
            "type": "object",
            "properties": {
                "finding_json": {"type": "string", "description": "JSON string of the finding (or pass fields directly)"},
                "target": {"type": "string"},
                "vuln_type": {
                    "type": "string",
                    "description": (
                        "The SPECIFIC vulnerability class. One of: sql_injection, nosql_injection, "
                        "xss, command_injection, file_inclusion, xxe, ssrf, broken_access_control "
                        "(IDOR/BOLA), broken_authentication, mass_assignment, csrf, "
                        "insecure_deserialization, business_logic, rate_limiting, open_redirect, "
                        "cors_misconfiguration, missing_security_headers, secret_exposure, "
                        "sensitive_path, subdomain_takeover, known_cve, misconfiguration, "
                        "info_disclosure, memory_corruption, attack_chain."
                    ),
                },
                "owasp": {"type": "string", "description": "Optional OWASP Top 10:2025 bucket (A01..A10); auto-derived from vuln_type if omitted."},
                "cwe_id": {"type": "integer", "description": "Optional CWE number for precision (e.g. 89 SQLi, 79 XSS, 918 SSRF, 915 mass assignment)."},
                "composed_of": {"type": "array", "items": {"type": "integer"}, "description": "Optional: observation ids this finding chains together (marks it a chain)."},
                "chain_narrative": {"type": "string", "description": "Optional: how individually-benign steps combine into impact."},
                "severity": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "evidence": {"type": "string"},
                "poc": {"type": "string", "description": "Proof of concept: curl command, HTTP request/response, or script that reproduces the vuln"},
                "impact": {"type": "string", "description": "Concrete security impact: what an attacker gains and why it matters. Goes directly into the report — be specific, not generic."},
                "url": {"type": "string"},
                "confidence_score": {"type": "number"},
            },
            "required": [],
        },
        handler=submit_finding,
        category="shared_memory",
    ))

    async def verify_finding(params: dict) -> Any:
        """Attempt to verify an existing finding by re-testing it."""
        from models.finding import VerificationStatus as _VS

        finding_id = params.get("id")
        verification_result = params.get("result", "").strip().lower()
        details = params.get("details", "")
        poc = params.get("poc", "")

        if finding_id is None:
            return "Must provide 'id' (from read_findings) to identify the finding"

        try:
            fid = int(finding_id)
        except (ValueError, TypeError):
            return f"Invalid finding ID: {finding_id} (must be an integer)"

        matched = shared_memory.get_finding_by_id(fid)
        if not matched:
            return f"No finding with id={fid}. Use read_findings to list available findings."

        # Update verification status
        if verification_result in ("verified", "confirmed", "reproduced", "yes"):
            matched.verification_status = _VS.VERIFIED
            matched.reproducible = True
        elif verification_result in ("failed", "disproved", "false_positive", "not_vulnerable", "no"):
            matched.verification_status = _VS.DISPROVED
            matched.reproducible = False
        elif verification_result in ("inconclusive", "unclear", "partial"):
            matched.verification_status = _VS.INCONCLUSIVE
            matched.reproducible = False
        else:
            matched.verification_status = _VS.INCONCLUSIVE

        if details:
            matched.verification_details = details
        if poc:
            matched.poc = poc

        status = matched.verification_status.value
        score_line = shared_memory.format_score_for_agent(agent_id)
        return f"Finding updated: {matched.title} → {status}. {score_line}"

    registry.register(Tool(
        name="verify_finding",
        description=(
            "Verify a previously submitted finding by re-testing it. Use 'id' from "
            "read_findings to identify which finding to verify. Provide result "
            "('verified', 'disproved', or 'inconclusive'), details of how you tested, and optionally a POC."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Finding ID from read_findings"},
                "result": {"type": "string", "description": "'verified', 'disproved' (not a real vuln), or 'inconclusive' (unclear)"},
                "details": {"type": "string", "description": "How you verified (tool used, what you observed)"},
                "poc": {"type": "string", "description": "Proof of concept from the verification attempt"},
            },
            "required": ["id", "result"],
        },
        handler=verify_finding,
        category="shared_memory",
    ))

    return registry


# ---------------------------------------------------------------------------
# Code execution tool (1) — run scripts in Docker
# ---------------------------------------------------------------------------


def create_graph_memory_tools(graph) -> ToolRegistry:
    """Create tools for querying and writing to the FalkorDB knowledge graph.

    Args:
        graph: A ``GraphMemory`` instance (from ``memory.graph``).
    """
    registry = ToolRegistry()

    async def get_similar_targets(params: dict) -> Any:
        domain = params["domain"]
        limit = params.get("limit", 10)
        results = await graph.get_similar_targets(domain, limit=limit)
        if not results:
            return f"No similar targets found for {domain} (graph may not have enough data yet)"
        return results

    registry.register(Tool(
        name="get_similar_targets",
        description=(
            "Find domains with similar tech stacks and their finding history "
            "from the cross-session knowledge graph. Use after recon to see what "
            "worked on similar targets in past sessions. Returns domains sharing "
            "2+ technologies, with their past findings and acceptance status."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Target domain to find similar targets for"},
                "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["domain"],
        },
        handler=get_similar_targets,
        category="graph_memory",
    ))

    async def get_effective_techniques(params: dict) -> Any:
        tech_stack = params["tech_stack"]
        limit = params.get("limit", 10)
        if isinstance(tech_stack, str):
            tech_stack = [t.strip() for t in tech_stack.split(",")]
        results = await graph.get_effective_techniques(tech_stack, limit=limit)
        if not results:
            return f"No effective techniques found for tech stack {tech_stack} (need more historical data)"
        return results

    registry.register(Tool(
        name="get_effective_techniques",
        description=(
            "What vulnerability types and attack techniques work on targets with "
            "this tech stack? Queries the cross-session knowledge graph for accepted "
            "findings on domains using these technologies. Returns vuln types ranked "
            "by success count and average bounty, with specific techniques used."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tech_stack": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of technologies (e.g. ['nginx', 'react', 'express'])",
                },
                "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["tech_stack"],
        },
        handler=get_effective_techniques,
        category="graph_memory",
    ))

    async def query_knowledge_graph(params: dict) -> Any:
        cypher = params["cypher"]
        results = await graph.query(cypher)
        return results

    registry.register(Tool(
        name="query_knowledge_graph",
        description=(
            "Run a custom Cypher query against the knowledge graph for advanced "
            "intelligence. The graph has nodes: Program, Domain, Technology, Finding, "
            "VulnType, Scan, Technique, Endpoint. "
            "Edges: HAS_DOMAIN, USES_TECH, HAS_FINDING, IS_TYPE, USED_TECHNIQUE, "
            "AT_ENDPOINT, SCANNED, FOUND, HAS_ENDPOINT.\n\n"
            "Example queries:\n"
            "- 'MATCH (d:Domain)-[:USES_TECH]->(t) RETURN d.name, collect(t.name) LIMIT 10'\n"
            "- 'MATCH (f:Finding {accepted: true})-[:IS_TYPE]->(v) RETURN v.name, count(f) ORDER BY count(f) DESC'\n"
            "- 'MATCH (d:Domain)-[:HAS_ENDPOINT]->(e)<-[:AT_ENDPOINT]-(f) RETURN e.path, f.severity LIMIT 20'"
        ),
        parameters={
            "type": "object",
            "properties": {
                "cypher": {"type": "string", "description": "Cypher query to execute"},
            },
            "required": ["cypher"],
        },
        handler=query_knowledge_graph,
        category="graph_memory",
        risk_level="low",
    ))

    async def store_graph_finding(params: dict) -> Any:
        target = params["target"]
        vuln_type = params["vuln_type"]
        severity = params.get("severity", "medium")
        title = params.get("title", f"{vuln_type} on {target}")
        technologies = params.get("technologies", [])
        technique = params.get("technique", "")
        endpoint = params.get("endpoint", "")

        finding_dict = {
            "id": hash(f"{target}:{vuln_type}:{title}") & 0x7FFFFFFF,
            "title": title,
            "severity": severity,
            "vuln_type": vuln_type,
            "target": target,
            "technique": technique,
            "url": endpoint,
        }
        await graph.store_finding(finding_dict)

        # Also store technologies if provided
        for tech in technologies:
            await graph.store_technology(target, tech)

        return f"Stored finding in knowledge graph: {title} ({vuln_type} on {target})"

    registry.register(Tool(
        name="store_graph_finding",
        description=(
            "Persist a confirmed finding in the knowledge graph for cross-session "
            "learning. Future sessions will use this data to prioritize similar "
            "targets and techniques. Include technologies and technique used for "
            "maximum learning value."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Target domain"},
                "vuln_type": {"type": "string", "description": "Vulnerability type (e.g. cors_misconfig, ssrf, xss)"},
                "severity": {"type": "string", "description": "Severity: info, low, medium, high, critical"},
                "title": {"type": "string", "description": "Finding title"},
                "technologies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Server-side software detected: web servers (nginx, apache), frameworks (express, django, laravel, spring), databases (mysql, mongodb, redis), CDNs (cloudflare), CMS (wordpress). NOT platforms (android, ios), languages (python, java), or auth providers.",
                },
                "technique": {"type": "string", "description": "Specific technique used (e.g. 'parameter pollution', 'null origin CORS')"},
                "endpoint": {"type": "string", "description": "URL or path where the vuln was found"},
            },
            "required": ["target", "vuln_type"],
        },
        handler=store_graph_finding,
        category="graph_memory",
    ))

    return registry


# ---------------------------------------------------------------------------
# Code execution tool (1) — run scripts in Docker
# ---------------------------------------------------------------------------


def create_code_execution_tools() -> ToolRegistry:
    """Create a tool for executing code inside Docker containers."""
    registry = ToolRegistry()

    TOOLS_IMAGE = "bugbounty-tools:latest"
    LANG_COMMANDS = {
        "python": "python3",
        "bash": "bash",
        "sh": "sh",
        "javascript": "node",
        "ruby": "ruby",
    }

    async def execute_code(params: dict) -> Any:
        from utils.docker_runner import ensure_image, run_container

        code = params["code"]
        language = params.get("language", "python")
        timeout = params.get("timeout", 30)

        interpreter = LANG_COMMANDS.get(language)
        if not interpreter:
            return {"error": f"Unsupported language: {language}. Use: {list(LANG_COMMANDS)}"}

        if not await ensure_image(TOOLS_IMAGE):
            return {"error": f"Docker image '{TOOLS_IMAGE}' not available."}

        # Write code to a temp file via stdin
        cmd = f"cat <<'CODEOF' | {interpreter}\n{code}\nCODEOF"
        exit_code, stdout, stderr = await run_container(TOOLS_IMAGE, [cmd], timeout=timeout)
        return {
            "exit_code": exit_code,
            "stdout": stdout[:4000],
            "stderr": stderr[:1000] if exit_code != 0 else "",
        }

    registry.register(Tool(
        name="execute_code",
        description=(
            "Execute a code snippet inside a Docker container. Supports python, "
            "bash, javascript, ruby. Use for custom exploit scripts, data processing, "
            "or any logic that doesn't fit existing tools. Runs in the same isolated "
            "Docker environment as CLI tools (no host access). "
            "Pre-installed Python packages: requests, httpx, beautifulsoup4, lxml, "
            "pyjwt, cryptography, pycryptodome, paramiko, dnspython, xmltodict, "
            "pyyaml, websockets, aiohttp, faker, boto3. "
            "Also has curl, openssl, jq, nmap, dig available as CLI tools."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The code to execute"},
                "language": {"type": "string", "description": "Language: python, bash, javascript, ruby", "default": "python"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
            },
            "required": ["code"],
        },
        handler=execute_code,
        category="execution",
        risk_level="medium",
    ))

    return registry


# ---------------------------------------------------------------------------
# Research tools — curated toolset for vulnerability researchers
# ---------------------------------------------------------------------------


def create_research_tools() -> ToolRegistry:
    """Curated toolset for vulnerability research.

    Includes only tools that support a researcher's workflow:
    HTTP requests, code execution, Docker CLI tools, web search,
    and targeted helpers (secrets, sensitive paths, regex).

    Excludes automated scanners (security_headers, open_redirect,
    subdomain_takeover, info_disclosure, nuclei_builtin, fuzz_parameter)
    that push the model toward scanner behavior and garbage findings.
    """
    scanner = create_scanner_tools()
    search = create_search_tools()
    code = create_code_execution_tools()

    registry = ToolRegistry()

    # Core: HTTP requests
    _pick(registry, create_recon_tools(), "http_fetch")

    # Core: Docker CLI tools (nmap, nuclei, ffuf, sqlmap, curl, dig, etc.)
    _pick(registry, scanner, "run_docker_tool")

    # Core: code execution (Python, bash, JS, ruby in Docker)
    registry.merge(code)

    # Core: web search for OSINT/CVE research
    registry.merge(search)

    # Core: MCP client (enumerate/exercise remote MCP servers)
    registry.merge(create_mcp_tools())

    return registry


def _pick(dest: ToolRegistry, src: ToolRegistry, name: str) -> None:
    """Copy a single tool from src to dest registry."""
    tool = src.get(name)
    if tool:
        dest.register(tool)


# ---------------------------------------------------------------------------
# MCP client tools — inspect & exercise Model Context Protocol servers
# ---------------------------------------------------------------------------

def create_mcp_tools() -> ToolRegistry:
    """Tools for talking to remote MCP servers using the official ``mcp`` SDK.

    MCP servers (e.g. mcp.opensea.io) expose tools over SSE or Streamable HTTP.
    Hand-rolling the session/transport burns agent budget and is error-prone,
    so these wrap the real client. Lets the agent enumerate a server's tools and
    invoke them — handy for both finding bugs (path traversal in tool args,
    auth gaps) and for the validator to reproduce them properly.
    """
    registry = ToolRegistry()

    async def _with_session(url: str, headers: dict | None, fn):
        """Open an MCP session over the right transport and run ``fn(session)``."""
        try:
            from mcp import ClientSession
        except ImportError:
            return {"error": "mcp package not installed. Add `mcp` to requirements and pip install it."}

        headers = headers or None
        u = url.rstrip("/")
        use_sse = u.endswith("/sse") or "/sse/" in url or "/sse?" in url
        try:
            if use_sse:
                from mcp.client.sse import sse_client
                async with sse_client(url, headers=headers) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await fn(session)
            else:
                from mcp.client.streamable_http import streamablehttp_client
                async with streamablehttp_client(url, headers=headers) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await fn(session)
        except Exception as exc:  # noqa: BLE001 — surface transport/protocol errors to the agent
            return {"error": f"{type(exc).__name__}: {exc}", "transport": "sse" if use_sse else "streamable_http"}

    async def mcp_list_tools(params: dict) -> Any:
        url = params["url"]
        headers = params.get("headers")

        async def _run(session):
            result = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": (t.description or "")[:500],
                    "input_schema": t.inputSchema,
                }
                for t in result.tools
            ]

        return await _with_session(url, headers, _run)

    registry.register(Tool(
        name="mcp_list_tools",
        description=(
            "Connect to a remote MCP (Model Context Protocol) server and list the tools "
            "it exposes, with their input schemas. Transport is auto-detected: URLs ending "
            "in /sse use SSE, otherwise Streamable HTTP. Pass an optional 'headers' object "
            "for auth (e.g. {\"Authorization\": \"Bearer <key>\"} or {\"X-API-KEY\": \"...\"}). "
            "Read the tool descriptions carefully — advertised behavior is intended, not a bug."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "MCP server endpoint (e.g. https://mcp.example.com/sse)"},
                "headers": {"type": "object", "description": "Optional HTTP headers for auth"},
            },
            "required": ["url"],
        },
        handler=mcp_list_tools,
        category="recon",
    ))

    async def mcp_call_tool(params: dict) -> Any:
        url = params["url"]
        tool_name = params["tool"]
        arguments = params.get("arguments") or {}
        headers = params.get("headers")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {"error": "arguments must be a JSON object or valid JSON string"}

        async def _run(session):
            result = await session.call_tool(tool_name, arguments)
            blocks = []
            for c in result.content:
                if getattr(c, "type", None) == "text":
                    blocks.append(getattr(c, "text", ""))
                else:
                    blocks.append(str(c))
            return {
                "is_error": bool(getattr(result, "isError", False)),
                "content": "\n".join(blocks)[:6000],
            }

        return await _with_session(url, headers, _run)

    registry.register(Tool(
        name="mcp_call_tool",
        description=(
            "Invoke a tool on a remote MCP server and return its result. Use mcp_list_tools "
            "first to learn the tool names and argument schemas. Great for probing tool "
            "arguments for injection/traversal (e.g. id='collection_../accounts/me') or for "
            "the validator to reproduce an MCP finding without managing the SSE session by hand. "
            "Remember: to prove data exposure you must reach ANOTHER user's data, not your own."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "MCP server endpoint"},
                "tool": {"type": "string", "description": "Name of the MCP tool to call"},
                "arguments": {"type": "object", "description": "Arguments for the tool (object or JSON string)"},
                "headers": {"type": "object", "description": "Optional HTTP headers for auth"},
            },
            "required": ["url", "tool"],
        },
        handler=mcp_call_tool,
        category="scanner",
        risk_level="medium",
    ))

    return registry
