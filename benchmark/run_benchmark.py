#!/usr/bin/env python3
"""
fantasma benchmark runner.

Brings up an intentionally-vulnerable target, runs fantasma against it, then
scores the findings against a ground-truth manifest. Reports recall (how many
known vuln classes were found), precision (how many findings map to a real
class vs. noise), cost, and wall-clock time.

Targets are handled ONE AT A TIME so findings attribute cleanly by time window.

Usage
-----
    # dry run — print the exact fantasma command, spend nothing
    python benchmark/run_benchmark.py --targets juice-shop --dry-run

    # real run against Claude
    python benchmark/run_benchmark.py --targets juice-shop \
        --director-llm claude --worker-llm claude --analyzer-llm claude \
        --budget 5 --agents 3 --timeout 1200

    # all targets
    python benchmark/run_benchmark.py --targets juice-shop,dvwa,vampi ...

The runner never brings targets down that it didn't bring up, and always tries
to stop the ones it started.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MANIFEST = HERE / "targets.json"
COMPOSE = HERE / "docker-compose.yml"
DB_PATH = REPO / "data" / "memory.db"
RESULTS_DIR = HERE / "results"


# --------------------------------------------------------------------------- #
# Manifest + scoring
# --------------------------------------------------------------------------- #
def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def score_target(findings: list[dict], ground_truth: list[str], vuln_classes: dict) -> dict:
    """Class-level scoring.

    A ground-truth class counts as DETECTED if any finding's vuln_type or its
    combined text (title+description+evidence) matches the class name or any of
    its aliases. A finding is ON-TARGET if it maps to at least one known class.
    """
    def finding_text(f: dict) -> str:
        parts = [
            f.get("vuln_type", ""), f.get("title", ""), f.get("description", ""),
            f.get("evidence", ""), f.get("url", ""),
        ]
        return " ".join(str(p) for p in parts).lower()

    def classes_for(f: dict) -> set[str]:
        text = finding_text(f)
        hit = set()
        for cls, aliases in vuln_classes.items():
            needles = [cls.replace("_", " ")] + list(aliases)
            if any(n.lower() in text for n in needles):
                hit.add(cls)
        return hit

    finding_classes = [classes_for(f) for f in findings]
    detected = set()
    for cs in finding_classes:
        detected |= cs

    gt = set(ground_truth)
    true_positives = sorted(gt & detected)
    missed = sorted(gt - detected)
    extra = sorted(detected - gt)  # classes found that weren't in ground truth

    on_target = sum(1 for cs in finding_classes if cs & gt)
    noise = len(findings) - on_target

    recall = len(true_positives) / len(gt) if gt else 0.0
    precision = on_target / len(findings) if findings else 0.0

    return {
        "num_findings": len(findings),
        "ground_truth_classes": sorted(gt),
        "detected_classes": true_positives,
        "missed_classes": missed,
        "extra_classes": extra,
        "on_target_findings": on_target,
        "noise_findings": noise,
        "recall": round(recall, 3),
        "precision": round(precision, 3),
    }


# --------------------------------------------------------------------------- #
# DB access (read-only; findings/cost written by the fantasma run)
# --------------------------------------------------------------------------- #
def _connect_ro(db_path: "Path | None" = None) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path or DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_findings_since(since_iso: str, db_path: "Path | None" = None) -> list[dict]:
    db = db_path or DB_PATH
    if not db.exists():
        return []
    out = []
    with _connect_ro(db) as conn:
        rows = conn.execute(
            "SELECT target, vuln_type, severity, title, created_at, data "
            "FROM findings WHERE created_at >= ? ORDER BY id",
            (since_iso,),
        ).fetchall()
    for r in rows:
        try:
            data = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            data = {}
        out.append({
            "target": r["target"],
            "vuln_type": data.get("vuln_type", r["vuln_type"]),
            "severity": data.get("severity", r["severity"]),
            "title": data.get("title", r["title"] or ""),
            "description": data.get("description", ""),
            "evidence": data.get("evidence", ""),
            "url": data.get("url", ""),
            "created_at": r["created_at"],
        })
    return out


def read_findings_by_scan(scan_id: int) -> list[dict]:
    if not DB_PATH.exists():
        return []
    out = []
    with _connect_ro() as conn:
        rows = conn.execute(
            "SELECT target, vuln_type, severity, title, data FROM findings "
            "WHERE scan_id = ? ORDER BY id", (scan_id,),
        ).fetchall()
    for r in rows:
        try:
            data = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            data = {}
        out.append({
            "target": r["target"],
            "vuln_type": data.get("vuln_type", r["vuln_type"]),
            "severity": data.get("severity", r["severity"]),
            "title": data.get("title", r["title"] or ""),
            "description": data.get("description", ""),
            "evidence": data.get("evidence", ""),
            "url": data.get("url", ""),
        })
    return out


def read_cost_since(since_iso: str, db_path: "Path | None" = None) -> float:
    db = db_path or DB_PATH
    if not db.exists():
        return 0.0
    with _connect_ro(db) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS c FROM scans WHERE started_at >= ?",
            (since_iso,),
        ).fetchone()
    return float(row["c"]) if row else 0.0


# --------------------------------------------------------------------------- #
# Target lifecycle
# --------------------------------------------------------------------------- #
def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), *args],
        capture_output=True, text=True,
    )


def wait_healthy(url: str, timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def build_command(target: dict, args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable, "main.py", "full",
        "--mode", args.mode,
        # Scope is just localhost — the swarm always addresses the target as
        # localhost:PORT. Host-side tools reach it directly; sandboxed Docker
        # tools have localhost:PORT transparently rewritten to the target's
        # in-network container name (see benchmark_env / docker_runner).
        "--targets", "localhost",
        "--program-url", target["host_url"],
        "--auto",
        "--swarm-agents", str(args.agents),
        # Multi-team competition is just num_teams > 1 on the same swarm
        # engine — there's no separate "group" mode value to select.
        "--num-teams", str(args.num_teams),
    ]
    if args.budget is not None:
        cmd += ["--budget", str(args.budget)]
    if args.director_llm:
        cmd += ["--director-llm", args.director_llm]
    if args.worker_llm:
        cmd += ["--worker-llm", args.worker_llm]
    if args.analyzer_llm:
        cmd += ["--analyzer-llm", args.analyzer_llm]
    if args.extra:
        cmd += shlex.split(args.extra)
    return cmd


NETWORK_NAME = "fantasma-benchnet"  # created by the compose file


def benchmark_env(target: dict) -> dict:
    """Env for the fantasma subprocess: join the sandboxed tools to the shared
    bench network and rewrite localhost:PORT -> the target's container name."""
    port = target["host_url"].rsplit(":", 1)[-1]
    sh = target["sandbox_host"]          # e.g. bench-juice-shop:3000
    sh_host = sh.split(":", 1)[0]        # e.g. bench-juice-shop
    pairs = [
        f"localhost:{port}={sh}", f"127.0.0.1:{port}={sh}", f"host.docker.internal:{port}={sh}",
        f"localhost={sh_host}", f"127.0.0.1={sh_host}", f"host.docker.internal={sh_host}",
    ]
    env = dict(os.environ)
    env["FANTASMA_TOOL_NETWORK"] = NETWORK_NAME
    env["FANTASMA_TOOL_HOST_REWRITE"] = ",".join(pairs)
    return env


def run_one(target: dict, args: argparse.Namespace, vuln_classes: dict) -> dict:
    name = target["name"]
    service = name  # compose service name == target name
    print(f"\n{'='*70}\n  TARGET: {name}  ({target['host_url']})\n{'='*70}")

    # Dry run: print the command and touch nothing (no Docker, no spend).
    if args.dry_run:
        printable = " ".join(shlex.quote(c) for c in build_command(target, args))
        print(f"  → command:\n    {printable}")
        return {"target": name, "dry_run": True, "command": printable}

    started_here = False
    try:
        if not args.no_lifecycle:
            print(f"  → docker compose up -d {service}")
            up = compose("up", "-d", service)
            if up.returncode != 0:
                return {"target": name, "error": f"compose up failed: {up.stderr.strip()[:400]}"}
            started_here = True
            print(f"  → waiting for {target['healthcheck_url']} ...")
            if not wait_healthy(target["healthcheck_url"], timeout=args.health_timeout):
                return {"target": name, "error": "target did not become healthy in time"}
            print("  → healthy.")

        cmd = build_command(target, args)
        printable = " ".join(shlex.quote(c) for c in cmd)
        print(f"  → command:\n    {printable}")

        since = datetime.now(timezone.utc).isoformat()
        t0 = time.time()
        try:
            subprocess.run(cmd, cwd=str(REPO), timeout=args.timeout, env=benchmark_env(target))
        except subprocess.TimeoutExpired:
            print(f"  ! run hit --timeout ({args.timeout}s); scoring whatever landed so far")
        wall = round(time.time() - t0, 1)

        findings = read_findings_since(since)
        cost = read_cost_since(since)
        result = score_target(findings, target["ground_truth"], vuln_classes)
        result.update({"target": name, "wall_seconds": wall, "cost_usd": round(cost, 4)})
        return result
    finally:
        if started_here and not args.keep_up:
            print(f"  → docker compose stop {service}")
            compose("stop", service)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def render_markdown(results: list[dict], meta: dict) -> str:
    lines = [
        "# fantasma benchmark results",
        "",
        f"- **Run:** {meta['timestamp']}",
        f"- **Model config:** director=`{meta['director']}` worker=`{meta['worker']}` analyzer=`{meta['analyzer']}`",
        f"- **Agents:** {meta['agents']}  |  **Per-target budget:** {meta['budget']}",
        "",
        "| Target | Findings | On-target | Recall | Precision | Cost (USD) | Time (s) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    tot_f = tot_on = 0
    tot_cost = tot_time = 0.0
    for r in results:
        if r.get("error") or r.get("dry_run"):
            continue
        lines.append(
            f"| {r['target']} | {r['num_findings']} | {r['on_target_findings']} "
            f"| {r['recall']:.0%} | {r['precision']:.0%} | ${r['cost_usd']:.2f} | {r['wall_seconds']:.0f} |"
        )
        tot_f += r["num_findings"]; tot_on += r["on_target_findings"]
        tot_cost += r["cost_usd"]; tot_time += r["wall_seconds"]
    lines.append(f"| **total** | **{tot_f}** | **{tot_on}** | | | **${tot_cost:.2f}** | **{tot_time:.0f}** |")
    lines.append("")
    for r in results:
        if r.get("error"):
            lines += [f"## {r['target']}", f"> ⚠️ {r['error']}", ""]
            continue
        if r.get("dry_run"):
            continue
        lines += [
            f"## {r['target']}",
            f"- **Detected:** {', '.join(r['detected_classes']) or '—'}",
            f"- **Missed:** {', '.join(r['missed_classes']) or '—'}",
            f"- **Extra (not in ground truth):** {', '.join(r['extra_classes']) or '—'}",
            f"- **Noise findings (map to no known class):** {r['noise_findings']}",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="fantasma benchmark runner")
    ap.add_argument("--targets", default="juice-shop",
                    help="comma-separated target names from targets.json (default: juice-shop)")
    ap.add_argument("--agents", type=int, default=3, help="agents per team (swarm-agents)")
    ap.add_argument("--mode", choices=["swarm", "lab"], default="swarm",
                    help="pipeline engine (see main.py full --help). Multi-team "
                         "competition is --num-teams > 1 on 'swarm', not a separate mode.")
    ap.add_argument("--num-teams", type=int, default=1,
                    help="competing teams on the swarm engine (>1 = multi-team competition, default: 1)")
    ap.add_argument("--budget", type=float, default=None, help="per-target USD budget")
    ap.add_argument("--timeout", type=int, default=1200, help="per-target run timeout (s)")
    ap.add_argument("--health-timeout", type=int, default=180)
    ap.add_argument("--director-llm", default=None)
    ap.add_argument("--worker-llm", default=None)
    ap.add_argument("--analyzer-llm", default=None)
    ap.add_argument("--extra", default=None, help="extra args appended to the fantasma command")
    ap.add_argument("--dry-run", action="store_true", help="print command, spend nothing")
    ap.add_argument("--no-lifecycle", action="store_true",
                    help="assume targets already running; don't up/down them")
    ap.add_argument("--keep-up", action="store_true", help="leave targets running after the run")
    ap.add_argument("--rescore", type=int, metavar="SCAN_ID",
                    help="re-score an existing scan's findings against the current manifest (no run, no spend)")
    args = ap.parse_args()

    manifest = load_manifest()
    vuln_classes = manifest["vuln_classes"]
    by_name = {t["name"]: t for t in manifest["targets"]}

    # ── Re-score an existing scan against the current manifest ──────────
    if args.rescore is not None:
        wanted = [t.strip() for t in args.targets.split(",") if t.strip()]
        target = by_name[wanted[0]]
        findings = read_findings_by_scan(args.rescore)
        r = score_target(findings, target["ground_truth"], vuln_classes)
        r.update({"target": target["name"], "wall_seconds": 0, "cost_usd": 0.0})
        meta = {"timestamp": f"rescore-scan{args.rescore}", "agents": "-", "budget": "-",
                "director": "-", "worker": "-", "analyzer": "-"}
        print(render_markdown([r], meta))
        return 0

    wanted = [t.strip() for t in args.targets.split(",") if t.strip()]
    unknown = [w for w in wanted if w not in by_name]
    if unknown:
        print(f"Unknown targets: {unknown}. Available: {list(by_name)}")
        return 2

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = [run_one(by_name[w], args, vuln_classes) for w in wanted]

    if args.dry_run:
        print("\n(dry run — nothing scored)")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    meta = {
        "timestamp": ts, "agents": args.agents, "budget": args.budget,
        "director": args.director_llm or "default",
        "worker": args.worker_llm or "default",
        "analyzer": args.analyzer_llm or "default",
    }
    md = render_markdown(results, meta)
    (RESULTS_DIR / f"benchmark_{ts}.json").write_text(
        json.dumps({"meta": meta, "results": results}, indent=2))
    (RESULTS_DIR / f"benchmark_{ts}.md").write_text(md)
    print("\n" + md)
    print(f"\nSaved: benchmark/results/benchmark_{ts}.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
