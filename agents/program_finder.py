"""Program finder agent — discovers and ranks bug bounty programs by ROI."""

import asyncio
import logging
from datetime import datetime, timezone

from models.program import BountyProgram
from platforms.hackerone import HackerOne
from platforms.bugcrowd import Bugcrowd
from platforms.intigriti import Intigriti
from platforms.yeswehack import YesWeHack
from cost.roi_model import ROIModel
from cost.tracker import CostTracker

logger = logging.getLogger(__name__)

# Programs scanned within this window get penalized (encourages rotation)
RECENCY_WINDOW_HOURS = 72


class ProgramFinder:
    """Fetches programs from all platforms and ranks by expected ROI."""

    def __init__(self, cost_tracker: CostTracker, min_bounty: float = 50.0,
                 max_response_days: float = 30.0, persistent_store=None):
        self.cost_tracker = cost_tracker
        self.min_bounty = min_bounty
        self.max_response_days = max_response_days
        self.roi_model = ROIModel()
        self.persistent_store = persistent_store
        self.platforms = [
            HackerOne(),
            Bugcrowd(),
            Intigriti(),
            YesWeHack(),
        ]

    def _get_recency_penalties(self) -> dict[str, float]:
        """Compute recency penalties for programs based on recent scan history.

        Returns a dict of program_url → penalty (0.0 = fresh, 1.0 = just scanned).
        Programs scanned within RECENCY_WINDOW_HOURS get a penalty that decays
        linearly from 1.0 (just now) to 0.0 (at the edge of the window).
        """
        if not self.persistent_store:
            return {}

        penalties: dict[str, float] = {}
        try:
            programs = self.persistent_store.get_programs()
            now = datetime.now(timezone.utc)
            for p in programs:
                last_scanned = p.get("last_scanned")
                if not last_scanned:
                    continue
                try:
                    last_dt = datetime.fromisoformat(last_scanned)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    hours_ago = (now - last_dt).total_seconds() / 3600
                    if hours_ago < RECENCY_WINDOW_HOURS:
                        # Linear decay: just scanned = 1.0, at window edge = 0.0
                        penalty = 1.0 - (hours_ago / RECENCY_WINDOW_HOURS)
                        url = p.get("url", "")
                        if url:
                            penalties[url] = penalty
                except (ValueError, TypeError):
                    continue
        except Exception:
            logger.debug("Could not compute recency penalties", exc_info=True)
        return penalties

    async def discover(self, top_n: int = 10) -> list[BountyProgram]:
        """Discover and rank top-N programs by ROI."""
        logger.info("Fetching programs from all platforms...")

        # Fetch from all platforms concurrently
        tasks = [p.fetch_programs() for p in self.platforms]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_programs = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Failed to fetch from {self.platforms[i].platform_name}: {result}")
                continue
            logger.info(f"Fetched {len(result)} programs from {self.platforms[i].platform_name}")
            all_programs.extend(result)

        # Filter — require at least 1 scannable domain
        programs = [
            p for p in all_programs
            if p.active
            and p.all_domains
            and p.max_bounty >= self.min_bounty
            and p.response_time_days <= self.max_response_days
        ]
        logger.info(f"After filtering: {len(programs)} programs with domains (from {len(all_programs)} total)")

        # Compute recency penalties from scan history
        recency = self._get_recency_penalties()

        # Score each program
        for program in programs:
            penalty = recency.get(program.url, 0.0)
            self.roi_model.score_program(program, self.cost_tracker, recency_penalty=penalty)

        # Sort by ROI score descending
        programs.sort(key=lambda p: p.roi_score, reverse=True)

        top = programs[:top_n]
        for i, p in enumerate(top, 1):
            summary = self.roi_model.format_roi_summary(p)
            penalty = recency.get(p.url, 0.0)
            recency_tag = f" [recency={penalty:.1f}]" if penalty > 0 else ""
            logger.info(f"#{i}: {p.name} ({p.platform}) - {summary}{recency_tag}")

        return top
