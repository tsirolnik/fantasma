"""Load a Profile from a TOML file — the non-Python authoring path.

Schema (every section optional; missing sections fall back to the bugbounty
defaults):

    [profile]
    name = "my-profile"
    description = "..."

    [methodology]
    type = "pentest"              # built-in name, or "module.path:ClassName"

    [target]
    provider = "auto"             # built-in name, or "module.path:ClassName"

    [topology]
    mode = "swarm"                # swarm | lab — no "group" value; see below
    agents_per_team = 4
    num_teams = 2                 # >1 on "swarm" IS multi-team competition

    [mechanisms]
    enabled = ["first_finder_bonus", "duplicate_escalation"]

    [scoring]
    default_accepted = 10
    rejected = -10
    duplicate = -5
    termination_threshold = -50
    [scoring.accepted]
    critical = 50
    high = 30
    medium = 15
    low = 5

See profiles/bugbounty.toml and profiles/redteam.toml for complete examples —
bugbounty.toml is the original/default behavior expressed entirely as this format.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from profiles import (
    Profile, Topology, LabWorkflow, Interaction, Dashboard,
    BUILTIN_METHODOLOGIES, BUILTIN_TARGET_PROVIDERS, _resolve_class,
)
from agents.llm_config import LLMConfigMap, AgentLLMConfig, configured_llm_map
from memory.scoring_policy import ScoringPolicy, DEFAULT_SCORING
from memory.coordination_policy import CoordinationPolicy, DEFAULT_COORDINATION
from memory.mechanisms import MECHANISM_REGISTRY

PROFILES_DIR = Path(__file__).resolve().parent


def _resolve_llm(raw: dict) -> LLMConfigMap:
    """Profile fields override role settings, which inherit LLM_PROVIDER."""
    source = raw.get("source", "env")
    if source != "env":
        raise ValueError(f"Unsupported llm.source: {source!r}; expected 'env' (.env/environment)")
    cfg = configured_llm_map()
    cfg.retain_thinking = raw.get("retain_thinking", True)
    if type(cfg.retain_thinking) is not bool:
        raise ValueError("llm.retain_thinking must be a boolean")
    def merge(base, spec):
        provider = spec.get("provider", base.provider)
        model = spec.get("model", "" if provider != base.provider else base.model)
        return AgentLLMConfig(provider=provider, model=model)
    for role in ("director", "default_worker"):
        if role in raw:
            setattr(cfg, role, merge(getattr(cfg, role), raw[role]))
    for agent_name, spec in raw.get("overrides", {}).items():
        cfg.overrides[agent_name] = merge(cfg.default_worker, spec)
    return cfg


def _resolve_coordination(raw: dict) -> CoordinationPolicy:
    if not raw:
        return DEFAULT_COORDINATION
    return CoordinationPolicy(
        claim_mode=raw.get("claim_mode", DEFAULT_COORDINATION.claim_mode),
        director_review_interval_s=raw.get("director_review_interval_s",
                                            DEFAULT_COORDINATION.director_review_interval_s),
        cross_team_sharing=raw.get("cross_team_sharing", DEFAULT_COORDINATION.cross_team_sharing),
    )


def _resolve_scoring(raw: dict) -> ScoringPolicy:
    if not raw:
        return DEFAULT_SCORING
    return ScoringPolicy(
        accepted=dict(raw.get("accepted", DEFAULT_SCORING.accepted)),
        default_accepted=raw.get("default_accepted", DEFAULT_SCORING.default_accepted),
        rejected=raw.get("rejected", DEFAULT_SCORING.rejected),
        duplicate=raw.get("duplicate", DEFAULT_SCORING.duplicate),
        termination_threshold=raw.get("termination_threshold", DEFAULT_SCORING.termination_threshold),
    )


def parse_profile_toml(text: str, *, source: str = "<string>") -> Profile:
    """Parse TOML text into a Profile. Raises ValueError with a clear message
    on an unknown methodology/target-provider/mechanism name — never silently
    falls back, since a typo'd config silently running the wrong behavior is
    worse than a startup error."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{source}: invalid TOML: {exc}") from exc

    meta = data.get("profile", {})
    name = meta.get("name", Path(source).stem)
    description = meta.get("description", "")

    # Every key besides the one that SELECTS the class is passed through as a
    # constructor kwarg — the same generic config-passthrough mechanisms get
    # via build_mechanisms. A profile isn't limited to knobs this file named
    # in advance; a class (built-in or a third party's own) just reads
    # self.config for whatever it wants.
    methodology_table = dict(data.get("methodology", {}))
    methodology_spec = methodology_table.pop("type", "pentest")
    methodology_cls = _resolve_class(methodology_spec, BUILTIN_METHODOLOGIES)
    try:
        methodology = methodology_cls(**methodology_table)
    except TypeError as exc:
        raise ValueError(f"{source}: bad [methodology] parameters: {exc}") from exc

    target_table = dict(data.get("target", {}))
    provider_spec = target_table.pop("provider", "auto")
    provider_cls = _resolve_class(provider_spec, BUILTIN_TARGET_PROVIDERS)
    try:
        target_provider = provider_cls(**target_table)
    except TypeError as exc:
        raise ValueError(f"{source}: bad [target] parameters: {exc}") from exc

    topo_raw = data.get("topology", {})
    topology = Topology(
        mode="pentester" if topo_raw.get("mode") == "interactive" else topo_raw.get("mode", "swarm"),
        agents_per_team=topo_raw.get("agents_per_team", 4),
        num_teams=topo_raw.get("num_teams", 1),
    )
    if topology.mode not in ("swarm", "lab", "pentester"):
        # No "group" value: multi-team competition is num_teams > 1 on the
        # "swarm" engine, not a separate mode — see Topology's docstring.
        raise ValueError(f"{source}: [topology].mode must be 'swarm', 'lab' or 'pentester', got {topology.mode!r}. "
                          f"For multi-team competition, set num_teams > 1, not a separate mode.")

    # Two forms, mergeable: `enabled = [...]` plain names (default knobs), and
    # `[[mechanisms.items]]` tables for tuned knobs or a custom "class".
    mech_raw = data.get("mechanisms", {})
    if not isinstance(mech_raw, dict) or any(
        not isinstance(mech_raw.get(key, []), list) for key in ("enabled", "items")
    ):
        raise ValueError(f"{source}: mechanisms.enabled and mechanisms.items must be arrays")
    mechanisms: list = list(mech_raw.get("enabled", []))
    mechanisms += list(mech_raw.get("items", []))
    for index, spec in enumerate(mechanisms):
        if not isinstance(spec, (str, dict)):
            raise ValueError(f"{source}: mechanism must be a name or table, got {spec!r}")
        ref = spec if isinstance(spec, str) else (spec.get("class") or spec.get("name"))
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"{source}: mechanism requires a nonempty name or class")
        if ":" in ref:
            module, class_name = ref.split(":", 1)
            if module.endswith(".py"):
                # Keep external extensions beside their profile, independent of cwd.
                path = Path(module)
                if not path.is_absolute():
                    path = Path(source).resolve().parent / path
                ref = f"{path.resolve()}:{class_name}"
                if isinstance(spec, str):
                    mechanisms[index] = ref
                else:
                    spec["class" if "class" in spec else "name"] = ref
        if isinstance(ref, str) and ":" not in ref and ref not in MECHANISM_REGISTRY:
            raise ValueError(f"{source}: unknown mechanism {ref!r}. Known: {sorted(MECHANISM_REGISTRY)}")

    workflow_raw = data.get("workflow", {})
    try:
        workflow = LabWorkflow(**workflow_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source}: invalid [workflow]: {exc}") from exc
    if workflow_raw and topology.mode != "lab":
        raise ValueError(f"{source}: [workflow] is supported only with topology.mode = 'lab'")

    try:
        interaction_raw = dict(data.get("interaction", {}))
        if topo_raw.get("mode") == "interactive":
            interaction_raw.setdefault("mode", "terminal")
        dashboard_raw = dict(data.get("dashboard", {}))
        if interaction_raw.get("mode") == "web":
            interaction_raw["mode"] = "terminal" if topology.mode == "pentester" else "batch"
            dashboard_raw.setdefault("enabled", True)
        dashboard = Dashboard(**dashboard_raw)
        interaction = Interaction(**interaction_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source}: invalid [interaction]: {exc}") from exc
    if topology.mode != "pentester" and {"max_iterations", "max_tool_calls"} & set(interaction_raw):
        raise ValueError(f"{source}: interaction request limits apply only to pentester topology")
    if topology.mode == "pentester" and interaction.mode != "terminal":
        raise ValueError(f"{source}: pentester currently requires interaction.mode = 'terminal'")
    if topology.mode == "pentester" and (topology.num_teams != 1 or topology.agents_per_team != 1):
        raise ValueError(f"{source}: interactive sessions require one team and one pentester")

    scoring = _resolve_scoring(data.get("scoring", {}))
    llm = _resolve_llm(data.get("llm", {}))
    coordination = _resolve_coordination(data.get("coordination", {}))
    if coordination.claim_mode not in ("exclusive", "shared"):
        raise ValueError(f"{source}: [coordination].claim_mode must be 'exclusive' or 'shared', "
                          f"got {coordination.claim_mode!r}")

    from profiles.mcp import MCPServer
    raw_mcp = data.get("mcp", {})
    if not isinstance(raw_mcp, dict) or set(raw_mcp) - {"servers"} or not isinstance(raw_mcp.get("servers", []), list):
        raise ValueError("MCP configuration requires [[mcp.servers]] tables")
    try:
        mcp = [MCPServer(**item) for item in raw_mcp.get("servers", [])]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source}: invalid MCP configuration: {exc}") from exc
    if len({server.name for server in mcp}) != len(mcp):
        raise ValueError("MCP server names must be unique")
    for server in mcp:
        if server.cwd is not None:
            server.cwd = str((Path(source).resolve().parent / server.cwd).resolve())
    if mcp and topology.mode == "lab" and workflow.worker_mode != "tool":
        raise ValueError("Lab MCP requires workflow.worker_mode = tool")

    return Profile(
        mcp=mcp, dashboard=dashboard,
        name=name, description=description,
        methodology=methodology, target_provider=target_provider,
        scoring=scoring, mechanisms=mechanisms, topology=topology,
        llm=llm, coordination=coordination, workflow=workflow, interaction=interaction,
    )


def load_profile(name_or_path: str) -> Profile:
    """Resolve a profile by built-in name ('bugbounty', 'redteam'), a path to
    a .toml file, or the name of a .toml file shipped under profiles/.

    Built-in names are NOT a separately-maintained Python object — they
    resolve to profiles/<name>.toml exactly like any other shipped profile.
    There is exactly one source of truth for what a named profile means."""
    candidates = [Path(name_or_path)]
    if not name_or_path.endswith(".toml"):
        candidates.append(PROFILES_DIR / f"{name_or_path}.toml")
    else:
        candidates.append(PROFILES_DIR / name_or_path)

    for path in candidates:
        if path.is_file():
            return parse_profile_toml(path.read_text(), source=str(path))

    shipped = sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))
    raise ValueError(
        f"No profile named/at {name_or_path!r}. "
        f"Shipped under profiles/: {shipped}. Or pass a path to a .toml file."
    )
