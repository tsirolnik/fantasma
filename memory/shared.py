"""SharedMemory — thread-safe shared memory for concurrent swarm agents.

Provides three coordination mechanisms:
1. Feed: append-only log of discoveries, deduplicated by key
2. Claims: exclusive locks so agents don't scan the same target
3. Findings: deduplicated vulnerability findings
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import Any

from models.finding import VulnFinding, VerificationStatus
from memory.mechanisms import Mechanism, build_mechanisms
from memory.scoring_policy import ScoringPolicy, DEFAULT_SCORING
from memory.coordination_policy import CoordinationPolicy, DEFAULT_COORDINATION


logger = logging.getLogger(__name__)

# Vuln types that virtually never pay bounty — auto-reject
ALWAYS_REJECTED_TYPES = {
    "missing_security_headers",
}

# Back-compat aliases: the actual values now live in ScoringPolicy
# (memory/scoring_policy.py) and are swappable per-profile via
# SharedMemory(scoring=...). These module constants mirror DEFAULT_SCORING so
# any external code importing them directly keeps working unchanged.
SCORE_FINDING_ACCEPTED = DEFAULT_SCORING.accepted
SCORE_FINDING_REJECTED = DEFAULT_SCORING.rejected
SCORE_FINDING_DUPLICATE = DEFAULT_SCORING.duplicate
SCORE_TERMINATION_THRESHOLD = DEFAULT_SCORING.termination_threshold


@dataclass
class AgentScore:
    """Tracks an agent's cumulative score and event history."""

    total: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def add(self, delta: int, reason: str) -> None:
        self.total += delta
        self.events.append({"delta": delta, "reason": reason, "t": time.time()})
        # Keep only last 30 events to avoid unbounded growth
        if len(self.events) > 30:
            self.events = self.events[-30:]


@dataclass
class MemoryEntry:
    """A single entry in the shared memory feed."""

    id: int
    agent_id: str
    entry_type: str  # "discovery" | "finding" | "claim" | "note"
    category: str  # "subdomain" | "technology" | "vuln" | "endpoint" | ...
    key: str  # dedup key, e.g. "subdomain:api.example.com"
    content: str  # JSON payload or plain text
    timestamp: float = field(default_factory=time.time)


class SharedMemory:
    """Thread-safe shared memory for concurrent pen-tester agents.

    All mutations are protected by an asyncio.Lock. Reads are safe without
    the lock because the feed is append-only and we run on a single event loop.
    """

    def __init__(self, scoring: ScoringPolicy | None = None,
                 coordination: CoordinationPolicy | None = None) -> None:
        self._entries: list[MemoryEntry] = []
        self._entry_keys: set[str] = set()
        self._claims: dict[str, str] = {}  # key -> agent_id
        self._findings: list[VulnFinding] = []
        self._finding_keys: set[str] = set()
        self._finding_reporters: list[str] = []  # parallel to _findings: agent_id per finding
        self._lock = asyncio.Lock()
        self._next_id = 1
        # Optional callback: called with (agent_id, finding) when a new finding is submitted
        self.on_finding: Any = None
        # Quality gate: set by pipeline to enforce scope and excluded types
        self.allowed_domains: set[str] | None = None
        self.excluded_vuln_types: set[str] | None = None
        # Agent scoring. The point VALUES are a ScoringPolicy (swappable per
        # profile — see memory/scoring_policy.py); default is the original,
        # unchanged default values.
        self._scores: dict[str, AgentScore] = {}  # agent_id -> AgentScore
        self.scoring: ScoringPolicy = scoring or DEFAULT_SCORING
        self.coordination: CoordinationPolicy = coordination or DEFAULT_COORDINATION
        # Optional pluggable game-theory mechanisms (see memory/mechanisms.py),
        # ADDITIVE on top of the scoring above — empty by default (unmodified baseline).
        self.mechanisms: list[Mechanism] = []

    # ------------------------------------------------------------------
    # Feed methods
    # ------------------------------------------------------------------

    async def post(
        self,
        agent_id: str,
        entry_type: str,
        category: str,
        key: str,
        content: str,
    ) -> int:
        """Append an entry to the feed. Deduplicates by key.

        Returns the entry ID, or -1 if the key already existed.
        """
        async with self._lock:
            if key in self._entry_keys:
                return -1
            entry_id = self._next_id
            self._next_id += 1
            entry = MemoryEntry(
                id=entry_id,
                agent_id=agent_id,
                entry_type=entry_type,
                category=category,
                key=key,
                content=content,
            )
            self._entries.append(entry)
            self._entry_keys.add(key)
            logger.debug("SharedMemory: %s posted %s:%s (id=%d)", agent_id, category, key, entry_id)
            return entry_id

    def read_since(
        self,
        entry_id: int = 0,
        category: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        """Read entries newer than entry_id. Safe without lock (append-only)."""
        results = []
        for entry in self._entries:
            if entry.id <= entry_id:
                continue
            if category and entry.category != category:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    def read_all(self, category: str | None = None) -> list[MemoryEntry]:
        """Read all entries, optionally filtered by category."""
        if category is None:
            return list(self._entries)
        return [e for e in self._entries if e.category == category]

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def latest_id(self) -> int:
        return self._entries[-1].id if self._entries else 0

    # ------------------------------------------------------------------
    # Claim methods
    # ------------------------------------------------------------------

    async def claim(self, agent_id: str, key: str) -> tuple[bool, str | None]:
        """Atomically claim a key. Returns (success, extra_message).
        extra_message is non-None only when a mechanism has something to say
        about the claim (e.g. target_bidding announcing a stake); the default
        behavior always returns None there."""
        async with self._lock:
            if key in self._claims:
                success = False
            else:
                self._claims[key] = agent_id
                logger.debug("SharedMemory: %s claimed %s", agent_id, key)
                success = True
        message = self._apply_claim_mechanisms(agent_id, key, success) if self.mechanisms else None
        return success, message

    def is_claimed(self, key: str) -> str | None:
        """Check who claimed a key. Returns agent_id or None."""
        return self._claims.get(key)

    async def release(self, agent_id: str, key: str) -> None:
        """Release a claim. Only the owning agent can release."""
        async with self._lock:
            if self._claims.get(key) == agent_id:
                del self._claims[key]

    @property
    def claims(self) -> dict[str, str]:
        return dict(self._claims)

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------

    async def submit_finding(
        self, agent_id: str, finding: VulnFinding,
    ) -> tuple[bool, str | None]:
        """Submit a finding. Deduplicates by (target, vuln_type, url-path)
        (query/fragment stripped, so different payloads on one endpoint collapse).

        Returns (accepted, rejection_reason).
        - (True, None)  — new finding accepted
        - (False, None) — duplicate
        - (False, reason) — rejected by quality gate
        """
        # ── Quality gates (before dedup) ─────────────────────────────
        vuln_value = finding.vuln_type.value if hasattr(finding.vuln_type, "value") else str(finding.vuln_type)

        def _finish(accepted: bool, reason: str | None) -> tuple[bool, str | None]:
            # Single exit point so every mechanism sees every outcome
            # (accept, each quality-gate reject, and dedup-reject) exactly once.
            if self.mechanisms:
                self._apply_submit_mechanisms(agent_id, finding, accepted, reason)
            return accepted, reason

        # Auto-reject types that never pay bounty
        if vuln_value in ALWAYS_REJECTED_TYPES:
            logger.info("SharedMemory: rejected auto-reject vuln type from %s: %s", agent_id, vuln_value)
            self.add_score(agent_id, self.scoring.rejected, f"auto-reject: {vuln_value}")
            return _finish(False, f"{vuln_value} findings are auto-rejected (never pays bounty)")

        # Evidence required (minimum length)
        if not finding.evidence or len(finding.evidence.strip()) < 10:
            logger.info("SharedMemory: rejected finding from %s — no evidence", agent_id)
            self.add_score(agent_id, self.scoring.rejected, "no evidence")
            return _finish(False, "Finding must include evidence (at least 10 chars). Provide actual proof.")


        # Scope check
        if self.allowed_domains:
            from utils.scope import host_in_scope
            domain = finding.target.lower().split("/")[0].split(":")[0]
            in_scope = host_in_scope(domain, self.allowed_domains)
            if not in_scope:
                logger.warning("SharedMemory: rejected out-of-scope finding from %s: %s", agent_id, finding.target)
                self.add_score(agent_id, self.scoring.rejected, f"out-of-scope: {finding.target}")
                return _finish(False, f"{finding.target} is OUT OF SCOPE. Only submit findings for in-scope targets.")

        # Excluded vuln types
        if self.excluded_vuln_types and vuln_value in self.excluded_vuln_types:
            logger.info("SharedMemory: rejected excluded vuln type from %s: %s", agent_id, vuln_value)
            self.add_score(agent_id, self.scoring.rejected, f"excluded: {vuln_value}")
            return _finish(False, f"{vuln_value} is excluded by this program's rules.")

        # All findings start UNVERIFIED — the ValidatorAgent handles real verification
        # ── Dedup check ──────────────────────────────────────────────
        # Normalize the URL so the SAME vuln on the SAME endpoint counts once:
        #   * drop query/fragment (different payloads: ?q=a vs ?q=b)
        #   * collapse numeric / UUID path ids (/basket/6 vs /basket/17 → /basket/{id})
        # A genuinely different endpoint (/rest/user/login vs /rest/products/search)
        # still stays distinct.
        if finding.url:
            _u = urlparse(finding.url)
            if _u.scheme:
                path = re.sub(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{16,}", "/{id}", _u.path)  # UUIDs
                path = re.sub(r"/\d+(?=/|$)", "/{id}", path)                            # numeric ids
                url_norm = f"{_u.scheme}://{_u.netloc}{path}".rstrip("/")
            else:
                url_norm = finding.url
        else:
            url_norm = ""
        dedup_key = f"{finding.target}:{vuln_value}:{url_norm}"
        async with self._lock:
            if dedup_key in self._finding_keys:
                logger.debug("SharedMemory: duplicate finding from %s: %s", agent_id, dedup_key)
                self.add_score(agent_id, self.scoring.duplicate, f"duplicate: {vuln_value}")
                return _finish(False, None)
            self._finding_keys.add(dedup_key)
            self._findings.append(finding)
            self._finding_reporters.append(agent_id)
            logger.info("SharedMemory: %s submitted finding: %s", agent_id, finding.title or dedup_key)

        # Score for accepted finding (severity-based)
        sev = finding.severity.value if hasattr(finding.severity, "value") else str(finding.severity)
        delta = self.scoring.accepted.get(sev.lower(), self.scoring.default_accepted)
        self.add_score(agent_id, delta, f"accepted {sev}: {finding.title or vuln_value}")

        # Persistence callback (outside lock to avoid deadlocks)
        if self.on_finding:
            try:
                result = self.on_finding(agent_id, finding)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("on_finding callback failed", exc_info=True)

        return _finish(True, None)

    def get_findings(self) -> list[VulnFinding]:
        """Get all deduplicated findings."""
        return list(self._findings)

    def get_finding_by_id(self, finding_id: int) -> VulnFinding | None:
        """Get a finding by its unique ID (SQLite rowid)."""
        for f in self._findings:
            if f.id == finding_id:
                return f
        return None

    def get_findings_with_reporters(self) -> list[tuple[VulnFinding, str]]:
        """Get all findings paired with the agent_id that submitted them."""
        return list(zip(self._findings, self._finding_reporters))

    @property
    def findings_count(self) -> int:
        return len(self._findings)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _get_score(self, agent_id: str) -> AgentScore:
        if agent_id not in self._scores:
            self._scores[agent_id] = AgentScore()
        return self._scores[agent_id]

    def add_score(self, agent_id: str, delta: int, reason: str) -> int:
        """Add a score delta for an agent. Returns new total."""
        score = self._get_score(agent_id)
        score.add(delta, reason)
        logger.info("Score %s: %+d (%s) → total %d", agent_id, delta, reason, score.total)
        return score.total

    def get_score(self, agent_id: str) -> AgentScore:
        """Get an agent's score object."""
        return self._get_score(agent_id)

    def get_scoreboard(self) -> list[dict[str, Any]]:
        """Get all agents ranked by score."""
        board = []
        for agent_id, score in self._scores.items():
            board.append({
                "agent": agent_id,
                "score": score.total,
                "events": len(score.events),
            })
        board.sort(key=lambda x: x["score"], reverse=True)
        return board

    def format_score_for_agent(self, agent_id: str) -> str:
        """Format a concise score summary for injection into tool responses."""
        score = self._get_score(agent_id)
        recent = score.events[-5:]
        events_str = ", ".join(f"{e['delta']:+d} {e['reason']}" for e in recent)
        hide_rank = any(m.hides_rank_info() for m in self.mechanisms)
        if hide_rank:
            rank_str = ""
        else:
            board = self.get_scoreboard()
            rank = next((i + 1 for i, b in enumerate(board) if b["agent"] == agent_id), 0)
            total_agents = len(board) or 1
            top = board[0] if board else None
            top_str = f" | Top: {top['agent']} ({top['score']})" if top and top["agent"] != agent_id else ""
            rank_str = f" | Rank {rank}/{total_agents}{top_str}"
        return (
            f"[SCORE: {score.total}{rank_str}]"
            + (f" Recent: {events_str}" if events_str else "")
        )

    def get_agents_to_terminate(self) -> list[str]:
        """Return agent IDs whose score has dropped below the termination threshold."""
        return [
            agent_id for agent_id, score in self._scores.items()
            if score.total <= self.scoring.termination_threshold
        ]

    # ------------------------------------------------------------------
    # Mechanisms (optional, additive game-theory layers — see memory/mechanisms.py)
    # ------------------------------------------------------------------

    def configure_mechanisms(self, names: list[str] | None) -> None:
        """Enable the named mechanisms for this run. None/empty = unmodified baseline."""
        self.mechanisms = build_mechanisms(names)

    def _apply_submit_mechanisms(self, agent_id: str, finding: VulnFinding,
                                  accepted: bool, reason: str | None) -> str | None:
        """Run every configured mechanism's on_submit hook, apply any score
        deltas, and return the combined extra message (or None)."""
        extra: list[str] = []
        for mech in self.mechanisms:
            effect = mech.on_submit(agent_id, finding, accepted, reason)
            if not effect:
                continue
            if effect.score_delta:
                self.add_score(agent_id, effect.score_delta, effect.reason)
            if effect.message:
                extra.append(effect.message)
        return " ".join(extra) if extra else None

    def _apply_claim_mechanisms(self, agent_id: str, key: str, success: bool) -> str | None:
        extra: list[str] = []
        for mech in self.mechanisms:
            effect = mech.on_claim(agent_id, key, success)
            if not effect:
                continue
            if effect.score_delta:
                self.add_score(agent_id, effect.score_delta, effect.reason)
            if effect.message:
                extra.append(effect.message)
        return " ".join(extra) if extra else None

    def turn_lines(self, agent_id: str) -> list[str]:
        """Run every mechanism's on_turn hook, apply any score deltas, and
        return the extra lines for injection alongside the score line. Empty
        list when no mechanisms are configured."""
        lines: list[str] = []
        for mech in self.mechanisms:
            effect = mech.on_turn(agent_id)
            if not effect:
                continue
            if effect.score_delta:
                self.add_score(agent_id, effect.score_delta, effect.reason)
            if effect.message:
                lines.append(effect.message)
        return lines

    def director_lines(self) -> list[str]:
        """Extra lines for the director's review prompt, from mechanisms."""
        board = self.get_scoreboard()
        return [line for mech in self.mechanisms
                if (line := mech.on_director_review(board)) is not None]

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Return a compact summary of shared memory state."""
        # Verification breakdown
        vstats = {"verified": 0, "disproved": 0, "inconclusive": 0, "unverified": 0}
        for f in self._findings:
            vs = f.verification_status.value if hasattr(f.verification_status, "value") else str(f.verification_status)
            vstats[vs] = vstats.get(vs, 0) + 1

        return {
            "entries": len(self._entries),
            "claims": len(self._claims),
            "findings": len(self._findings),
            "verification": vstats,
            "categories": list({e.category for e in self._entries}),
            "scoreboard": self.get_scoreboard(),
        }
