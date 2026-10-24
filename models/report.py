"""Bounty report data model."""

from datetime import datetime
from pydantic import BaseModel, Field

from models.finding import VulnFinding, Severity


class BountyReport(BaseModel):
    """A submission-ready vulnerability report."""

    title: str
    severity: Severity
    finding: VulnFinding
    description: str = ""
    steps_to_reproduce: list[str] = Field(default_factory=list)
    impact: str = ""
    remediation: str = ""
    references: list[str] = Field(default_factory=list)
    rendered_markdown: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    estimated_bounty: float = 0.0
    estimated_cost: float = 0.0
    roi_ratio: float = 0.0

    def __str__(self) -> str:
        return f"Report: {self.title} [{self.severity.value}] (est. ${self.estimated_bounty})"
