"""CoordinationPolicy — how agents relate to each other, as data.

This governs ONLY inter-agent coordination: whether a claimed target blocks
other agents from it, how often the director reviews progress, whether teams
share intel cross-team. It has NO authority over scope — out-of-scope hosts
stay blocked by agents/tool_agent.py's _allowed_domains check regardless of
this policy; that check runs first and is never gated by it. Coordination
answers "can two agents both work the same IN-SCOPE target," not "what's
in scope."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CoordinationPolicy:
    # "exclusive": first claim locks a target — matches the original/default
    # behavior. "shared": claims record investigation intent only; multiple
    # agents may work the same target (useful when a mechanism like
    # first_finder_bonus already rewards not colliding, or when the scope has
    # fewer hosts than agents and exclusive locks would idle most of them).
    claim_mode: Literal["exclusive", "shared"] = "exclusive"
    director_review_interval_s: int = 30
    cross_team_sharing: bool = True  # group mode only; no-op with 1 team


DEFAULT_COORDINATION = CoordinationPolicy()
