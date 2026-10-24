"""Persistent cross-session memory backed by SQLite."""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    # FANTASMA_DB_PATH lets a single run (or an isolated experiment trial) use
    # its own database instead of the shared ./data/memory.db — so findings can
    # never leak between concurrent or back-to-back runs.
    return Path(os.environ.get("FANTASMA_DB_PATH") or os.environ.get("SWARMHOUND_DB_PATH") or "./data/memory.db")


DEFAULT_DB_PATH = _default_db_path()


class PersistentMemory:
    """SQLite-backed store for cross-session data: past scans, findings, program data."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                findings_count INTEGER DEFAULT 0,
                cost REAL DEFAULT 0.0,
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER REFERENCES scans(id),
                target TEXT NOT NULL,
                vuln_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT,
                confidence REAL DEFAULT 0.0,
                estimated_bounty REAL DEFAULT 0.0,
                was_submitted INTEGER DEFAULT 0,
                was_accepted INTEGER DEFAULT 0,
                actual_bounty REAL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                data TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS programs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                platform TEXT NOT NULL,
                url TEXT UNIQUE,
                last_scanned TEXT,
                scan_count INTEGER DEFAULT 0,
                total_findings INTEGER DEFAULT 0,
                total_bounty_earned REAL DEFAULT 0.0,
                roi_score REAL DEFAULT 0.0,
                data TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_findings_target ON findings(target);
            CREATE INDEX IF NOT EXISTS idx_findings_vuln_type ON findings(vuln_type);
            CREATE INDEX IF NOT EXISTS idx_scans_target ON scans(target);
            CREATE INDEX IF NOT EXISTS idx_programs_platform ON programs(platform);
        """)
        self.conn.commit()

    # ── Scans ──────────────────────────────────────────────────────

    def start_scan(self, target: str, metadata: dict | None = None) -> int:
        """Record a new scan start. Returns the scan ID."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO scans (target, started_at, metadata) VALUES (?, ?, ?)",
            (target, now, json.dumps(metadata or {})),
        )
        self.conn.commit()
        return cur.lastrowid

    def complete_scan(self, scan_id: int, findings_count: int, cost: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE scans SET completed_at=?, findings_count=?, cost=? WHERE id=?",
            (now, findings_count, cost, scan_id),
        )
        self.conn.commit()

    def update_scan_progress(
        self, scan_id: int | None, cost: float, findings_count: int | None = None
    ) -> None:
        """Live-update a running scan's cost (and optionally findings_count) so
        spend is durable even if the process is killed before complete_scan.
        Deliberately does NOT set completed_at — the scan is still in progress."""
        if scan_id is None:
            return
        if findings_count is None:
            self.conn.execute(
                "UPDATE scans SET cost=? WHERE id=?", (cost, scan_id)
            )
        else:
            self.conn.execute(
                "UPDATE scans SET cost=?, findings_count=? WHERE id=?",
                (cost, findings_count, scan_id),
            )
        self.conn.commit()

    def get_past_scans(self, target: str | None = None, limit: int = 50) -> list[dict]:
        if target:
            rows = self.conn.execute(
                "SELECT * FROM scans WHERE target=? ORDER BY started_at DESC LIMIT ?",
                (target, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Findings ───────────────────────────────────────────────────

    def save_finding(self, scan_id: int, finding_dict: dict) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO findings
               (scan_id, target, vuln_type, severity, title, confidence,
                estimated_bounty, created_at, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scan_id,
                finding_dict.get("target", ""),
                finding_dict.get("vuln_type", ""),
                finding_dict.get("severity", "info"),
                finding_dict.get("title", ""),
                finding_dict.get("confidence_score", 0.0),
                finding_dict.get("estimated_bounty", 0.0),
                now,
                json.dumps(finding_dict),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def mark_finding_submitted(self, finding_id: int, accepted: bool = False,
                                actual_bounty: float = 0.0) -> None:
        self.conn.execute(
            "UPDATE findings SET was_submitted=1, was_accepted=?, actual_bounty=? WHERE id=?",
            (int(accepted), actual_bounty, finding_id),
        )
        self.conn.commit()

    def get_past_findings(self, target: str | None = None,
                          vuln_type: str | None = None,
                          limit: int = 100) -> list[dict]:
        query = "SELECT * FROM findings WHERE 1=1"
        params: list = []
        if target:
            query += " AND target=?"
            params.append(target)
        if vuln_type:
            query += " AND vuln_type=?"
            params.append(vuln_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def was_recently_scanned(self, target: str, hours: int = 24) -> bool:
        """Check if target was scanned within the last N hours."""
        row = self.conn.execute(
            """SELECT MAX(started_at) as last_scan FROM scans
               WHERE target=? AND completed_at IS NOT NULL""",
            (target,),
        ).fetchone()
        if not row or not row["last_scan"]:
            return False
        last = datetime.fromisoformat(row["last_scan"])
        elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return elapsed < hours

    # ── Programs ───────────────────────────────────────────────────

    def save_program(self, program_dict: dict) -> None:
        """Upsert a program record."""
        self.conn.execute(
            """INSERT INTO programs (name, platform, url, roi_score, data)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                   roi_score=excluded.roi_score,
                   data=excluded.data""",
            (
                program_dict.get("name", ""),
                program_dict.get("platform", ""),
                program_dict.get("url", ""),
                program_dict.get("roi_score", 0.0),
                json.dumps(program_dict),
            ),
        )
        self.conn.commit()

    def get_programs(self, platform: str | None = None) -> list[dict]:
        if platform:
            rows = self.conn.execute(
                "SELECT * FROM programs WHERE platform=? ORDER BY roi_score DESC",
                (platform,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM programs ORDER BY roi_score DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_program_stats(self, url: str, findings: int = 0, bounty: float = 0.0) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE programs SET
                   last_scanned=?, scan_count=scan_count+1,
                   total_findings=total_findings+?, total_bounty_earned=total_bounty_earned+?
               WHERE url=?""",
            (now, findings, bounty, url),
        )
        self.conn.commit()

    # ── Cost aggregation ─────────────────────────────────────────

    def get_total_cost(self) -> float:
        """Return total cost across all completed scans."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0.0) AS total FROM scans WHERE cost > 0"
        ).fetchone()
        return row["total"]

    def get_cost_history(self, limit: int = 30) -> list[dict]:
        """Return per-scan cost history (most recent first)."""
        rows = self.conn.execute(
            "SELECT id, target, started_at, completed_at, findings_count, cost "
            "FROM scans WHERE cost > 0 ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_cost_stats(self) -> dict:
        """Return aggregate cost statistics across all sessions."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS total_scans, "
            "COALESCE(SUM(cost), 0.0) AS total_cost, "
            "COALESCE(AVG(cost), 0.0) AS avg_cost, "
            "COALESCE(MAX(cost), 0.0) AS max_cost, "
            "COALESCE(SUM(findings_count), 0) AS total_findings "
            "FROM scans WHERE cost > 0"
        ).fetchone()
        return dict(row)

    # ── Key-Value store ────────────────────────────────────────────

    def set(self, key: str, value: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), now),
        )
        self.conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row:
            return json.loads(row["value"])
        return default

    def close(self) -> None:
        self.conn.close()
