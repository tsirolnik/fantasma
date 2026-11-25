"""Vulnerability finding data model.

Classification is two-tier + chain:
  * `vuln_type`  — the specific class the agent/scanner identifies (SQLi, XSS,
                   IDOR, ...). Expanded well beyond the old infra-only set.
  * `owasp`      — the standardized OWASP Top 10:2025 bucket, auto-derived from
                   vuln_type when not given. Stable coarse label for reporting,
                   dedup and learning priors.
  * `cwe_id`     — optional precise sub-type; the escape hatch for anything the
                   Top 10 doesn't name (prototype pollution, request smuggling…).
  * Observation + `composed_of` — model a vuln as a CHAIN of individually-benign
                   observations, not only a single pattern match.
"""

from enum import Enum
from pydantic import BaseModel, Field, model_validator


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"      # No reproduction attempted
    VERIFIED = "verified"          # Reproduced with confirming response
    DISPROVED = "disproved"        # Tested and confirmed NOT a real vulnerability
    INCONCLUSIVE = "inconclusive"  # Attempted but result ambiguous


class OwaspCategory(str, Enum):
    """OWASP Top 10:2025 (final edition, released Jan 2026). NOTE 2025 != 2021:
    SSRF merged into A01, Injection is A05, Security Misconfiguration is A02."""
    A01_BROKEN_ACCESS_CONTROL = "A01:broken_access_control"          # IDOR/BOLA, forced browsing, path traversal, open redirect, SSRF, CSRF
    A02_SECURITY_MISCONFIGURATION = "A02:security_misconfiguration"  # headers, CORS, verbose errors, default config, subdomain takeover, XXE
    A03_SUPPLY_CHAIN_FAILURES = "A03:software_supply_chain_failures"  # known CVEs, outdated/compromised deps
    A04_CRYPTOGRAPHIC_FAILURES = "A04:cryptographic_failures"        # secret/sensitive-data exposure, weak crypto
    A05_INJECTION = "A05:injection"                                  # SQLi, NoSQLi, XSS, command injection, LFI/RFI
    A06_INSECURE_DESIGN = "A06:insecure_design"                      # logic flaws, mass assignment, missing rate-limit
    A07_AUTH_FAILURES = "A07:auth_failures"                          # broken auth, weak/default creds, session, JWT
    A08_INTEGRITY_FAILURES = "A08:integrity_failures"                # insecure deserialization, CI/CD, unsigned updates
    A09_LOGGING_FAILURES = "A09:logging_alerting_failures"
    A10_EXCEPTIONAL_CONDITIONS = "A10:mishandling_exceptional_conditions"
    OTHER = "other"                                                  # non-web (e.g. binary memory corruption)


class VulnType(str, Enum):
    # --- Original infra / recon-oriented set (kept; scanners rely on these) ---
    SUBDOMAIN_TAKEOVER = "subdomain_takeover"
    INFO_DISCLOSURE = "info_disclosure"
    CORS_MISCONFIG = "cors_misconfiguration"
    MISSING_HEADERS = "missing_security_headers"
    OPEN_REDIRECT = "open_redirect"
    MEMORY_CORRUPTION = "memory_corruption"
    SECRET_EXPOSURE = "secret_exposure"
    SENSITIVE_PATH = "sensitive_path"
    KNOWN_CVE = "known_cve"
    MISCONFIGURATION = "misconfiguration"
    ATTACK_CHAIN = "attack_chain"
    # --- Web-app classes (the gap that forced everything into attack_chain) ---
    SQL_INJECTION = "sql_injection"
    NOSQL_INJECTION = "nosql_injection"
    XSS = "xss"
    COMMAND_INJECTION = "command_injection"
    FILE_INCLUSION = "file_inclusion"          # LFI / RFI / path traversal
    XXE = "xxe"
    SSRF = "ssrf"
    BROKEN_ACCESS_CONTROL = "broken_access_control"   # IDOR / BOLA / BFLA
    BROKEN_AUTH = "broken_authentication"
    MASS_ASSIGNMENT = "mass_assignment"
    CSRF = "csrf"
    INSECURE_DESERIALIZATION = "insecure_deserialization"
    BUSINESS_LOGIC = "business_logic"
    RATE_LIMITING = "rate_limiting"


# Every VulnType maps to exactly one OWASP:2025 bucket.
VULNTYPE_TO_OWASP: dict[VulnType, OwaspCategory] = {
    VulnType.SUBDOMAIN_TAKEOVER:       OwaspCategory.A02_SECURITY_MISCONFIGURATION,
    VulnType.INFO_DISCLOSURE:          OwaspCategory.A02_SECURITY_MISCONFIGURATION,
    VulnType.CORS_MISCONFIG:           OwaspCategory.A02_SECURITY_MISCONFIGURATION,
    VulnType.MISSING_HEADERS:          OwaspCategory.A02_SECURITY_MISCONFIGURATION,
    VulnType.MISCONFIGURATION:         OwaspCategory.A02_SECURITY_MISCONFIGURATION,
    VulnType.XXE:                      OwaspCategory.A02_SECURITY_MISCONFIGURATION,
    VulnType.OPEN_REDIRECT:            OwaspCategory.A01_BROKEN_ACCESS_CONTROL,
    VulnType.SENSITIVE_PATH:           OwaspCategory.A01_BROKEN_ACCESS_CONTROL,
    VulnType.SSRF:                     OwaspCategory.A01_BROKEN_ACCESS_CONTROL,
    VulnType.BROKEN_ACCESS_CONTROL:    OwaspCategory.A01_BROKEN_ACCESS_CONTROL,
    VulnType.CSRF:                     OwaspCategory.A01_BROKEN_ACCESS_CONTROL,
    VulnType.SECRET_EXPOSURE:          OwaspCategory.A04_CRYPTOGRAPHIC_FAILURES,
    VulnType.KNOWN_CVE:                OwaspCategory.A03_SUPPLY_CHAIN_FAILURES,
    VulnType.SQL_INJECTION:            OwaspCategory.A05_INJECTION,
    VulnType.NOSQL_INJECTION:          OwaspCategory.A05_INJECTION,
    VulnType.XSS:                      OwaspCategory.A05_INJECTION,
    VulnType.COMMAND_INJECTION:        OwaspCategory.A05_INJECTION,
    VulnType.FILE_INCLUSION:           OwaspCategory.A05_INJECTION,
    VulnType.BROKEN_AUTH:              OwaspCategory.A07_AUTH_FAILURES,
    VulnType.MASS_ASSIGNMENT:          OwaspCategory.A06_INSECURE_DESIGN,
    VulnType.BUSINESS_LOGIC:           OwaspCategory.A06_INSECURE_DESIGN,
    VulnType.RATE_LIMITING:            OwaspCategory.A06_INSECURE_DESIGN,
    VulnType.ATTACK_CHAIN:             OwaspCategory.A06_INSECURE_DESIGN,
    VulnType.INSECURE_DESERIALIZATION: OwaspCategory.A08_INTEGRITY_FAILURES,
    VulnType.MEMORY_CORRUPTION:        OwaspCategory.OTHER,
}


class Observation(BaseModel):
    """A single low-level signal. May be completely benign on its own — the
    building block a chain is assembled from (predictable IDs, reflected param,
    verbose stack trace, exposed .git, ...)."""
    id: int = -1
    target: str
    url: str = ""
    kind: str = ""            # free-form: "reflected_param", "sequential_id", "verbose_error"
    detail: str = ""
    evidence: str = ""
    benign_alone: bool = True


class VulnFinding(BaseModel):
    """Represents a discovered vulnerability — atomic OR a chain."""

    id: int = -1  # Assigned by SharedMemory/SQLite on submission
    target: str  # domain or binary path

    # --- Classification -------------------------------------------------
    vuln_type: VulnType
    owasp: OwaspCategory | None = None   # auto-derived from vuln_type if omitted
    cwe_id: int | None = None            # precise sub-type (e.g. 89 SQLi, 79 XSS)

    severity: Severity = Severity.INFO
    title: str = ""
    description: str = ""
    evidence: str = ""  # Raw proof data
    url: str = ""  # Affected URL if applicable
    poc: str = ""  # Proof of concept: curl command, HTTP request/response, or script
    impact: str = ""  # Author-written security impact (why this matters / what an attacker gains)

    # --- Chain modeling -------------------------------------------------
    composed_of: list[int] = Field(default_factory=list)  # Observation ids that make up this finding
    chain_narrative: str = ""                             # how the benign steps combine into impact

    reproducible: bool = False
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_details: str = ""  # How/when verification was performed
    confidence_score: float = 0.0  # 0.0-1.0
    estimated_bounty: float = 0.0

    # AI analysis fields (filled by analyzer)
    ai_severity: Severity | None = None
    ai_exploitability: str = ""
    ai_false_positive_likelihood: float = 0.0
    ai_bounty_estimate: float = 0.0

    @model_validator(mode="after")
    def _derive_owasp(self) -> "VulnFinding":
        if self.owasp is None and self.vuln_type is not None:
            self.owasp = VULNTYPE_TO_OWASP.get(self.vuln_type, OwaspCategory.OTHER)
        return self

    @property
    def is_chain(self) -> bool:
        return len(self.composed_of) > 1

    @property
    def effective_severity(self) -> Severity:
        return self.ai_severity or self.severity

    @property
    def dedup_key(self) -> tuple:
        """Stable identity for dedup. Chains dedupe on their step-set; atomic
        findings on OWASP category + url."""
        owasp = self.owasp.value if self.owasp else self.vuln_type.value
        if self.is_chain:
            return (self.target, owasp, tuple(sorted(self.composed_of)))
        return (self.target, owasp, self.url)

    def __str__(self) -> str:
        return f"[{self.effective_severity.value.upper()}] {self.title} on {self.target}"
