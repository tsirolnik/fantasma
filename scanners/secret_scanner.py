"""JS and response secret scanner — finds leaked API keys, tokens, and credentials."""

import logging
import re
from urllib.parse import urljoin, urlparse

from models.finding import Severity, VulnFinding, VulnType
from scanners.base import ScannerBase
from utils.http import fetch

logger = logging.getLogger(__name__)

# Regex patterns for common secrets. Each tuple: (name, pattern, severity, confidence)
SECRET_PATTERNS: list[tuple[str, re.Pattern, Severity, float]] = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}"), Severity.CRITICAL, 0.95),
    ("AWS Secret Key", re.compile(r"""(?:aws_secret_access_key|aws_secret)\s*[=:]\s*['"]?([A-Za-z0-9/+=]{40})""", re.I), Severity.CRITICAL, 0.85),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}"), Severity.HIGH, 0.9),
    ("Google OAuth Token", re.compile(r"ya29\.[0-9A-Za-z\-_]+"), Severity.HIGH, 0.85),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,255}"), Severity.CRITICAL, 0.95),
    ("GitHub Classic Token", re.compile(r"ghp_[A-Za-z0-9]{36}"), Severity.CRITICAL, 0.95),
    ("Slack Token", re.compile(r"xox[boaprs]-[0-9a-zA-Z\-]{10,250}"), Severity.HIGH, 0.9),
    ("Slack Webhook", re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[a-zA-Z0-9]+"), Severity.HIGH, 0.95),
    ("Stripe Secret Key", re.compile(r"sk_live_[0-9a-zA-Z]{24,99}"), Severity.CRITICAL, 0.95),
    ("Stripe Publishable Key", re.compile(r"pk_live_[0-9a-zA-Z]{24,99}"), Severity.LOW, 0.9),
    ("Twilio API Key", re.compile(r"SK[0-9a-fA-F]{32}"), Severity.HIGH, 0.7),
    ("Twilio Account SID", re.compile(r"AC[0-9a-fA-F]{32}"), Severity.MEDIUM, 0.7),
    ("Mailgun API Key", re.compile(r"key-[0-9a-zA-Z]{32}"), Severity.HIGH, 0.7),
    ("SendGrid API Key", re.compile(r"SG\.[a-zA-Z0-9_\-]{22}\.[a-zA-Z0-9_\-]{43}"), Severity.CRITICAL, 0.95),
    ("Firebase URL", re.compile(r"https://[a-z0-9-]+\.firebaseio\.com"), Severity.MEDIUM, 0.8),
    ("Private Key", re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"), Severity.CRITICAL, 0.95),
    ("JWT Token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_\-]{10,}"), Severity.HIGH, 0.75),
    ("Heroku API Key", re.compile(r"(?:heroku_api_key|HEROKU_API_KEY)\s*[=:]\s*['\"]*[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", re.I), Severity.HIGH, 0.9),
    ("Generic API Key", re.compile(r"""(?:api_key|apikey|api_secret|access_token|auth_token|client_secret)\s*[=:]\s*['"]([a-zA-Z0-9_\-]{16,64})['"]""", re.I), Severity.MEDIUM, 0.6),
    ("Generic Password", re.compile(r"""(?:password|passwd|pwd)\s*[=:]\s*['"]([^'"]{8,64})['"]""", re.I), Severity.HIGH, 0.5),
    ("Internal IP", re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"), Severity.LOW, 0.6),
]

# Common JS file paths to check
JS_DISCOVERY_PATHS = [
    "/main.js", "/app.js", "/bundle.js", "/vendor.js", "/config.js",
    "/static/js/main.js", "/static/js/app.js",
    "/assets/js/app.js", "/assets/js/main.js",
    "/dist/main.js", "/dist/bundle.js",
    "/js/app.js", "/js/main.js",
    "/_next/static/chunks/main.js",
    "/build/bundle.js",
]


class SecretScanner(ScannerBase):
    """Scans JavaScript files and HTML responses for leaked secrets."""

    scanner_name = "secret_scanner"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        findings: list[VulnFinding] = []
        base_url = f"https://{target}" if not target.startswith("http") else target

        # 1. Scan the main page HTML for secrets and discover JS files
        js_urls = set()
        main_response = await fetch(base_url)
        if main_response and main_response.status_code == 200:
            # Check HTML for secrets
            findings.extend(self._scan_content(main_response.text, base_url, target))

            # Extract JS file URLs from HTML
            js_urls.update(self._extract_js_urls(main_response.text, base_url))

        # 2. Try common JS paths
        for path in JS_DISCOVERY_PATHS:
            js_urls.add(urljoin(base_url, path))

        # 3. Scan each JS file (cap at 20 to avoid excessive requests)
        scanned = 0
        for js_url in sorted(js_urls):
            if scanned >= 20:
                break
            response = await fetch(js_url)
            if response and response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                # Only scan actual JS/text responses
                if any(ct in content_type for ct in ("javascript", "text/", "json", "application/x-")):
                    findings.extend(self._scan_content(response.text, js_url, target))
                    scanned += 1

        # Deduplicate findings by (secret_name, matched_value)
        seen = set()
        deduped = []
        for f in findings:
            key = (f.title, f.evidence[:100])
            if key not in seen:
                seen.add(key)
                deduped.append(f)

        return deduped

    def _extract_js_urls(self, html: str, base_url: str) -> set[str]:
        """Extract JavaScript file URLs from HTML."""
        urls = set()
        # Match src attributes in script tags
        for match in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
            src = match.group(1)
            if src.endswith(".js") or ".js?" in src or "/js/" in src:
                urls.add(urljoin(base_url, src))
        return urls

    def _scan_content(self, content: str, source_url: str, target: str) -> list[VulnFinding]:
        """Scan text content for secret patterns."""
        findings = []
        # Limit content size to avoid regex DoS
        content = content[:500_000]

        for secret_name, pattern, severity, confidence in SECRET_PATTERNS:
            matches = pattern.findall(content)
            if not matches:
                continue

            # Get the first match for evidence
            match_obj = pattern.search(content)
            if not match_obj:
                continue

            # Get surrounding context (50 chars each side)
            start = max(0, match_obj.start() - 50)
            end = min(len(content), match_obj.end() + 50)
            context = content[start:end]
            # Mask the actual secret value (show first 4 and last 4 chars)
            matched = match_obj.group(0)
            if len(matched) > 12:
                masked = matched[:4] + "****" + matched[-4:]
            else:
                masked = matched[:3] + "****"

            findings.append(VulnFinding(
                target=target,
                vuln_type=VulnType.SECRET_EXPOSURE,
                severity=severity,
                title=f"{secret_name} Found in {urlparse(source_url).path or '/'}",
                description=(
                    f"A potential {secret_name} was found exposed in a publicly "
                    f"accessible resource. This could allow unauthorized access "
                    f"to services or data."
                ),
                evidence=(
                    f"Source: {source_url}\n"
                    f"Match: {masked}\n"
                    f"Context: ...{context}..."
                ),
                url=source_url,
                reproducible=True,
                confidence_score=confidence,
                estimated_bounty=500.0 if severity in (Severity.CRITICAL, Severity.HIGH) else 100.0,
            ))

        return findings
