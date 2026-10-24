"""Learning memory — adapts scanning strategy based on historical outcomes."""

import logging
from dataclasses import dataclass

from memory.store import PersistentMemory

logger = logging.getLogger(__name__)

# Severity-based prior ranking for cold start (no submission/acceptance data yet).
# Scores reflect typical bounty value / exploitability of each vuln class.
VULN_TYPE_PRIOR: dict[str, float] = {
    "memory_corruption": 10.0,   # RCE-class
    "command_injection": 10.0,   # RCE-class
    "sql_injection": 9.0,        # data exfil / auth bypass, frequently critical
    "insecure_deserialization": 9.0,  # RCE-class
    "secret_exposure": 8.0,      # credential leaks
    "broken_authentication": 8.0,
    "known_cve": 7.0,            # exploitable CVEs
    "nosql_injection": 7.0,
    "xxe": 7.0,
    "ssrf": 7.0,
    "file_inclusion": 7.0,       # LFI/RFI/path traversal
    "broken_access_control": 7.0,  # IDOR/BOLA/BFLA
    "mass_assignment": 6.0,
    "subdomain_takeover": 5.0,
    "sensitive_path": 5.0,       # .env, .git exposure
    "business_logic": 5.0,
    "xss": 4.0,
    "info_disclosure": 2.0,
    "csrf": 2.0,
    "open_redirect": 1.5,        # standalone rarely pays; valuable only as chain component
    "misconfiguration": 1.5,
    "missing_security_headers": 0.5,
    "rate_limiting": 0.5,
    "cors_misconfiguration": 0.0,  # almost never a real vuln — public APIs don't need CORS
}


@dataclass
class VulnTypeStats:
    """Aggregated historical stats for a vulnerability type."""
    vuln_type: str
    total_found: int = 0
    total_submitted: int = 0
    total_accepted: int = 0
    total_bounty_earned: float = 0.0
    avg_confidence: float = 0.0
    acceptance_rate: float = 0.0
    avg_bounty: float = 0.0
    roi_score: float = 0.0


class LearningMemory:
    """Learns from past scan outcomes to improve future decisions.

    Tracks which vuln types, programs, and targets have produced the
    best ROI historically and adjusts scoring/prioritization.
    """

    def __init__(self, store: PersistentMemory):
        self.store = store

    def get_vuln_type_stats(self) -> dict[str, VulnTypeStats]:
        """Aggregate historical performance by vulnerability type."""
        findings = self.store.get_past_findings(limit=10000)
        stats: dict[str, VulnTypeStats] = {}

        for f in findings:
            vt = f["vuln_type"]
            if vt not in stats:
                stats[vt] = VulnTypeStats(vuln_type=vt)
            s = stats[vt]
            s.total_found += 1
            s.avg_confidence += f.get("confidence", 0.0)
            if f.get("was_submitted"):
                s.total_submitted += 1
            if f.get("was_accepted"):
                s.total_accepted += 1
                s.total_bounty_earned += f.get("actual_bounty", 0.0)

        for s in stats.values():
            if s.total_found > 0:
                s.avg_confidence /= s.total_found
            if s.total_submitted > 0:
                s.acceptance_rate = s.total_accepted / s.total_submitted
            if s.total_accepted > 0:
                s.avg_bounty = s.total_bounty_earned / s.total_accepted

        return stats

    def get_best_vuln_types(self, top_n: int = 5) -> list[str]:
        """Return the vuln types ranked by historical ROI.

        When no findings have been submitted yet (cold start), falls back to
        severity-based prior ranking instead of the all-zero ROI formula.
        """
        stats = self.get_vuln_type_stats()

        # Check if we have any real submission data
        has_submissions = any(s.total_submitted > 0 for s in stats.values())

        if has_submissions:
            # Real data: score by acceptance_rate * avg_bounty * reliability
            for s in stats.values():
                s.roi_score = s.acceptance_rate * s.avg_bounty * min(s.total_found, 10) / 10
        else:
            # Cold start: use severity-based priors, boosted by detection count
            for s in stats.values():
                prior = VULN_TYPE_PRIOR.get(s.vuln_type, 1.0)
                detectability_boost = min(s.total_found, 10) / 10  # 0.0–1.0
                s.roi_score = prior * (0.5 + 0.5 * detectability_boost)

            # Also include prior-only types not yet detected
            for vt, prior_score in VULN_TYPE_PRIOR.items():
                if vt not in stats:
                    stats[vt] = VulnTypeStats(vuln_type=vt, roi_score=prior_score * 0.5)

        ranked = sorted(stats.values(), key=lambda s: s.roi_score, reverse=True)
        return [s.vuln_type for s in ranked[:top_n]]

    def get_target_history(self, target: str) -> dict:
        """Get historical scan data for a target."""
        scans = self.store.get_past_scans(target=target)
        findings = self.store.get_past_findings(target=target)

        total_bounty = sum(f.get("actual_bounty", 0.0) for f in findings if f.get("was_accepted"))
        total_cost = sum(s.get("cost", 0.0) for s in scans)

        return {
            "target": target,
            "scan_count": len(scans),
            "total_findings": len(findings),
            "accepted_findings": sum(1 for f in findings if f.get("was_accepted")),
            "total_bounty_earned": total_bounty,
            "total_cost": total_cost,
            "net_roi": total_bounty - total_cost,
            "last_scanned": scans[0]["started_at"] if scans else None,
            "finding_types": list({f["vuln_type"] for f in findings}),
        }

    def should_scan_target(self, target: str, hours_cooldown: int = 24) -> tuple[bool, str]:
        """Decide whether to scan a target based on history.

        Returns (should_scan, reason).
        """
        # Skip the recent-scan cooldown — allows re-testing same target.
        # The learning model still tracks history for ROI adaptation.

        history = self.get_target_history(target)
        if history["scan_count"] >= 3 and history["total_findings"] == 0:
            return False, "No findings in 3+ previous scans"

        return True, "OK"

    def adjust_roi_model(self, roi_model) -> None:
        """Adjust ROI model parameters based on historical data.

        Overrides default vuln class metrics with observed acceptance
        rates and bounty amounts when we have enough data.
        """
        from cost.roi_model import VULN_CLASS_METRICS, VulnClassMetrics
        from models.finding import VulnType

        stats = self.get_vuln_type_stats()
        min_samples = 5  # Need at least this many findings to adjust

        for vt_str, s in stats.items():
            if s.total_submitted < min_samples:
                continue

            try:
                vt = VulnType(vt_str)
            except ValueError:
                continue

            if vt in VULN_CLASS_METRICS:
                metrics = VULN_CLASS_METRICS[vt]
                # Blend: 70% observed, 30% prior (to avoid overfitting)
                if s.acceptance_rate > 0:
                    metrics.acceptance_rate = 0.7 * s.acceptance_rate + 0.3 * metrics.acceptance_rate
                if s.avg_bounty > 0:
                    metrics.avg_bounty = 0.7 * s.avg_bounty + 0.3 * metrics.avg_bounty
                    metrics.median_bounty = 0.7 * s.avg_bounty + 0.3 * metrics.median_bounty

                logger.info(
                    f"Adjusted {vt.value}: acceptance_rate={metrics.acceptance_rate:.2f}, "
                    f"avg_bounty=${metrics.avg_bounty:.0f}"
                )

    def record_outcome(self, finding_id: int, accepted: bool, bounty: float = 0.0) -> None:
        """Record the outcome of a submitted finding for future learning."""
        self.store.mark_finding_submitted(finding_id, accepted=accepted, actual_bounty=bounty)
        logger.info(
            f"Recorded outcome for finding {finding_id}: "
            f"accepted={accepted}, bounty=${bounty:.2f}"
        )

    def summary(self) -> str:
        """Return a human-readable learning summary."""
        stats = self.get_vuln_type_stats()
        if not stats:
            return "No historical data yet."

        has_submissions = any(s.total_submitted > 0 for s in stats.values())

        if has_submissions:
            lines = ["=== Learning Memory (real data) ==="]
        else:
            lines = ["=== Learning Memory (severity-based priors — no submissions yet) ==="]

        # Recompute scores to match get_best_vuln_types() logic
        if has_submissions:
            for s in stats.values():
                s.roi_score = s.acceptance_rate * s.avg_bounty * min(s.total_found, 10) / 10
        else:
            for s in stats.values():
                prior = VULN_TYPE_PRIOR.get(s.vuln_type, 1.0)
                detectability_boost = min(s.total_found, 10) / 10
                s.roi_score = prior * (0.5 + 0.5 * detectability_boost)

        for vt, s in sorted(stats.items(), key=lambda x: x[1].roi_score, reverse=True):
            lines.append(
                f"  {vt}: {s.total_found} found, {s.total_accepted}/{s.total_submitted} accepted, "
                f"${s.total_bounty_earned:.0f} earned, ROI score: {s.roi_score:.2f}"
            )
        return "\n".join(lines)
