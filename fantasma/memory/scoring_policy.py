"""ScoringPolicy — the point values that make up the default scoring, as data.

Before this module, severity rewards, rejection/duplicate penalties, and the
termination threshold were module-level constants baked directly into
SharedMemory's methods — there was exactly one scoring behavior, and it
could only be changed by editing memory/shared.py itself.

DEFAULT_SCORING is that same behavior, byte-for-byte, now expressed as data a
Profile can swap out. A new profile that wants harsher penalties, a
different severity scale, or no termination at all writes a new
ScoringPolicy instance — it never touches shared.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScoringPolicy:
    accepted: dict[str, int] = field(
        default_factory=lambda: {"critical": 50, "high": 30, "medium": 15, "low": 5})
    default_accepted: int = 10        # fallback when severity isn't one of the above
    rejected: int = -10               # quality-gate rejection (auto-reject type, no evidence, out-of-scope, excluded)
    duplicate: int = -5               # exact repeat of (target, vuln_type, url)
    termination_threshold: int = -50  # score <= this -> agent is replaced


# The original, unchanged scoring — now just the default VALUE of a
# ScoringPolicy rather than the only possible one.
DEFAULT_SCORING = ScoringPolicy()
