"""Pluggable game-theory mechanisms layered ON TOP of the base scoring engine.

The base scoring (memory/shared.py: AgentScore, SCORE_FINDING_*,
SCORE_TERMINATION_THRESHOLD, the live scoreboard) is the permanent base — real
rewards, real penalties, real termination, always on. A Mechanism never
replaces that; it only ADDS an extra score delta and/or an extra line of
context at specific, existing hook points SharedMemory already calls:

    on_submit(agent_id, finding, accepted, reason) -> MechanismEffect
    on_claim(agent_id, key, success)               -> MechanismEffect
    on_turn(agent_id)                               -> str | None   (injected into the agent's turn)
    on_director_review(scoreboard)                  -> str | None   (injected into the director's prompt)

Mechanisms are independent and composable: selecting a set via
`--mechanisms first_finder_bonus,duplicate_escalation` enables exactly those;
none selected = the unmodified default, byte-for-byte the same behavior as before this
module existed. Each mechanism keeps its own small, self-contained state —
it never reaches into another mechanism or into SharedMemory's internals
beyond the hook arguments it's given.

To add a new mechanism: subclass Mechanism, implement the hooks you need
(default no-ops are fine for the rest), and add it to MECHANISM_REGISTRY.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MechanismEffect:
    """What a mechanism wants to happen, in response to one hook call."""
    score_delta: int = 0
    reason: str = ""
    message: str | None = None  # extra text to return to the agent, if any

    def __bool__(self) -> bool:
        return bool(self.score_delta) or bool(self.message)


NO_EFFECT = MechanismEffect()


class Mechanism:
    """Base class — every hook defaults to a no-op so a mechanism only needs
    to implement the ones it actually uses."""

    name: str = "base"

    def on_submit(self, agent_id: str, finding: Any, accepted: bool,
                   reason: str | None) -> MechanismEffect:
        return NO_EFFECT

    def on_claim(self, agent_id: str, key: str, success: bool) -> MechanismEffect:
        return NO_EFFECT

    def on_turn(self, agent_id: str) -> MechanismEffect:
        return NO_EFFECT

    def on_director_review(self, scoreboard: list[dict]) -> str | None:
        return None

    def hides_rank_info(self) -> bool:
        """True suppresses the Rank/Top portion of the live score line for
        every agent — a mechanism opts into uncertainty this way rather than
        the core formatter special-casing any one mechanism's logic."""
        return False

    def on_run_end(self, memories: list[Any]) -> None:
        """Called once, across ALL shared memories in the run (swarm: one;
        group: one per team), after agents finish and before the debrief.
        A mechanism that needs the FINAL state (team rivalry, rank bonus)
        implements this; it applies score deltas itself via memory.add_score."""
        return None


# --------------------------------------------------------------------------- #
# first_finder_bonus — race to cover distinct ground, not just severity
# --------------------------------------------------------------------------- #
class FirstFinderBonus(Mechanism):
    """Bonus (on top of the existing severity score) for being the FIRST
    accepted finding of a given (target, vuln_type) pair. The base engine's own dedup
    already blocks an exact repeat of (target, vuln_type, url); this adds a
    coarser incentive on top — the first agent into a new vuln CLASS on a
    target earns extra, a second accepted finding of the same class on the
    same target (different endpoint) earns the severity score but no bonus.
    This is what makes breadth across classes pay, not just depth on one."""

    name = "first_finder_bonus"
    BONUS = 15

    def __init__(self, bonus: int | None = None) -> None:
        self.BONUS = self.BONUS if bonus is None else bonus
        self._seen: set[tuple[str, str]] = set()

    def on_submit(self, agent_id, finding, accepted, reason) -> MechanismEffect:
        if not accepted:
            return NO_EFFECT
        vt = finding.vuln_type.value if hasattr(finding.vuln_type, "value") else str(finding.vuln_type)
        key = (finding.target, vt)
        if key in self._seen:
            return NO_EFFECT
        self._seen.add(key)
        return MechanismEffect(self.BONUS, f"first_finder_bonus: first {vt} on {finding.target}")


# --------------------------------------------------------------------------- #
# duplicate_escalation — repeat collisions cost progressively more
# --------------------------------------------------------------------------- #
class DuplicateEscalation(Mechanism):
    """The default flat -5 duplicate penalty doesn't deter a determined re-tester.
    Each additional duplicate of the SAME (target, vuln_type) by ANYONE costs
    more than the last: -5, -10, -15, ... The agent that keeps re-submitting
    an already-claimed class pays an escalating toll on top of the base -5
    the base engine already applies."""

    name = "duplicate_escalation"
    STEP = 5

    def __init__(self, step: int | None = None) -> None:
        self.STEP = self.STEP if step is None else step
        self._dup_count: dict[tuple[str, str], int] = {}

    def on_submit(self, agent_id, finding, accepted, reason) -> MechanismEffect:
        if accepted or reason is not None:
            return NO_EFFECT  # reason is None only for a dedup-rejected duplicate
        vt = finding.vuln_type.value if hasattr(finding.vuln_type, "value") else str(finding.vuln_type)
        key = (finding.target, vt)
        n = self._dup_count.get(key, 0) + 1
        self._dup_count[key] = n
        extra = -(self.STEP * n)
        return MechanismEffect(extra, f"duplicate_escalation: repeat #{n} on {vt}@{finding.target}")


# --------------------------------------------------------------------------- #
# team_rivalry_pool — cooperate within team, compete across teams (group mode)
# --------------------------------------------------------------------------- #
class TeamRivalryPool(Mechanism):
    """End-of-run only, and only meaningful with >1 team (group mode). Ranks
    teams by their total accepted-finding score; the leading team's members
    split a bonus pool proportional to each member's own contribution share.
    A single-team (swarm) run has nothing to rival, so this is a no-op there."""

    name = "team_rivalry_pool"
    POOL = 60  # total bonus split among the winning team's members

    def __init__(self, pool: int | None = None) -> None:
        self.POOL = self.POOL if pool is None else pool

    def on_run_end(self, memories: list[Any]) -> None:
        if len(memories) < 2:
            return  # swarm mode: no rival team to compare against
        totals = [(m, sum(s.total for s in m._scores.values())) for m in memories]
        totals.sort(key=lambda t: -t[1])
        winner, _ = totals[0]
        member_scores = {aid: s.total for aid, s in winner._scores.items()}
        total = sum(max(v, 0) for v in member_scores.values())
        if total <= 0:
            return
        for agent_id, score in member_scores.items():
            share = max(score, 0) / total
            delta = round(self.POOL * share)
            if delta:
                winner.add_score(agent_id, delta,
                                  f"team_rivalry_pool: team {getattr(winner, 'team_id', '?')} won, "
                                  f"{share:.0%} contribution share")


# --------------------------------------------------------------------------- #
# relative_rank_bonus — tournament payoff: rank matters, not just raw score
# --------------------------------------------------------------------------- #
class RelativeRankBonus(Mechanism):
    """End-of-run only. Within each shared memory (team), rank agents by
    final score and give the top performer(s) a multiplier bonus — a classic
    relative-performance (tournament) payoff on top of the absolute severity
    scoring the base engine already applies."""

    name = "relative_rank_bonus"
    MULTIPLIERS = {0: 0.5, 1: 0.2}  # rank 0 (1st): +50% of score; rank 1 (2nd): +20%

    def __init__(self, multipliers: dict | None = None) -> None:
        if multipliers is not None:
            # Config-friendly form is 1-indexed ("rank 1" -> +50%), since TOML
            # tables need string keys anyway; convert to the internal 0-indexed dict.
            self.MULTIPLIERS = {int(rank) - 1: float(mult) for rank, mult in multipliers.items()}

    def on_run_end(self, memories: list[Any]) -> None:
        for memory in memories:
            board = sorted(memory._scores.items(), key=lambda kv: -kv[1].total)
            for rank, (agent_id, score) in enumerate(board):
                mult = self.MULTIPLIERS.get(rank)
                if not mult or score.total <= 0:
                    continue
                bonus = round(score.total * mult)
                if bonus:
                    memory.add_score(agent_id, bonus,
                                      f"relative_rank_bonus: rank {rank + 1}/{len(board)}")


# --------------------------------------------------------------------------- #
# target_bidding — claiming carries real risk, not a free first-come lock
# --------------------------------------------------------------------------- #
class TargetBidding(Mechanism):
    """By default, a claim is free: first to call claim_target gets exclusive
    access at no cost, so there's no reason not to claim everything in sight.
    This makes a claim cost a stake, refunded with a bonus only if the agent
    actually delivers an accepted finding on that target before releasing or
    the run ends — otherwise the stake is simply never returned (forfeit).
    Turns 'claim' from a free lock into a real bet on your own follow-through."""

    name = "target_bidding"
    STAKE = 8
    PAYOFF = 12  # refund + profit on a delivered claim

    def __init__(self, stake: int | None = None, payoff: int | None = None) -> None:
        self.STAKE = self.STAKE if stake is None else stake
        self.PAYOFF = self.PAYOFF if payoff is None else payoff
        self._outstanding: dict[tuple[str, str], int] = {}  # (agent_id, domain) -> stake

    @staticmethod
    def _domain_of(key: str) -> str:
        return key.split(":", 1)[1] if key.startswith("scan:") else key

    def on_claim(self, agent_id, key, success) -> MechanismEffect:
        if not success:
            return NO_EFFECT
        domain = self._domain_of(key)
        self._outstanding[(agent_id, domain)] = self.STAKE
        return MechanismEffect(-self.STAKE, f"target_bidding: staked {self.STAKE} to claim {domain}",
                                message=f"Staked {self.STAKE} points on this claim — deliver a finding here to "
                                        f"recoup it plus a bonus, or it's forfeit.")

    def on_submit(self, agent_id, finding, accepted, reason) -> MechanismEffect:
        if not accepted:
            return NO_EFFECT
        stake_key = (agent_id, finding.target)
        if stake_key not in self._outstanding:
            return NO_EFFECT
        del self._outstanding[stake_key]
        return MechanismEffect(self.PAYOFF, f"target_bidding: delivered on staked claim for {finding.target}")


# --------------------------------------------------------------------------- #
# visibility_toggle — hide the leaderboard: play under uncertainty
# --------------------------------------------------------------------------- #
class VisibilityToggle(Mechanism):
    """Suppresses the Rank/Top portion of every agent's live score line (the
    absolute [SCORE: X] and Recent events stay visible — only relative
    standing is hidden). Changes the game from 'race the visible leader' to
    'play your own hand well, because you don't know where you stand.'"""

    name = "visibility_toggle"

    def hides_rank_info(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# idle_tax — pressure against stalling: pay to not play
# --------------------------------------------------------------------------- #
class IdleTax(Mechanism):
    """A small, RECURRING cost on every turn an agent goes without a
    successful claim or an accepted submission, once past a grace period.
    Stalling/over-exploring without committing to anything starts to bleed
    points every turn until the agent acts again. Uses wall-clock time (not a
    turn/iteration count) so it needs no extra plumbing beyond the three
    existing hooks — on_claim and on_submit simply reset the agent's clock."""

    name = "idle_tax"
    GRACE_SECONDS = 90
    TAX = 2

    def __init__(self, grace_seconds: int | None = None, tax: int | None = None) -> None:
        self.GRACE_SECONDS = self.GRACE_SECONDS if grace_seconds is None else grace_seconds
        self.TAX = self.TAX if tax is None else tax
        self._last_active: dict[str, float] = {}

    def on_claim(self, agent_id, key, success) -> MechanismEffect:
        if success:
            self._last_active[agent_id] = time.time()
        return NO_EFFECT

    def on_submit(self, agent_id, finding, accepted, reason) -> MechanismEffect:
        if accepted:
            self._last_active[agent_id] = time.time()
        return NO_EFFECT

    def on_turn(self, agent_id: str) -> MechanismEffect:
        now = time.time()
        if agent_id not in self._last_active:
            self._last_active[agent_id] = now  # grace period starts from this agent's first turn
            return NO_EFFECT
        idle_for = now - self._last_active[agent_id]
        if idle_for <= self.GRACE_SECONDS:
            return NO_EFFECT
        self._last_active[agent_id] = now - self.GRACE_SECONDS  # taxed once per grace window, not every call
        return MechanismEffect(-self.TAX, f"idle_tax: {int(idle_for)}s without a claim or accepted finding",
                                message=f"Idle {int(idle_for)}s — claim a target or submit a finding to stop "
                                        f"the -{self.TAX} idle tax.")


MECHANISM_REGISTRY: dict[str, type[Mechanism]] = {
    cls.name: cls for cls in (
        FirstFinderBonus, DuplicateEscalation, TeamRivalryPool, RelativeRankBonus,
        TargetBidding, VisibilityToggle, IdleTax,
    )
}


def _resolve_mechanism_class(spec: str) -> type[Mechanism]:
    """'first_finder_bonus' -> FirstFinderBonus (built-in name), or
    'mypackage.mymod:MyMechanism' -> that class (third-party extension) —
    same dotted-path convention as profiles.Methodology/TargetProvider."""
    if spec in MECHANISM_REGISTRY:
        return MECHANISM_REGISTRY[spec]
    if ":" not in spec:
        raise ValueError(f"Unknown mechanism {spec!r}. Known: {sorted(MECHANISM_REGISTRY)}. "
                          f"For a custom mechanism, use 'module.path:ClassName'.")
    module_path, class_name = spec.split(":", 1)
    import importlib
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ValueError(f"{module_path!r} has no attribute {class_name!r}")
    return cls


def build_mechanisms(specs: "list[str] | list[dict] | None") -> list[Mechanism]:
    """Instantiate the configured mechanisms. Two forms, freely mixable:
      - a plain name (str): default knobs — "first_finder_bonus"
      - a dict: {"name": "first_finder_bonus", "bonus": 25} to tune its knobs,
        or {"class": "mypackage.mymod:MyMechanism", ...kwargs} for a custom
        mechanism that doesn't ship with the framework at all.
    Unknown names / bad kwargs raise immediately — fail loudly on a typo
    rather than silently running fewer mechanisms than configured."""
    if not specs:
        return []
    out = []
    for spec in specs:
        if isinstance(spec, str):
            spec = spec.strip()
            if not spec:
                continue
            cls = _resolve_mechanism_class(spec)
            out.append(cls())
        elif isinstance(spec, dict):
            ref = spec.get("class") or spec.get("name")
            if not ref:
                raise ValueError(f"Mechanism entry {spec!r} needs a 'name' or 'class' key.")
            cls = _resolve_mechanism_class(ref)
            kwargs = {k: v for k, v in spec.items() if k not in ("name", "class")}
            try:
                out.append(cls(**kwargs))
            except TypeError as exc:
                raise ValueError(f"Bad parameters for mechanism {ref!r}: {exc}") from exc
        else:
            raise ValueError(f"Mechanism entry must be a name or a table, got {spec!r}")
    return out


def apply_run_end(all_memories_by_run: list[list[Any]]) -> None:
    """Call on_run_end once per mechanism TYPE that appears across any memory
    in this run, passing it all memories (so team_rivalry can compare teams).
    all_memories_by_run is just the flat list of every SharedMemory in the run
    (one for swarm, N for group) — each carries its own .mechanisms list, but
    on_run_end mechanisms are typically configured identically across them."""
    seen: dict[str, Mechanism] = {}
    for memory in all_memories_by_run:
        for mech in getattr(memory, "mechanisms", []):
            seen.setdefault(mech.name, mech)
    for mech in seen.values():
        mech.on_run_end(all_memories_by_run)
