"""Sensitive path and file scanner — extensive path brute-forcing."""

import logging

from models.finding import Severity, VulnFinding, VulnType
from scanners.base import ScannerBase
from utils.http import fetch

logger = logging.getLogger(__name__)

# File extensions that should NOT be served as HTML
_NON_HTML_EXTENSIONS = {
    ".env", ".git", ".sql", ".yml", ".yaml", ".json", ".xml",
    ".php", ".htaccess", ".htpasswd", ".bak", ".zip", ".gz",
    ".config", ".cfg", ".ini", ".log", ".key", ".pem",
}

# JS redirect patterns found in catch-all / SPA pages
_JS_REDIRECT_PATTERNS = (
    "window.location", "document.location", "location.href",
    "location.replace", 'meta http-equiv="refresh"', "meta http-equiv='refresh'",
)


def is_sensitive_path_false_positive(
    response_text: str, content_type: str, path: str
) -> bool:
    """Detect common false-positive patterns for sensitive-path findings.

    Returns True if the response is almost certainly NOT the real file
    (e.g. an SPA catch-all page, or an HTML page served for a .env path).
    """
    text_lower = response_text.lower()
    ct_lower = (content_type or "").lower()

    # Check if the path expects a non-HTML file
    path_lower = path.lower().rstrip("/")
    expects_non_html = any(
        path_lower.endswith(ext) or f"/{ext.lstrip('.')}/" in path_lower or path_lower.endswith(f"/{ext.lstrip('.')}")
        for ext in _NON_HTML_EXTENSIONS
    )
    # Also match paths like /.env.local, /.env.production etc.
    if "/.env" in path_lower or "/.git" in path_lower or "/.htpasswd" in path_lower or "/.htaccess" in path_lower:
        expects_non_html = True

    # 1. HTML content-type for a non-HTML file → false positive
    if expects_non_html and "text/html" in ct_lower:
        return True

    # 2. HTML document markers in a file that shouldn't be HTML
    if expects_non_html and ("<!doctype" in text_lower or "<html" in text_lower):
        return True

    # 3. JS redirect / SPA catch-all detection
    if "<script" in text_lower and any(p in text_lower for p in _JS_REDIRECT_PATTERNS):
        return True

    return False


# Comprehensive list of sensitive paths grouped by category
SENSITIVE_PATHS = [
    # -- Version control --
    {"path": "/.git/HEAD", "indicator": "ref:", "severity": Severity.HIGH,
     "title": "Exposed Git Repository", "bounty": 500.0},
    {"path": "/.git/config", "indicator": "[core]", "severity": Severity.HIGH,
     "title": "Exposed Git Config", "bounty": 500.0},
    {"path": "/.svn/entries", "indicator": "svn", "severity": Severity.HIGH,
     "title": "Exposed SVN Repository", "bounty": 400.0},
    {"path": "/.hg/dirstate", "indicator": None, "severity": Severity.HIGH,
     "title": "Exposed Mercurial Repository", "bounty": 400.0, "status_only": True},

    # -- Environment / Config files --
    {"path": "/.env", "indicator": "=", "severity": Severity.CRITICAL,
     "title": "Exposed .env File", "bounty": 1000.0},
    {"path": "/.env.local", "indicator": "=", "severity": Severity.CRITICAL,
     "title": "Exposed .env.local File", "bounty": 1000.0},
    {"path": "/.env.production", "indicator": "=", "severity": Severity.CRITICAL,
     "title": "Exposed .env.production File", "bounty": 1000.0},
    {"path": "/.env.backup", "indicator": "=", "severity": Severity.CRITICAL,
     "title": "Exposed .env.backup File", "bounty": 1000.0},
    {"path": "/config.yml", "indicator": None, "severity": Severity.HIGH,
     "title": "Exposed config.yml", "bounty": 500.0, "status_only": True},
    {"path": "/config.json", "indicator": None, "severity": Severity.HIGH,
     "title": "Exposed config.json", "bounty": 500.0, "status_only": True},
    {"path": "/wp-config.php.bak", "indicator": "DB_", "severity": Severity.CRITICAL,
     "title": "WordPress Config Backup Exposed", "bounty": 1500.0},
    {"path": "/web.config", "indicator": "configuration", "severity": Severity.HIGH,
     "title": "Exposed IIS web.config", "bounty": 500.0},

    # -- Backups --
    {"path": "/backup.sql", "indicator": "INSERT", "severity": Severity.CRITICAL,
     "title": "SQL Backup File Exposed", "bounty": 2000.0},
    {"path": "/backup.zip", "indicator": None, "severity": Severity.HIGH,
     "title": "Backup Archive Exposed", "bounty": 1000.0, "status_only": True},
    {"path": "/backup.tar.gz", "indicator": None, "severity": Severity.HIGH,
     "title": "Backup Archive Exposed", "bounty": 1000.0, "status_only": True},
    {"path": "/db.sql", "indicator": "INSERT", "severity": Severity.CRITICAL,
     "title": "Database Dump Exposed", "bounty": 2000.0},
    {"path": "/dump.sql", "indicator": "INSERT", "severity": Severity.CRITICAL,
     "title": "Database Dump Exposed", "bounty": 2000.0},

    # -- Debug / Server info --
    {"path": "/phpinfo.php", "indicator": "phpinfo()", "severity": Severity.MEDIUM,
     "title": "PHP Info Page Exposed", "bounty": 200.0},
    {"path": "/info.php", "indicator": "phpinfo()", "severity": Severity.MEDIUM,
     "title": "PHP Info Page Exposed", "bounty": 200.0},
    {"path": "/server-status", "indicator": "Apache Server Status", "severity": Severity.MEDIUM,
     "title": "Apache Server Status Exposed", "bounty": 300.0},
    {"path": "/server-info", "indicator": "Apache Server Information", "severity": Severity.MEDIUM,
     "title": "Apache Server Info Exposed", "bounty": 300.0},
    {"path": "/debug", "indicator": "Traceback", "severity": Severity.HIGH,
     "title": "Debug Endpoint Exposed", "bounty": 500.0},
    {"path": "/_debug/", "indicator": None, "severity": Severity.HIGH,
     "title": "Debug Endpoint Exposed", "bounty": 500.0, "status_only": True},
    {"path": "/trace", "indicator": "TRACE", "severity": Severity.MEDIUM,
     "title": "HTTP TRACE Enabled", "bounty": 200.0},
    {"path": "/elmah.axd", "indicator": "Error Log", "severity": Severity.HIGH,
     "title": "ELMAH Error Log Exposed", "bounty": 500.0},

    # -- Spring Boot Actuator --
    {"path": "/actuator", "indicator": "self", "severity": Severity.MEDIUM,
     "title": "Spring Actuator Root", "bounty": 300.0},
    {"path": "/actuator/health", "indicator": "status", "severity": Severity.LOW,
     "title": "Spring Actuator Health", "bounty": 100.0},
    {"path": "/actuator/env", "indicator": "propertySources", "severity": Severity.CRITICAL,
     "title": "Spring Actuator Env Exposed", "bounty": 1500.0},
    {"path": "/actuator/configprops", "indicator": "contexts", "severity": Severity.HIGH,
     "title": "Spring Actuator Config Props", "bounty": 800.0},
    {"path": "/actuator/heapdump", "indicator": None, "severity": Severity.CRITICAL,
     "title": "Spring Actuator Heap Dump", "bounty": 2000.0, "status_only": True},
    {"path": "/actuator/mappings", "indicator": "dispatcherServlets", "severity": Severity.MEDIUM,
     "title": "Spring Actuator Mappings", "bounty": 400.0},
    {"path": "/actuator/beans", "indicator": "contexts", "severity": Severity.MEDIUM,
     "title": "Spring Actuator Beans", "bounty": 300.0},

    # -- API docs --
    {"path": "/swagger-ui.html", "indicator": "swagger", "severity": Severity.LOW,
     "title": "Swagger UI Exposed", "bounty": 100.0},
    {"path": "/swagger-ui/", "indicator": "swagger", "severity": Severity.LOW,
     "title": "Swagger UI Exposed", "bounty": 100.0},
    {"path": "/api-docs", "indicator": "openapi", "severity": Severity.LOW,
     "title": "API Docs Exposed", "bounty": 100.0},
    {"path": "/api/swagger.json", "indicator": "swagger", "severity": Severity.LOW,
     "title": "Swagger JSON Exposed", "bounty": 100.0},
    {"path": "/v2/api-docs", "indicator": "swagger", "severity": Severity.LOW,
     "title": "Swagger v2 API Docs", "bounty": 100.0},
    {"path": "/v3/api-docs", "indicator": "openapi", "severity": Severity.LOW,
     "title": "OpenAPI v3 Docs", "bounty": 100.0},
    {"path": "/graphql", "indicator": None, "severity": Severity.LOW,
     "title": "GraphQL Endpoint", "bounty": 100.0, "status_only": True},
    {"path": "/graphiql", "indicator": "graphi", "severity": Severity.MEDIUM,
     "title": "GraphiQL IDE Exposed", "bounty": 300.0},

    # -- Admin panels --
    {"path": "/admin/", "indicator": None, "severity": Severity.MEDIUM,
     "title": "Admin Panel Found", "bounty": 200.0, "status_only": True},
    {"path": "/admin/login", "indicator": None, "severity": Severity.MEDIUM,
     "title": "Admin Login Found", "bounty": 200.0, "status_only": True},
    {"path": "/wp-admin/", "indicator": "wp-login", "severity": Severity.LOW,
     "title": "WordPress Admin", "bounty": 50.0},
    {"path": "/wp-login.php", "indicator": "wp-login", "severity": Severity.LOW,
     "title": "WordPress Login", "bounty": 50.0},
    {"path": "/administrator/", "indicator": None, "severity": Severity.MEDIUM,
     "title": "Administrator Panel", "bounty": 200.0, "status_only": True},
    {"path": "/_admin/", "indicator": None, "severity": Severity.MEDIUM,
     "title": "Admin Panel Found", "bounty": 200.0, "status_only": True},
    {"path": "/manage/", "indicator": None, "severity": Severity.MEDIUM,
     "title": "Management Panel Found", "bounty": 200.0, "status_only": True},
    {"path": "/dashboard/", "indicator": None, "severity": Severity.MEDIUM,
     "title": "Dashboard Panel Found", "bounty": 200.0, "status_only": True},

    # -- DevOps/CI --
    {"path": "/Dockerfile", "indicator": "FROM", "severity": Severity.MEDIUM,
     "title": "Dockerfile Exposed", "bounty": 300.0},
    {"path": "/docker-compose.yml", "indicator": "services:", "severity": Severity.HIGH,
     "title": "Docker Compose Exposed", "bounty": 500.0},
    {"path": "/.github/workflows/", "indicator": None, "severity": Severity.LOW,
     "title": "GitHub Actions Workflows", "bounty": 100.0, "status_only": True},
    {"path": "/Jenkinsfile", "indicator": "pipeline", "severity": Severity.MEDIUM,
     "title": "Jenkinsfile Exposed", "bounty": 300.0},
    {"path": "/.circleci/config.yml", "indicator": "version:", "severity": Severity.MEDIUM,
     "title": "CircleCI Config Exposed", "bounty": 300.0},

    # -- Misc sensitive --
    {"path": "/.DS_Store", "indicator": "\x00\x00\x00\x01Bud1", "severity": Severity.LOW,
     "title": "DS_Store File Exposed", "bounty": 50.0},
    {"path": "/crossdomain.xml", "indicator": "allow-access-from", "severity": Severity.MEDIUM,
     "title": "Permissive crossdomain.xml", "bounty": 300.0},
    {"path": "/clientaccesspolicy.xml", "indicator": "allow-from", "severity": Severity.MEDIUM,
     "title": "Permissive Client Access Policy", "bounty": 300.0},
    {"path": "/.htaccess", "indicator": "Rewrite", "severity": Severity.MEDIUM,
     "title": "htaccess File Exposed", "bounty": 300.0},
    {"path": "/.htpasswd", "indicator": ":", "severity": Severity.CRITICAL,
     "title": "htpasswd File Exposed", "bounty": 1500.0},
    {"path": "/sitemap.xml", "indicator": "<url>", "severity": Severity.INFO,
     "title": "Sitemap Found", "bounty": 0.0},
    {"path": "/package.json", "indicator": "dependencies", "severity": Severity.LOW,
     "title": "package.json Exposed", "bounty": 100.0},
    {"path": "/composer.json", "indicator": "require", "severity": Severity.LOW,
     "title": "composer.json Exposed", "bounty": 100.0},
]


class SensitivePathScanner(ScannerBase):
    """Comprehensive sensitive path and file scanner."""

    scanner_name = "sensitive_paths"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        findings: list[VulnFinding] = []
        base_url = f"https://{target}" if not target.startswith("http") else target

        for check in SENSITIVE_PATHS:
            url = f"{base_url}{check['path']}"
            response = await fetch(url)
            if response is None:
                continue

            status_only = check.get("status_only", False)
            indicator = check.get("indicator")

            found = False
            if status_only:
                # For binary/unknown files, just check if we get 200 with content
                found = response.status_code == 200 and len(response.content) > 0
            elif indicator:
                found = response.status_code == 200 and indicator.lower() in response.text.lower()

            if not found:
                continue

            # False-positive filtering
            content_type = response.headers.get("content-type", "")
            if is_sensitive_path_false_positive(response.text or "", content_type, check["path"]):
                logger.debug("Skipping false positive: %s on %s", check["path"], target)
                continue

            # Skip info-level findings
            if check["severity"] == Severity.INFO:
                continue

            snippet = response.text[:500] if response.text else f"[Binary content, {len(response.content)} bytes]"

            findings.append(VulnFinding(
                target=target,
                vuln_type=VulnType.SENSITIVE_PATH,
                severity=check["severity"],
                title=f"{check['title']} on {target}",
                description=(
                    f"The sensitive path {check['path']} is publicly accessible. "
                    f"This may expose internal configuration, credentials, or "
                    f"other sensitive information."
                ),
                evidence=f"URL: {url}\nHTTP Status: {response.status_code}\nSnippet:\n{snippet}",
                url=url,
                reproducible=True,
                confidence_score=0.9,
                estimated_bounty=check.get("bounty", 200.0),
            ))

        return findings
