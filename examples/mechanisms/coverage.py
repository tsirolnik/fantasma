"""A small, stateful mechanism; no registry modification required."""
from memory.mechanisms import Mechanism, MechanismEffect


class CoverageBonus(Mechanism):
    """Reward each worker's first accepted finding on each target."""

    name = "coverage_bonus"

    def __init__(self, bonus: int = 7):
        if type(bonus) is not int or bonus < 0:
            raise ValueError("bonus must be a nonnegative integer")
        self.bonus = bonus
        self.seen: set[tuple[str, str]] = set()

    def on_submit(self, agent_id, finding, accepted, reason):
        key = (agent_id, finding.target)
        if not accepted or key in self.seen:
            return MechanismEffect()
        self.seen.add(key)
        return MechanismEffect(
            score_delta=self.bonus,
            reason=f"coverage_bonus: first accepted finding on {finding.target}",
            message=f"You earned {self.bonus} extra points for covering a new target.",
        )
