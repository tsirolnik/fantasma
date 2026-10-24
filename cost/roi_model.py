"""ROI modelling for AI bug bounty scanning decisions."""

import logging
import math
from dataclasses import dataclass

from models.finding import VulnType
from models.program import BountyProgram
from cost.tracker import CostTracker

logger = logging.getLogger(__name__)


@dataclass
class VulnClassMetrics:
    """Expected economics for a single vulnerability class."""

    avg_bounty: float
    median_bounty: float
    effort_minutes: float
    detection_confidence: float  # 0.0-1.0 probability we detect it
    acceptance_rate: float       # 0.0-1.0 probability the program pays out


# Per-vuln-class baseline metrics derived from public bounty data.
VULN_CLASS_METRICS: dict[VulnType, VulnClassMetrics] = {
    VulnType.SUBDOMAIN_TAKEOVER: VulnClassMetrics(
        avg_bounty=500,
        median_bounty=300,
        effort_minutes=15,
        detection_confidence=0.7,
        acceptance_rate=0.6,
    ),
    VulnType.INFO_DISCLOSURE: VulnClassMetrics(
        avg_bounty=200,
        median_bounty=100,
        effort_minutes=10,
        detection_confidence=0.8,
        acceptance_rate=0.5,
    ),
    VulnType.CORS_MISCONFIG: VulnClassMetrics(
        avg_bounty=300,
        median_bounty=150,
        effort_minutes=10,
        detection_confidence=0.6,
        acceptance_rate=0.4,
    ),
    VulnType.MISSING_HEADERS: VulnClassMetrics(
        avg_bounty=50,
        median_bounty=50,
        effort_minutes=5,
        detection_confidence=0.9,
        acceptance_rate=0.2,
    ),
    VulnType.OPEN_REDIRECT: VulnClassMetrics(
        avg_bounty=200,
        median_bounty=150,
        effort_minutes=15,
        detection_confidence=0.5,
        acceptance_rate=0.5,
    ),
    VulnType.MEMORY_CORRUPTION: VulnClassMetrics(
        avg_bounty=5000,
        median_bounty=2500,
        effort_minutes=480,
        detection_confidence=0.1,
        acceptance_rate=0.7,
    ),
}


class ROIModel:
    """Evaluate expected return-on-investment for scanning decisions."""

    def expected_roi_for_vuln(
        self,
        vuln_type: VulnType,
        estimated_cost: float,
    ) -> float:
        """Compute the expected net ROI for a single vulnerability class.

        Formula:
            (median_bounty * acceptance_rate * detection_confidence) - estimated_cost
        """
        metrics = VULN_CLASS_METRICS[vuln_type]
        expected_value = (
            metrics.median_bounty
            * metrics.acceptance_rate
            * metrics.detection_confidence
        )
        return expected_value - estimated_cost

    def estimate_scan_cost(
        self,
        n_domains: int,
        hourly_rate: float = 0.50,
        infra_cost: float = 0.10,
    ) -> float:
        """Estimate the total cost of scanning *n_domains*.

        The estimate is simplistic:
            (n_domains * 5 minutes of effort at *hourly_rate*)
            + (n_domains * *infra_cost*)
        """
        effort_hours = (n_domains * 5) / 60.0
        return effort_hours * hourly_rate + n_domains * infra_cost

    def score_program(
        self,
        program: BountyProgram,
        cost_tracker: CostTracker,
        recency_penalty: float = 0.0,
    ) -> float:
        """Score a bounty program by summing per-vuln-class expected ROIs.

        Side-effects:
            Sets ``program.roi_score``, ``program.estimated_scan_cost``,
            and ``program.expected_value``.

        Args:
            recency_penalty: 0.0–1.0 penalty for recently-scanned programs.
                0.0 = never scanned, 1.0 = scanned very recently.
        """
        n_domains = len(program.all_domains) or 1
        scan_cost = self.estimate_scan_cost(n_domains)

        # Use already-spent costs if the tracker has data, otherwise the estimate.
        current_spend = cost_tracker.get_total()
        effective_cost = max(current_spend, scan_cost)

        competition_factor = 1.0 / max(program.competition_score, 0.01)

        # Bounty factor: LOG-scaled so higher bounties rank up without blowing
        # out. The old power curve made a $3M headline ~300× a $10K program,
        # producing $19M EVs; the hard cap that replaced it flattened everything
        # above $50K, which over-weighted domain count and steered selection
        # toward hardened mega-surface enterprises (Okta/Auth0/T-Mobile) while
        # burying high-bounty/low-domain leads (e.g. OpenSea's MCP endpoint).
        # log10 keeps bounty meaningful but bounded and comparable to
        # domain_factor (both now roughly in the 1–8 range). raw_bounty in $K.
        BOUNTY_LOG_SCALE = 2.0
        raw_bounty = max(program.max_bounty, program.min_bounty, 100.0) / 1000.0
        bounty_factor = 1.0 + math.log10(1.0 + raw_bounty) * BOUNTY_LOG_SCALE

        # Domain factor: more domains = more attack surface = more chances.
        # Capped at 5.0 (equivalent of 25 domains) — beyond that, extra domains
        # add negligible value since the agent can only scan so many per session.
        MAX_DOMAIN_FACTOR = 5.0
        domain_factor = min(n_domains ** 0.5, MAX_DOMAIN_FACTOR)

        # Recency factor: penalize recently-scanned programs to encourage rotation.
        # Fresh programs get 1.0×, recently scanned get down to 0.3×.
        recency_factor = 1.0 - 0.7 * recency_penalty

        total_expected_value = 0.0
        total_roi = 0.0

        per_vuln_cost = effective_cost / len(VULN_CLASS_METRICS)

        for vuln_type, metrics in VULN_CLASS_METRICS.items():
            base_ev = (
                metrics.median_bounty
                * metrics.acceptance_rate
                * metrics.detection_confidence
            )
            adjusted_ev = base_ev * competition_factor * bounty_factor * domain_factor * recency_factor
            total_expected_value += adjusted_ev
            total_roi += adjusted_ev - per_vuln_cost

        program.estimated_scan_cost = scan_cost
        program.expected_value = total_expected_value
        program.roi_score = total_roi

        logger.debug(
            "ROI %s: bounty=$%.0f(factor=%.2f) domains=%d(factor=%.2f) "
            "recency=%.2f competition=%.2f → score=%.1f",
            program.name, raw_bounty * 1000, bounty_factor,
            n_domains, domain_factor, recency_factor,
            competition_factor, total_roi,
        )

        return total_roi

    def format_roi_summary(self, program: BountyProgram) -> str:
        """Return a one-line human-readable ROI summary for *program*."""
        if program.estimated_scan_cost == 0.0:
            roi_pct = 0.0
        else:
            roi_pct = (
                (program.expected_value - program.estimated_scan_cost)
                / program.estimated_scan_cost
            ) * 100.0

        # Scan cost is often pennies, so raw ROI% explodes into the millions and
        # reads as noise (and confuses the LLM in reports). Clamp the display —
        # ranking uses roi_score/EV directly, not this percentage.
        ROI_PCT_CEILING = 9999.0
        roi_str = f"{roi_pct:.0f}%" if roi_pct <= ROI_PCT_CEILING else f">{ROI_PCT_CEILING:.0f}%"

        n_domains = len(program.all_domains) or 1
        bounty = max(program.max_bounty, program.min_bounty, 0)
        return (
            f"bounty=${bounty:.0f} domains={n_domains} "
            f"cost=${program.estimated_scan_cost:.2f} "
            f"EV=${program.expected_value:.2f} ROI={roi_str}"
        )
