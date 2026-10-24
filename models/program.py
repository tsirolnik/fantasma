"""Bounty program data model."""

from pydantic import BaseModel, Field


class BountyProgram(BaseModel):
    """Represents a bug bounty program from any platform."""

    name: str
    platform: str  # hackerone, bugcrowd, intigriti
    url: str
    scope_domains: list[str] = Field(default_factory=list)
    scope_wildcards: list[str] = Field(default_factory=list)
    min_bounty: float = 0.0
    max_bounty: float = 0.0
    avg_bounty: float = 0.0
    response_time_days: float = 30.0
    resolved_count: int = 0
    competition_score: float = 1.0  # Higher = more competition
    active: bool = True

    # Scope details (populated by browser-based scope reading)
    scope_instructions: str = ""
    out_of_scope: list[str] = Field(default_factory=list)
    excluded_vuln_types: list[str] = Field(default_factory=list)
    required_auth: str = ""
    scope_raw_text: str = ""

    # Calculated fields
    roi_score: float = 0.0
    estimated_scan_cost: float = 0.0
    expected_value: float = 0.0

    @property
    def all_domains(self) -> list[str]:
        return self.scope_domains + self.scope_wildcards

    def __str__(self) -> str:
        return f"{self.name} ({self.platform}) - ${self.min_bounty}-${self.max_bounty}"
