"""Nuclei-style template scanner — checks for known CVEs and misconfigurations."""

import logging
import re

from models.finding import Severity, VulnFinding, VulnType
from scanners.base import ScannerBase
from utils.http import fetch

logger = logging.getLogger(__name__)


# Each template is a dict with:
#   id, name, severity, method, path, headers (optional),
#   matchers (list of {type, value} — "status", "body", "header", "regex"),
#   match_condition ("and"/"or", default "and"),
#   description, bounty
TEMPLATES = [
    # -- Known CVEs --
    {
        "id": "CVE-2017-5638",
        "name": "Apache Struts2 RCE (CVE-2017-5638)",
        "severity": Severity.CRITICAL,
        "method": "GET",
        "path": "/",
        "headers": {"Content-Type": "%{#context['com.opensymphony.xwork2.dispatcher.HttpServletResponse'].addHeader('X-Struts-Test','active')}.multipart/form-data"},
        "matchers": [{"type": "header", "value": "X-Struts-Test: active"}],
        "description": "Apache Struts2 is vulnerable to remote code execution via crafted Content-Type header.",
        "bounty": 5000.0,
    },
    {
        "id": "CVE-2021-44228",
        "name": "Log4Shell Detection (CVE-2021-44228)",
        "severity": Severity.CRITICAL,
        "method": "GET",
        "path": "/",
        "headers": {"X-Api-Version": "${jndi:ldap://127.0.0.1/a}", "User-Agent": "${jndi:ldap://127.0.0.1/a}"},
        "matchers": [{"type": "status", "value": "500"}],
        "match_condition": "or",
        "description": "Application may be vulnerable to Log4Shell (Log4j RCE). A 500 error on JNDI payload suggests the payload was processed.",
        "bounty": 5000.0,
        "confidence": 0.3,  # Low confidence — 500 alone is weak signal
    },
    {
        "id": "CVE-2023-44487",
        "name": "HTTP/2 Rapid Reset (CVE-2023-44487)",
        "severity": Severity.MEDIUM,
        "method": "GET",
        "path": "/",
        "matchers": [{"type": "header", "value": "HTTP/2"}],
        "description": "Server supports HTTP/2 and may be vulnerable to Rapid Reset DoS.",
        "bounty": 500.0,
        "confidence": 0.2,
    },

    # -- Exposed panels & dashboards --
    {
        "id": "kibana-exposed",
        "name": "Kibana Dashboard Exposed",
        "severity": Severity.HIGH,
        "method": "GET",
        "path": "/app/kibana",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "kibana"},
        ],
        "description": "Kibana dashboard is publicly accessible without authentication.",
        "bounty": 1000.0,
    },
    {
        "id": "elasticsearch-exposed",
        "name": "Elasticsearch Exposed",
        "severity": Severity.CRITICAL,
        "method": "GET",
        "path": "/_cat/indices",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "health"},
        ],
        "description": "Elasticsearch indices are publicly accessible.",
        "bounty": 2000.0,
    },
    {
        "id": "jenkins-exposed",
        "name": "Jenkins Dashboard Exposed",
        "severity": Severity.HIGH,
        "method": "GET",
        "path": "/",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "Dashboard [Jenkins]"},
        ],
        "description": "Jenkins CI dashboard is publicly accessible.",
        "bounty": 1000.0,
    },
    {
        "id": "jenkins-script",
        "name": "Jenkins Script Console",
        "severity": Severity.CRITICAL,
        "method": "GET",
        "path": "/script",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "Script Console"},
        ],
        "description": "Jenkins script console is accessible — allows arbitrary code execution.",
        "bounty": 5000.0,
    },
    {
        "id": "grafana-exposed",
        "name": "Grafana Dashboard Exposed",
        "severity": Severity.MEDIUM,
        "method": "GET",
        "path": "/api/health",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "database"},
        ],
        "description": "Grafana health endpoint is publicly accessible.",
        "bounty": 500.0,
    },
    {
        "id": "prometheus-exposed",
        "name": "Prometheus Metrics Exposed",
        "severity": Severity.MEDIUM,
        "method": "GET",
        "path": "/metrics",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "# HELP"},
        ],
        "description": "Prometheus metrics endpoint is publicly accessible, exposing internal application metrics.",
        "bounty": 500.0,
    },
    {
        "id": "phpmyadmin-exposed",
        "name": "phpMyAdmin Exposed",
        "severity": Severity.HIGH,
        "method": "GET",
        "path": "/phpmyadmin/",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "phpMyAdmin"},
        ],
        "description": "phpMyAdmin database management interface is publicly accessible.",
        "bounty": 800.0,
    },

    # -- Cloud / Infrastructure misconfigs --
    {
        "id": "aws-metadata",
        "name": "AWS Instance Metadata Accessible",
        "severity": Severity.CRITICAL,
        "method": "GET",
        "path": "/latest/meta-data/",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "ami-id"},
        ],
        "description": "AWS EC2 instance metadata service is accessible, potentially leaking IAM credentials.",
        "bounty": 3000.0,
    },
    {
        "id": "firebase-db-open",
        "name": "Firebase Database Open",
        "severity": Severity.CRITICAL,
        "method": "GET",
        "path": "/.json",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "regex", "value": r'^\s*\{'},
        ],
        "description": "Firebase Realtime Database is publicly readable without authentication.",
        "bounty": 2000.0,
    },

    # -- Misconfiguration --
    {
        "id": "directory-listing",
        "name": "Directory Listing Enabled",
        "severity": Severity.LOW,
        "method": "GET",
        "path": "/images/",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "Index of"},
        ],
        "description": "Directory listing is enabled, exposing file structure.",
        "bounty": 100.0,
    },
    {
        "id": "directory-listing-2",
        "name": "Directory Listing Enabled",
        "severity": Severity.LOW,
        "method": "GET",
        "path": "/assets/",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "Index of"},
        ],
        "description": "Directory listing is enabled, exposing file structure.",
        "bounty": 100.0,
    },
    {
        "id": "trace-method",
        "name": "HTTP TRACE Method Enabled",
        "severity": Severity.MEDIUM,
        "method": "TRACE",
        "path": "/",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "TRACE / HTTP"},
        ],
        "description": "HTTP TRACE method is enabled. This can be exploited for Cross-Site Tracing (XST) attacks.",
        "bounty": 300.0,
    },
    {
        "id": "options-method",
        "name": "Dangerous HTTP Methods Allowed",
        "severity": Severity.MEDIUM,
        "method": "OPTIONS",
        "path": "/",
        "matchers": [
            {"type": "header", "value": "PUT"},
        ],
        "description": "Server allows dangerous HTTP methods (PUT/DELETE) via OPTIONS response.",
        "bounty": 300.0,
    },

    # -- WordPress specific --
    {
        "id": "wp-user-enum",
        "name": "WordPress User Enumeration",
        "severity": Severity.LOW,
        "method": "GET",
        "path": "/wp-json/wp/v2/users",
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": '"slug"'},
        ],
        "description": "WordPress REST API exposes user information including usernames.",
        "bounty": 100.0,
    },
    {
        "id": "wp-xmlrpc",
        "name": "WordPress XML-RPC Enabled",
        "severity": Severity.MEDIUM,
        "method": "POST",
        "path": "/xmlrpc.php",
        "headers": {"Content-Type": "text/xml"},
        "matchers": [
            {"type": "status", "value": "200"},
            {"type": "body", "value": "XML-RPC server accepts POST requests only"},
        ],
        "match_condition": "or",
        "description": "WordPress XML-RPC is enabled, allowing brute force and DDoS amplification attacks.",
        "bounty": 200.0,
    },

    # -- Version disclosure --
    {
        "id": "nginx-version",
        "name": "Nginx Version Disclosure",
        "severity": Severity.INFO,
        "method": "GET",
        "path": "/",
        "matchers": [
            {"type": "header_regex", "value": r"nginx/\d+\.\d+"},
        ],
        "description": "Server discloses Nginx version in headers.",
        "bounty": 0.0,
        "confidence": 0.5,
    },
    {
        "id": "apache-version",
        "name": "Apache Version Disclosure",
        "severity": Severity.INFO,
        "method": "GET",
        "path": "/",
        "matchers": [
            {"type": "header_regex", "value": r"Apache/\d+\.\d+"},
        ],
        "description": "Server discloses Apache version in headers.",
        "bounty": 0.0,
        "confidence": 0.5,
    },
]


class NucleiScanner(ScannerBase):
    """Template-based scanner for known CVEs and misconfigurations."""

    scanner_name = "nuclei_templates"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        return await self.scan_with_templates(target, TEMPLATES)

    async def scan_with_templates(
        self,
        target: str,
        templates: list[dict] | None = None,
    ) -> list[VulnFinding]:
        """Run specific templates against a target."""
        findings: list[VulnFinding] = []
        base_url = f"https://{target}" if not target.startswith("http") else target
        templates = templates or TEMPLATES

        for tmpl in templates:
            # Skip info-level templates
            if tmpl["severity"] == Severity.INFO:
                continue

            url = f"{base_url}{tmpl['path']}"
            method = tmpl.get("method", "GET")
            headers = tmpl.get("headers")

            response = await fetch(url, method=method, headers=headers)
            if response is None:
                continue

            matched = self._check_matchers(
                response, tmpl["matchers"], tmpl.get("match_condition", "and")
            )

            if matched:
                snippet = response.text[:300] if response.text else ""
                confidence = tmpl.get("confidence", 0.85)
                findings.append(VulnFinding(
                    target=target,
                    vuln_type=VulnType.KNOWN_CVE if tmpl["id"].startswith("CVE") else VulnType.MISCONFIGURATION,
                    severity=tmpl["severity"],
                    title=f"{tmpl['name']} on {target}",
                    description=tmpl["description"],
                    evidence=(
                        f"Template: {tmpl['id']}\n"
                        f"URL: {url}\n"
                        f"Method: {method}\n"
                        f"Status: {response.status_code}\n"
                        f"Response: {snippet}"
                    ),
                    url=url,
                    reproducible=True,
                    confidence_score=confidence,
                    estimated_bounty=tmpl.get("bounty", 200.0),
                ))

        return findings

    @staticmethod
    def _check_matchers(response, matchers: list[dict], condition: str = "and") -> bool:
        """Check if response matches all (and) or any (or) of the matchers."""
        results = []

        for matcher in matchers:
            match_type = matcher["type"]
            value = matcher["value"]

            if match_type == "status":
                results.append(str(response.status_code) == value)
            elif match_type == "body":
                results.append(value.lower() in response.text.lower())
            elif match_type == "header":
                header_str = str(response.headers)
                results.append(value.lower() in header_str.lower())
            elif match_type == "regex":
                results.append(bool(re.search(value, response.text)))
            elif match_type == "header_regex":
                header_str = "\n".join(f"{k}: {v}" for k, v in response.headers.items())
                results.append(bool(re.search(value, header_str)))
            else:
                results.append(False)

        if condition == "or":
            return any(results)
        return all(results)
