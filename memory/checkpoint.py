"""Pipeline checkpoint persistence — save/restore swarm state for stop/resume.

Stores checkpoint data in the PersistentMemory kv table under keys like
`checkpoint:<scan_id>` and `checkpoint:latest`.

Each run gets a human-friendly name (e.g. "bold-falcon-42") for easy reference.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memory.shared import SharedMemory
    from memory.store import PersistentMemory

logger = logging.getLogger(__name__)

# Word lists for generating memorable run names
_ADJECTIVES = [
    "bold", "calm", "dark", "fast", "keen", "loud", "pure", "red", "swift", "wild",
    "blue", "cold", "deep", "gold", "iron", "jade", "neon", "pale", "sharp", "warm",
    "bright", "clever", "fierce", "gentle", "hidden", "lucky", "noble", "quiet", "rusty", "silent",
]
_NOUNS = [
    "arrow", "blade", "cobra", "delta", "eagle", "flame", "ghost", "hawk", "ion", "jet",
    "knife", "lance", "moth", "nova", "orbit", "pulse", "reef", "storm", "titan", "viper",
    "wolf", "byte", "cipher", "drone", "flare", "grid", "helix", "nexus", "proxy", "raven",
]


def generate_run_name(scan_id: int | None = None) -> str:
    """Generate a memorable run name like 'bold-falcon-42'."""
    adj = random.choice(_ADJECTIVES)
    noun = random.choice(_NOUNS)
    num = scan_id if scan_id is not None else random.randint(10, 99)
    return f"{adj}-{noun}-{num}"


@dataclass
class PipelineCheckpoint:
    """Snapshot of pipeline state at a point in time."""

    scan_id: int
    created_at: float
    pipeline_mode: str  # "swarm" | "lab"
    phase: str  # "discover" | "swarm" | "debrief" | "report"
    phase_completed: list[str] = field(default_factory=list)

    # Config
    num_agents: int = 3
    swarm_timeout_minutes: int = 30
    budget: float = 5.0

    # Programs + domains
    selected_programs: list[dict] = field(default_factory=list)
    all_domains: list[str] = field(default_factory=list)
    assignment_context: str = ""

    # SharedMemory snapshot
    shared_memory: dict = field(default_factory=dict)

    # SessionMemory snapshot
    session: dict = field(default_factory=dict)

    # Cost
    cost_spent: float = 0.0

    # Human-friendly run name (e.g. "bold-falcon-42")
    run_name: str = ""

    # Direct targets (if --targets was used)
    direct_targets: list[str] | None = None
    direct_program_url: str | None = None


def capture_shared_memory(sm: SharedMemory) -> dict:
    """Snapshot SharedMemory state into a serializable dict."""
    entries = []
    for entry in sm._entries:
        entries.append({
            "id": entry.id,
            "agent_id": entry.agent_id,
            "entry_type": entry.entry_type,
            "category": entry.category,
            "key": entry.key,
            "content": entry.content,
            "timestamp": entry.timestamp,
        })

    findings = []
    for finding in sm._findings:
        findings.append(finding.model_dump(mode="json"))

    # Serialize scores
    scores = {}
    for agent_id, score in sm._scores.items():
        scores[agent_id] = {
            "total": score.total,
            "events": score.events,
        }

    return {
        "entries": entries,
        "entry_keys": list(sm._entry_keys),
        "findings": findings,
        "finding_keys": list(sm._finding_keys),
        "next_id": sm._next_id,
        "scores": scores,
    }


def restore_shared_memory(sm: SharedMemory, data: dict) -> None:
    """Rebuild SharedMemory from a checkpoint snapshot.

    Claims are intentionally NOT restored — the old agents are dead,
    so new agents need to re-claim targets.
    """
    from memory.shared import MemoryEntry
    from models.finding import VulnFinding

    sm._entries.clear()
    sm._entry_keys.clear()
    sm._findings.clear()
    sm._finding_keys.clear()
    sm._claims.clear()  # Release all claims

    for entry_data in data.get("entries", []):
        entry = MemoryEntry(
            id=entry_data["id"],
            agent_id=entry_data["agent_id"],
            entry_type=entry_data["entry_type"],
            category=entry_data["category"],
            key=entry_data["key"],
            content=entry_data["content"],
            timestamp=entry_data.get("timestamp", time.time()),
        )
        sm._entries.append(entry)
        sm._entry_keys.add(entry.key)

    for finding_data in data.get("findings", []):
        try:
            finding = VulnFinding(**finding_data)
            sm._findings.append(finding)
        except Exception:
            logger.debug("Could not restore finding: %s", finding_data, exc_info=True)

    sm._finding_keys = set(data.get("finding_keys", []))
    sm._next_id = data.get("next_id", len(sm._entries) + 1)

    # Restore scores
    from memory.shared import AgentScore
    sm._scores.clear()
    for agent_id, score_data in data.get("scores", {}).items():
        score = AgentScore(
            total=score_data.get("total", 0),
            events=score_data.get("events", []),
        )
        sm._scores[agent_id] = score

    score_count = len(sm._scores)
    logger.info(
        "Restored SharedMemory: %d entries, %d findings, %d agent scores (claims cleared)",
        len(sm._entries), len(sm._findings), score_count,
    )


def capture_session(session) -> dict:
    """Snapshot SessionMemory fields needed for resume."""
    return {
        "targets_scanned": list(session.targets_scanned),
        "targets_skipped": list(session.targets_skipped),
        "notes": dict(session.notes),
        "scope_rules": dict(session.scope_rules),
    }


def restore_session(session, data: dict) -> None:
    """Rebuild SessionMemory fields from checkpoint."""
    session.targets_scanned = data.get("targets_scanned", [])
    session.targets_skipped = data.get("targets_skipped", [])
    session.notes = data.get("notes", {})
    session.scope_rules = data.get("scope_rules", {})


# -- Persistence helpers --


def save_checkpoint(store: PersistentMemory, checkpoint: PipelineCheckpoint) -> None:
    """Serialize checkpoint to kv table."""
    data = asdict(checkpoint)
    store.set(f"checkpoint:{checkpoint.scan_id}", data)
    store.set("checkpoint:latest", data)
    # Store run_name → scan_id mapping for lookup by name
    if checkpoint.run_name:
        store.set(f"run_name:{checkpoint.run_name}", {"scan_id": checkpoint.scan_id})
    logger.info(
        "Checkpoint saved: %s (scan=%d) phase=%s domains=%d findings=%d cost=$%.2f",
        checkpoint.run_name or "?", checkpoint.scan_id, checkpoint.phase,
        len(checkpoint.all_domains),
        len(checkpoint.shared_memory.get("findings", [])),
        checkpoint.cost_spent,
    )


def load_checkpoint(
    store: PersistentMemory,
    scan_id: int | None = None,
    run_name: str | None = None,
) -> PipelineCheckpoint | None:
    """Load a checkpoint from kv table.

    Accepts scan_id (int), run_name (str like "bold-falcon-42"),
    or neither (loads latest).
    """
    # Resolve run_name to scan_id
    if run_name:
        mapping = store.get(f"run_name:{run_name}")
        if mapping and "scan_id" in mapping:
            scan_id = mapping["scan_id"]
        else:
            logger.warning("No checkpoint found for run name: %s", run_name)
            return None

    if scan_id is not None:
        data = store.get(f"checkpoint:{scan_id}")
        if not data:
            return None
        try:
            return PipelineCheckpoint(**data)
        except Exception:
            logger.warning("Could not deserialize checkpoint:%s", scan_id, exc_info=True)
            return None

    # Try latest pointer first
    data = store.get("checkpoint:latest")
    if data:
        try:
            return PipelineCheckpoint(**data)
        except Exception:
            logger.warning("Could not deserialize checkpoint:latest", exc_info=True)

    # Fallback: find the most recently updated per-scan checkpoint
    row = store.conn.execute(
        "SELECT value FROM kv WHERE key LIKE 'checkpoint:%' AND key != 'checkpoint:latest' "
        "ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if row:
        try:
            fallback_data = json.loads(row["value"])
            cp = PipelineCheckpoint(**fallback_data)
            logger.info("Loaded fallback checkpoint: scan=%d (checkpoint:latest was missing)", cp.scan_id)
            return cp
        except Exception:
            logger.warning("Could not deserialize fallback checkpoint", exc_info=True)

    return None


def list_checkpoints(store: PersistentMemory) -> list[dict]:
    """List all available checkpoints with summary info."""
    rows = store.conn.execute(
        "SELECT key, value, updated_at FROM kv WHERE key LIKE 'checkpoint:%' AND key != 'checkpoint:latest' ORDER BY updated_at DESC"
    ).fetchall()

    results = []
    for row in rows:
        try:
            data = json.loads(row["value"])
            results.append({
                "scan_id": data.get("scan_id"),
                "run_name": data.get("run_name", ""),
                "phase": data.get("phase"),
                "pipeline_mode": data.get("pipeline_mode"),
                "domains": len(data.get("all_domains", [])),
                "findings": len(data.get("shared_memory", {}).get("findings", [])),
                "cost_spent": data.get("cost_spent", 0.0),
                "created_at": data.get("created_at"),
                "updated_at": row["updated_at"],
            })
        except Exception:
            continue
    return results


def delete_checkpoint(store: PersistentMemory, scan_id: int) -> None:
    """Remove a checkpoint after successful completion."""
    # Get run_name before deleting so we can clean up the mapping
    data = store.get(f"checkpoint:{scan_id}")
    run_name = data.get("run_name", "") if data else ""

    store.conn.execute("DELETE FROM kv WHERE key=?", (f"checkpoint:{scan_id}",))
    # Also clear latest if it points to this scan
    latest = store.get("checkpoint:latest")
    if latest and latest.get("scan_id") == scan_id:
        store.conn.execute("DELETE FROM kv WHERE key='checkpoint:latest'")
    # Clean up run_name mapping
    if run_name:
        store.conn.execute("DELETE FROM kv WHERE key=?", (f"run_name:{run_name}",))
    store.conn.commit()
    logger.info("Checkpoint deleted: scan=%d (%s)", scan_id, run_name or "no name")
