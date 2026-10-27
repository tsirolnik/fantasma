"""Profiles — a full run configuration (methodology, targets, scoring,
mechanisms, topology) as one object, loadable from a TOML file.

This is the "bug-bounty hunting is just a config" layer: every piece
the engine takes (Methodology, TargetProvider, ScoringPolicy, mechanism
names, topology) is swappable independently in Python (agents/methodology.py,
agents/target_provider.py, memory/scoring_policy.py, memory/mechanisms.py),
and a Profile is simply the named BUNDLE of a choice for each — resolvable
from a TOML file so non-Python users can author new behavior too.

`Profile` is also the stable, serializable "what is this run configured to
do" object other code (a dashboard, a report, an API) can consume via
`.summary()` without reaching into pipeline/agent internals.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from profiles.mcp import MCPServer

from agents.methodology import Methodology, PentestMethodology, RedTeamMethodology, InteractiveMethodology
from agents.target_provider import (
    TargetProvider, AutoTargetProvider, DirectTargetProvider, BugBountyTargetProvider,
)
from agents.llm_config import LLMConfigMap
from memory.scoring_policy import ScoringPolicy, DEFAULT_SCORING
from memory.coordination_policy import CoordinationPolicy, DEFAULT_COORDINATION

# Built-in name -> class, for the common case. Anything else in a TOML file
# is treated as a "module.path:ClassName" dotted import (see _resolve_class),
# which is how a third party plugs in their OWN methodology/target provider
# without touching framework code.
BUILTIN_METHODOLOGIES: dict[str, type[Methodology]] = {
    "interactive": InteractiveMethodology,
    "pentest": PentestMethodology,
    "redteam": RedTeamMethodology,
}
BUILTIN_TARGET_PROVIDERS: dict[str, type[TargetProvider]] = {
    "auto": AutoTargetProvider,
    "direct": DirectTargetProvider,
    "bugbounty": BugBountyTargetProvider,
}


def _resolve_class(spec: str, builtins: dict[str, type]) -> type:
    """'pentest' -> PentestMethodology (built-in name), or
    'mypackage.mymod:MyMethodology' -> that class (third-party extension)."""
    if spec in builtins:
        return builtins[spec]
    if ":" not in spec:
        raise ValueError(
            f"Unknown name {spec!r}. Known: {sorted(builtins)}. "
            f"For a custom class, use 'module.path:ClassName'."
        )
    module_path, class_name = spec.split(":", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ValueError(f"{module_path!r} has no attribute {class_name!r}")
    return cls


@dataclass
class Topology:
    """Execution topology: pentester (conversational worker), swarm (teams),
    or lab (sequential stages). Interaction is configured independently.
    Multiple teams use swarm with num_teams > 1, not a separate group mode."""
    mode: str = "swarm"
    agents_per_team: int = 4
    num_teams: int = 1


@dataclass
class LabWorkflow:
    """Supported sequential stages; data dependencies determine their order."""
    stages: list[str] = field(default_factory=lambda: ["recon", "scan", "analyze", "report"])
    worker_mode: str = "auto"
    max_scan_targets: int = 50

    def __post_init__(self):
        order = ["recon", "scan", "analyze", "report"]
        if (not isinstance(self.stages, list) or
                any(stage not in order for stage in self.stages) or
                self.stages != [stage for stage in order if stage in self.stages] or
                "scan" not in self.stages):
            raise ValueError("workflow.stages must be an ordered subset of recon, scan, analyze, report containing scan")
        if self.worker_mode not in ("auto", "tool", "deterministic"):
            raise ValueError("workflow.worker_mode must be auto, tool or deterministic")
        if type(self.max_scan_targets) is not int or self.max_scan_targets < 1:
            raise ValueError("workflow.max_scan_targets must be a positive integer")


@dataclass
class Interaction:
    mode: str = "batch"  # batch | terminal; dashboard is independent
    max_iterations: int = 20
    max_tool_calls: int = 50
    goal_max_idle_turns: int = 3
    goal_max_reviews: int = 3
    diagnostics: bool = False

    def __post_init__(self):
        if type(self.diagnostics) is not bool:
            raise ValueError("interaction.diagnostics must be a boolean")
        if self.mode not in ("batch", "terminal"):
            raise ValueError("interaction.mode must be batch or terminal")
        for name in ("max_iterations", "max_tool_calls", "goal_max_idle_turns", "goal_max_reviews"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"interaction.{name} must be a positive integer")


@dataclass
class Dashboard:
    enabled: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("dashboard.enabled must be a boolean")


@dataclass
class Profile:
    """A complete run configuration. Always resolved from TOML via
    `profiles.loader.load_profile(name_or_path)` — including the built-in
    names ("bugbounty", "redteam"), which load profiles/<name>.toml rather
    than a separately-maintained Python object, so there is exactly one
    source of truth for what "default behavior" means. Construct a Profile
    directly in Python only for programmatic/test use, never as a second
    built-in definition."""

    name: str
    description: str = ""
    methodology: Methodology = field(default_factory=PentestMethodology)
    target_provider: TargetProvider = field(default_factory=AutoTargetProvider)
    scoring: ScoringPolicy = field(default_factory=lambda: DEFAULT_SCORING)
    mechanisms: list[str | dict] = field(default_factory=list)
    topology: Topology = field(default_factory=Topology)
    llm: LLMConfigMap = field(default_factory=LLMConfigMap)
    interaction: Interaction = field(default_factory=Interaction)
    workflow: LabWorkflow = field(default_factory=LabWorkflow)
    coordination: CoordinationPolicy = field(default_factory=lambda: DEFAULT_COORDINATION)
    mcp: list[MCPServer] = field(default_factory=list)
    dashboard: Dashboard = field(default_factory=Dashboard)

    def summary(self) -> dict[str, Any]:
        """Stable, serializable 'what is this run configured to do' view —
        the API surface a dashboard or report can consume without reaching
        into pipeline/agent internals."""
        return {
            "name": self.name,
            "mcp": [asdict(server) for server in self.mcp],
            "description": self.description,
            "methodology": getattr(self.methodology, "name", type(self.methodology).__name__),
            "target_provider": type(self.target_provider).__name__,
            "scoring": asdict(self.scoring),
            "mechanisms": list(self.mechanisms),
            "topology": asdict(self.topology),
            "llm": {
                "director": asdict(self.llm.director),
                "default_worker": asdict(self.llm.default_worker),
                "overrides": {k: asdict(v) for k, v in self.llm.overrides.items()},
            },
            "coordination": asdict(self.coordination),
            "workflow": asdict(self.workflow),
            "interaction": asdict(self.interaction),
            "dashboard": asdict(self.dashboard),
        }
