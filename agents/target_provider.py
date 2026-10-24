"""TargetProvider — pluggable 'what are we authorized to test' strategies.

A TargetProvider decides which `BountyProgram` objects (the framework's
generic authorized-scope unit — despite the name, it's just
name/url/domains/out-of-scope rules; nothing requires an actual bounty) the
run should test. Swapping providers lets the same orchestration engine point
at a bug-bounty platform, a fixed internal asset list, a CTF target, or any
other source of authorized scope — the pipeline/swarm code only ever calls
`get_targets()` and never knows which concrete source it's talking to.

This also de-duplicates logic that previously existed as two byte-identical
copies inline in agents/pipeline.py and agents/swarm_pipeline.py.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from models.program import BountyProgram

logger = logging.getLogger(__name__)


class TargetProvider(Protocol):
    """Resolves the in-scope targets for a run.

    Implementations are given the running pipeline instance (AgentPipeline or
    SwarmPipeline) so they can reuse its discovery worker, director, session,
    persistence, and graph store rather than duplicating that wiring — but a
    provider must not assume anything about pipeline MODE (swarm/group/lab);
    all of those share this interface.
    """

    async def get_targets(self, pipeline: Any, top_n: int, min_bounty: float) -> list[BountyProgram] | None:
        """Return the selected targets, or None to abort the run (the
        reason is already logged by the implementation before returning)."""
        ...


class DirectTargetProvider:
    """Domain-agnostic: scope is exactly what the caller passed via
    --targets. No platform, no ROI ranking, no bounty concept at all — this
    is the provider any non-bug-bounty profile (red team, CTF, code audit)
    uses, and it already existed as the pipelines' 'skip discovery' path."""

    def __init__(self, **config: Any) -> None:
        # Nothing built-in reads this today — stored for a subclass (built-in
        # or a third party's 'module:ClassName') that wants to, same generic
        # config-passthrough every pluggable class gets, not just mechanisms.
        self.config = config

    async def get_targets(self, pipeline: Any, top_n: int, min_bounty: float) -> list[BountyProgram] | None:
        direct_targets = getattr(pipeline, "_direct_targets", None)
        if not direct_targets:
            return None
        selected = [pipeline._build_program_from_targets(
            direct_targets, getattr(pipeline, "_direct_program_url", None),
        )]
        logger.info("=== Direct targets: %d domains (skipping discovery) ===", len(direct_targets))
        pipeline.session.programs = selected
        for p in selected:
            pipeline.persistent.save_program(p.model_dump())
            if pipeline.graph:
                await pipeline.graph.store_program(p)
        return selected


class BugBountyTargetProvider:
    """Discovers programs from HackerOne/Bugcrowd/Intigriti/YesWeHack via the
    pipeline's DiscoverWorker, ranks them with the Director, and persists the
    selection. This is the bug-bounty profile's provider.

    Config knobs (a profile's [target] table, beyond 'provider'):
      top_n, min_bounty — override whatever the CLI/pipeline passed in, so a
      profile can pin "always the top 20 programs, $500+ bounty" regardless
      of run-time flags.
    """

    def __init__(self, **config: Any) -> None:
        self.config = config

    async def get_targets(self, pipeline: Any, top_n: int, min_bounty: float) -> list[BountyProgram] | None:
        from agents.messaging import TaskAssignment

        top_n = self.config.get("top_n", top_n)
        min_bounty = self.config.get("min_bounty", min_bounty)

        if not await pipeline._check_pause_stop():
            return None
        pipeline._emit_phase("discover")
        logger.info("=== Phase 1: Program Discovery ===")
        discover_result = await pipeline.discover.execute(
            TaskAssignment(
                agent_name="discover",
                task_type="discover",
                parameters={"top_n": top_n * 2, "min_bounty": min_bounty},
                context="Find and rank bug bounty programs by ROI.",
            )
        )
        if not discover_result.success or not discover_result.data:
            logger.warning("Discovery failed or returned no programs.")
            pipeline._print_conversation_log()
            return None

        programs = pipeline._coerce_programs(discover_result.data)
        if not programs:
            logger.warning("Discovery returned data but no valid programs could be extracted.")
            pipeline._print_conversation_log()
            return None

        selected = await pipeline.director.decide_programs(programs, discover_result.reflection, top_n=top_n)
        pipeline.session.programs = selected
        for p in selected:
            pipeline.persistent.save_program(p.model_dump())
            if pipeline.graph:
                await pipeline.graph.store_program(p)

        if not await pipeline._checkpoint("discover", selected):
            return None
        return selected


class AutoTargetProvider:
    """Default provider: direct targets if the caller gave --targets, else
    bug-bounty discovery. Exists so existing behavior is preserved exactly
    when no --profile / explicit provider is chosen — a fresh install keeps
    working precisely as it did before this module existed."""

    def __init__(self, **config: Any) -> None:
        self.config = config
        self._direct = DirectTargetProvider(**config)
        self._bounty = BugBountyTargetProvider(**config)

    async def get_targets(self, pipeline: Any, top_n: int, min_bounty: float) -> list[BountyProgram] | None:
        if getattr(pipeline, "_direct_targets", None):
            return await self._direct.get_targets(pipeline, top_n, min_bounty)
        return await self._bounty.get_targets(pipeline, top_n, min_bounty)
