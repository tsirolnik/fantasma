"""Session-shared state for passing context between pipeline stages."""

from dataclasses import dataclass, field
from typing import Any

from models.program import BountyProgram
from models.finding import VulnFinding


@dataclass
class SessionMemory:
    """Shared context passed between agents within a single pipeline run.

    Each pipeline stage reads from and writes to this object so downstream
    stages can access upstream results without re-fetching.
    """

    # Pipeline-stage outputs
    programs: list[BountyProgram] = field(default_factory=list)
    recon_data: dict[str, Any] = field(default_factory=dict)
    raw_findings: list[VulnFinding] = field(default_factory=list)
    analyzed_findings: list[VulnFinding] = field(default_factory=list)

    # Running context
    current_target: str = ""
    targets_scanned: list[str] = field(default_factory=list)
    targets_skipped: list[str] = field(default_factory=list)

    # Tags / notes agents can leave for each other
    notes: dict[str, str] = field(default_factory=dict)
    tags: dict[str, list[str]] = field(default_factory=dict)

    # Scope rules per program (program_url → raw scope text)
    scope_rules: dict[str, str] = field(default_factory=dict)

    # Cost tracking snapshot
    cost_snapshot: float = 0.0

    def add_note(self, agent: str, note: str) -> None:
        """Leave a note for downstream agents."""
        self.notes[agent] = note

    def add_tags(self, target: str, tags: list[str]) -> None:
        """Tag a target with metadata (e.g., 'wordpress', 'cloudflare')."""
        existing = self.tags.get(target, [])
        self.tags[target] = list(set(existing + tags))

    def get_tags(self, target: str) -> list[str]:
        return self.tags.get(target, [])

    def mark_scanned(self, target: str) -> None:
        if target not in self.targets_scanned:
            self.targets_scanned.append(target)

    def mark_skipped(self, target: str, reason: str = "") -> None:
        if target not in self.targets_skipped:
            self.targets_skipped.append(target)
        if reason:
            self.add_note(f"skip:{target}", reason)

    def summary(self) -> dict:
        return {
            "programs_found": len(self.programs),
            "targets_scanned": len(self.targets_scanned),
            "targets_skipped": len(self.targets_skipped),
            "raw_findings": len(self.raw_findings),
            "analyzed_findings": len(self.analyzed_findings),
            "cost": self.cost_snapshot,
        }
