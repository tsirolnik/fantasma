"""Cost tracking for AI bug bounty sessions."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


# Pricing per 1M tokens (USD) for supported providers/models.
LLM_PRICING: dict[str, dict[str, tuple[float, float]]] = {
    "anthropic": {
        "claude-sonnet-4-20250514": (3.00, 15.00),
        "claude-opus-4-20250514": (15.00, 75.00),
        "claude-haiku-3": (0.25, 1.25),
        "default": (3.00, 15.00),
    },
    "openai": {
        "gpt-4o": (2.50, 10.00),
        "gpt-4o-mini": (0.15, 0.60),
        "default": (2.50, 10.00),
    },
}


@dataclass
class CostEntry:
    """A single recorded cost event."""

    timestamp: str
    category: str
    amount: float
    target: str
    detail: str


@dataclass
class CostTracker:
    """Tracks per-session costs across multiple categories.

    Categories:
        llm_api          - LLM inference costs (token-based pricing)
        infrastructure   - compute / hosting / VPS hours
        time_effort      - human or agent time valued at an hourly rate
    """

    entries: list[CostEntry] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    #  High-level helpers
    # ------------------------------------------------------------------ #

    def add_llm_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        provider: str = "anthropic",
        model: str = "default",
    ) -> float:
        """Calculate and record the cost for an LLM API call.

        Returns the computed cost so callers can inspect it immediately.
        """
        provider_pricing = LLM_PRICING.get(provider, LLM_PRICING["anthropic"])
        input_rate, output_rate = provider_pricing.get(
            model, provider_pricing["default"]
        )

        cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        detail = (
            f"{provider}/{model} "
            f"in={input_tokens} out={output_tokens}"
        )
        self.add_cost("llm_api", cost, detail=detail)
        return cost

    def add_infra_cost(
        self,
        hours: float,
        rate_per_hour: float,
        target: str = "",
    ) -> float:
        """Record infrastructure cost (e.g. VPS, cloud compute)."""
        cost = hours * rate_per_hour
        detail = f"{hours:.2f}h @ ${rate_per_hour:.2f}/h"
        self.add_cost("infrastructure", cost, target=target, detail=detail)
        return cost

    def add_effort_cost(
        self,
        minutes: float,
        hourly_rate: float,
        target: str = "",
    ) -> float:
        """Record time-effort cost valued at *hourly_rate*."""
        cost = (minutes / 60.0) * hourly_rate
        detail = f"{minutes:.1f}min @ ${hourly_rate:.2f}/h"
        self.add_cost("time_effort", cost, target=target, detail=detail)
        return cost

    # ------------------------------------------------------------------ #
    #  Generic cost addition
    # ------------------------------------------------------------------ #

    def add_cost(
        self,
        category: str,
        amount: float,
        target: str = "",
        detail: str = "",
    ) -> None:
        """Append an arbitrary cost entry."""
        entry = CostEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            category=category,
            amount=amount,
            target=target,
            detail=detail,
        )
        self.entries.append(entry)

    def add_baseline(self, amount: float) -> None:
        """Add a cost baseline from a previous session (e.g. checkpoint resume).

        This ensures budget enforcement accounts for costs already spent.
        """
        self.add_cost("baseline", amount, detail="restored from checkpoint")

    # ------------------------------------------------------------------ #
    #  Queries
    # ------------------------------------------------------------------ #

    def get_total(self) -> float:
        """Return the sum of all recorded costs."""
        return sum(e.amount for e in self.entries)

    def get_breakdown(self) -> dict[str, float]:
        """Return a ``{category: total}`` mapping."""
        breakdown: dict[str, float] = {}
        for entry in self.entries:
            breakdown[entry.category] = breakdown.get(entry.category, 0.0) + entry.amount
        return breakdown

    def get_per_target_costs(self) -> dict[str, float]:
        """Return a ``{target_domain: total}`` mapping.

        Entries with an empty *target* are grouped under ``"_unassigned"``.
        """
        per_target: dict[str, float] = {}
        for entry in self.entries:
            key = entry.target if entry.target else "_unassigned"
            per_target[key] = per_target.get(key, 0.0) + entry.amount
        return per_target

    def check_budget(self, budget: float) -> bool:
        """Return ``True`` if total spend is within the given *budget*."""
        return self.get_total() <= budget

    # ------------------------------------------------------------------ #
    #  Formatting
    # ------------------------------------------------------------------ #

    def summary(self) -> str:
        """Return a human-readable cost summary."""
        total = self.get_total()
        breakdown = self.get_breakdown()
        lines = [
            "=== Cost Summary ===",
            f"Total: ${total:.4f}",
            "",
            "By category:",
        ]
        for cat, amount in sorted(breakdown.items()):
            lines.append(f"  {cat}: ${amount:.4f}")

        per_target = self.get_per_target_costs()
        if per_target:
            lines.append("")
            lines.append("By target:")
            for tgt, amount in sorted(per_target.items()):
                lines.append(f"  {tgt}: ${amount:.4f}")

        lines.append(f"\nEntries recorded: {len(self.entries)}")
        return "\n".join(lines)
