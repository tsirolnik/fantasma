"""Technology fingerprinting scanner — Wappalyzer-style tech detection."""

import logging
import re

from scanners.base import ScannerBase
from models.finding import VulnFinding
from utils.http import fetch

logger = logging.getLogger(__name__)

TECH_SIGNATURES = {
    "WordPress": {
        "headers": {},
        "body_patterns": [r"/wp-content/", r"/wp-includes/", r"wp-json"],
        "paths": ["/wp-admin/", "/wp-login.php"],
    },
    "Drupal": {
        "headers": {"x-generator": "Drupal"},
        "body_patterns": [r"Drupal.settings", r"/sites/default/files"],
        "paths": ["/core/misc/drupal.js"],
    },
    "Joomla": {
        "headers": {},
        "body_patterns": [r"/media/jui/", r"Joomla!"],
        "paths": ["/administrator/"],
    },
    "React": {
        "headers": {},
        "body_patterns": [r"react\.production\.min\.js", r"__NEXT_DATA__", r"_reactRoot"],
        "paths": [],
    },
    "Next.js": {
        "headers": {"x-powered-by": "Next.js"},
        "body_patterns": [r"__NEXT_DATA__", r"/_next/static"],
        "paths": [],
    },
    "Laravel": {
        "headers": {},
        "body_patterns": [r"laravel_session", r"csrf-token"],
        "paths": [],
    },
    "Django": {
        "headers": {},
        "body_patterns": [r"csrfmiddlewaretoken", r"__django_"],
        "paths": [],
    },
    "Express": {
        "headers": {"x-powered-by": "Express"},
        "body_patterns": [],
        "paths": [],
    },
    "Nginx": {
        "headers": {"server": "nginx"},
        "body_patterns": [],
        "paths": [],
    },
    "Apache": {
        "headers": {"server": "Apache"},
        "body_patterns": [],
        "paths": [],
    },
    "Cloudflare": {
        "headers": {"server": "cloudflare"},
        "body_patterns": [],
        "paths": [],
    },
    "AWS": {
        "headers": {"server": "AmazonS3"},
        "body_patterns": [],
        "paths": [],
    },
    "Spring Boot": {
        "headers": {},
        "body_patterns": [r"Whitelabel Error Page"],
        "paths": ["/actuator/health"],
    },
    "ASP.NET": {
        "headers": {"x-aspnet-version": "", "x-powered-by": "ASP.NET"},
        "body_patterns": [r"__VIEWSTATE", r"__EVENTVALIDATION"],
        "paths": [],
    },
}


class TechFingerprintScanner(ScannerBase):
    """Detects technologies used by a target. Not a vulnerability scanner per se,
    but provides recon data useful for other scanners."""

    scanner_name = "tech_fingerprint"

    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        """Returns empty findings list but populates recon_data['technologies']."""
        base_url = f"https://{target}" if not target.startswith("http") else target
        response = await fetch(base_url)
        if response is None:
            return []

        detected = []
        headers = {k.lower(): v.lower() for k, v in response.headers.items()}
        body = response.text

        for tech_name, sig in TECH_SIGNATURES.items():
            # Check headers
            for header_key, header_val in sig["headers"].items():
                if header_key in headers:
                    if not header_val or header_val.lower() in headers[header_key]:
                        detected.append(tech_name)
                        break

            if tech_name in detected:
                continue

            # Check body patterns
            for pattern in sig["body_patterns"]:
                if re.search(pattern, body, re.IGNORECASE):
                    detected.append(tech_name)
                    break

        # Store in recon_data if provided
        if recon_data is not None:
            recon_data["technologies"] = detected

        if detected:
            logger.info(f"Technologies detected on {target}: {', '.join(detected)}")

        return []  # Tech fingerprinting doesn't produce vulnerability findings
