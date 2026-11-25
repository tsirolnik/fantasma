"""Graph RAG memory backed by FalkorDB (Redis-based graph database).

Provides cross-session relational learning: which vuln types work on
targets with similar tech stacks, what techniques produced accepted
findings, domain context with full history, etc.

Complements (does not replace) the SQLite PersistentMemory.  If
FalkorDB is unavailable, everything degrades gracefully — the pipeline
runs as before with SQLite only.

Graph schema:
  (:Program {url, name, platform, max_bounty})
  (:Domain {name, first_seen, last_scanned})
  (:Technology {name})
  (:Finding {id, title, severity, confidence, bounty, accepted, created_at})
  (:VulnType {name})
  (:Scan {id, started_at, findings_count, cost})
  (:Technique {name})
  (:Endpoint {path, method, domain})

Edges:
  (Program)-[:HAS_DOMAIN]->(Domain)
  (Domain)-[:USES_TECH]->(Technology)
  (Domain)-[:HAS_FINDING]->(Finding)
  (Finding)-[:IS_TYPE]->(VulnType)
  (Finding)-[:USED_TECHNIQUE]->(Technique)
  (Finding)-[:AT_ENDPOINT]->(Endpoint)
  (Scan)-[:SCANNED]->(Domain)
  (Scan)-[:FOUND]->(Finding)
  (Domain)-[:HAS_ENDPOINT]->(Endpoint)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Callback signature: (op_type: str, details: dict) -> None
GraphEventCallback = Callable[[str, dict], None]

DOCKER_BIN = shutil.which("docker") or "docker"


class GraphMemory:
    """FalkorDB-backed graph memory for cross-session learning."""

    CONTAINER_NAME = "bugbounty-falkordb"
    IMAGE = "falkordb/falkordb:latest"
    GRAPH_NAME = "bugbounty"
    PORT = 6380  # avoid conflict with user's Redis on 6379

    def __init__(self, host: str = "localhost", port: int = 6380) -> None:
        self.host = host
        self.port = port
        self._db = None
        self._graph = None
        self._event_callback: GraphEventCallback | None = None
        self._op_counts: dict[str, int] = {}  # op_type → count

    def _emit(self, op_type: str, **details: Any) -> None:
        """Log a graph operation and notify the event callback."""
        self._op_counts[op_type] = self._op_counts.get(op_type, 0) + 1
        logger.debug("graph op: %s %s", op_type, details)
        if self._event_callback:
            try:
                self._event_callback(op_type, details)
            except Exception:
                pass

    @property
    def op_counts(self) -> dict[str, int]:
        """Return a copy of operation counts."""
        return dict(self._op_counts)

    @property
    def total_ops(self) -> int:
        return sum(self._op_counts.values())

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def ensure_container(self) -> bool:
        """Ensure the FalkorDB container is running. Returns True on success."""
        try:
            # Check if container is already running
            proc = await asyncio.create_subprocess_exec(
                DOCKER_BIN, "inspect", "-f", "{{.State.Running}}",
                self.CONTAINER_NAME,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and b"true" in stdout:
                logger.info("FalkorDB container already running")
                return True

            # Check if container exists but stopped
            if proc.returncode == 0:
                logger.info("Starting stopped FalkorDB container")
                proc = await asyncio.create_subprocess_exec(
                    DOCKER_BIN, "start", self.CONTAINER_NAME,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                if proc.returncode == 0:
                    await asyncio.sleep(2)  # wait for Redis to accept connections
                    return True

            # Container doesn't exist — create it
            logger.info("Creating FalkorDB container on port %d", self.port)

            # Ensure data directory exists
            import os
            os.makedirs("./data/falkordb", exist_ok=True)
            data_path = os.path.abspath("./data/falkordb")

            proc = await asyncio.create_subprocess_exec(
                DOCKER_BIN, "run", "-d",
                "--name", self.CONTAINER_NAME,
                "-p", f"{self.port}:6379",
                "-v", f"{data_path}:/var/lib/falkordb/data",
                "--restart", "unless-stopped",
                self.IMAGE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                logger.error("Failed to start FalkorDB: %s", stderr.decode())
                return False

            # Wait for FalkorDB to be ready
            await asyncio.sleep(3)
            logger.info("FalkorDB container started")
            return True

        except FileNotFoundError:
            logger.warning("Docker not found — graph memory disabled")
            return False
        except asyncio.TimeoutError:
            logger.warning("Timeout starting FalkorDB container")
            return False
        except Exception:
            logger.warning("Could not ensure FalkorDB container", exc_info=True)
            return False

    async def connect(self) -> bool:
        """Connect to FalkorDB. Returns True on success."""
        try:
            from falkordb.asyncio import FalkorDB
            self._db = FalkorDB(host=self.host, port=self.port)
            self._graph = self._db.select_graph(self.GRAPH_NAME)
            # Test the connection with a simple query
            await self._graph.query("RETURN 1")
            logger.info("Connected to FalkorDB graph '%s'", self.GRAPH_NAME)
            # Clean up garbage technology nodes from prior runs
            await self.cleanup_bad_technologies()
            return True
        except Exception:
            logger.warning("Could not connect to FalkorDB at %s:%d", self.host, self.port, exc_info=True)
            self._db = None
            self._graph = None
            return False

    async def cleanup_bad_technologies(self) -> None:
        """Remove rejected technology nodes from the graph."""
        if not self._graph:
            return
        for term in self._REJECTED_TECH:
            try:
                await self._graph.query(
                    "MATCH (t:Technology {name: $name}) DETACH DELETE t",
                    {"name": term},
                )
            except Exception:
                pass
        logger.info("Cleaned up rejected technology nodes from graph")

    async def init_indices(self) -> None:
        """Create indices for fast lookups."""
        if not self._graph:
            return
        indices = [
            "CREATE INDEX IF NOT EXISTS FOR (p:Program) ON (p.url)",
            "CREATE INDEX IF NOT EXISTS FOR (d:Domain) ON (d.name)",
            "CREATE INDEX IF NOT EXISTS FOR (t:Technology) ON (t.name)",
            "CREATE INDEX IF NOT EXISTS FOR (f:Finding) ON (f.id)",
            "CREATE INDEX IF NOT EXISTS FOR (v:VulnType) ON (v.name)",
            "CREATE INDEX IF NOT EXISTS FOR (s:Scan) ON (s.id)",
            "CREATE INDEX IF NOT EXISTS FOR (tc:Technique) ON (tc.name)",
            "CREATE INDEX IF NOT EXISTS FOR (e:Endpoint) ON (e.path)",
        ]
        for idx in indices:
            try:
                await self._graph.query(idx)
            except Exception:
                logger.debug("Index creation skipped: %s", idx, exc_info=True)

    async def close(self) -> None:
        """Close the FalkorDB connection. Leaves the container running."""
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                pass
            self._db = None
            self._graph = None
            logger.info("FalkorDB connection closed")

    @property
    def available(self) -> bool:
        return self._graph is not None

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    async def store_program(self, program) -> None:
        """MERGE a Program node and its Domain nodes with HAS_DOMAIN edges."""
        if not self._graph:
            return
        try:
            url = getattr(program, "url", "") or ""
            name = getattr(program, "name", "") or ""
            platform = getattr(program, "platform", "") or ""
            max_bounty = getattr(program, "max_bounty", 0) or 0
            domains = getattr(program, "all_domains", []) or []
            self._emit("store_program", name=name, platform=platform, domains=len(domains))

            await self._graph.query(
                "MERGE (p:Program {url: $url}) "
                "SET p.name = $name, p.platform = $platform, p.max_bounty = $max_bounty",
                {"url": url, "name": name, "platform": platform, "max_bounty": float(max_bounty)},
            )

            # Create domain nodes and edges
            domains = getattr(program, "all_domains", []) or []
            now = datetime.now(timezone.utc).isoformat()
            for domain in domains:
                await self._graph.query(
                    "MERGE (d:Domain {name: $domain}) "
                    "ON CREATE SET d.first_seen = $now "
                    "WITH d "
                    "MATCH (p:Program {url: $url}) "
                    "MERGE (p)-[:HAS_DOMAIN]->(d)",
                    {"domain": domain, "now": now, "url": url},
                )
        except Exception:
            logger.debug("Failed to store program in graph", exc_info=True)

    # Terms that are NOT server-side technologies — reject at storage level
    _REJECTED_TECH = frozenset({
        "android", "ios", "iphone", "ipad", "macos", "windows", "linux", "unix",
        "python", "java", "go", "golang", "rust", "c", "c++", "c#", "swift",
        "kotlin", "typescript", "javascript", "ruby", "perl", "php", "scala",
        "html", "css", "json", "xml", "yaml", "csv", "api", "web", "http", "https",
        "rest", "graphql", "grpc", "soap", "websocket", "tcp", "udp", "dns",
        "ssl", "tls", "oauth", "saml", "jwt", "sso",
        "microsoft entra id", "entra id", "active directory", "azure ad",
        "google", "facebook", "apple", "amazon", "microsoft",
        "seaport", "opensea", "metamask",
    })

    async def store_domain(self, domain: str) -> bool:
        """MERGE a Domain node. Returns True on success."""
        if not self._graph:
            return False
        try:
            self._emit("store_domain", domain=domain)
            await self._graph.query(
                "MERGE (d:Domain {name: $domain}) "
                "ON CREATE SET d.first_seen = timestamp()",
                {"domain": domain},
            )
            return True
        except Exception:
            logger.debug("Failed to store domain in graph", exc_info=True)
            return False

    async def store_technology(self, domain: str, tech: str) -> bool | None:
        """MERGE a Technology node and USES_TECH edge from Domain.

        Returns True on success, False on error, None if filtered/skipped.
        """
        if not self._graph:
            return None
        tech_lower = tech.lower().strip()
        if tech_lower in self._REJECTED_TECH or len(tech_lower) < 2:
            logger.debug("Rejected non-technology term: %s", tech)
            return None
        try:
            self._emit("store_technology", domain=domain, tech=tech)
            await self._graph.query(
                "MERGE (d:Domain {name: $domain}) "
                "MERGE (t:Technology {name: $tech}) "
                "MERGE (d)-[:USES_TECH]->(t)",
                {"domain": domain, "tech": tech},
            )
            return True
        except Exception:
            logger.debug("Failed to store technology in graph", exc_info=True)
            return False

    async def store_finding(
        self,
        finding_dict: dict,
        scan_id: int | None = None,
        domain: str | None = None,
    ) -> bool:
        """Create a Finding node with edges to Domain, VulnType, Scan. Returns True on success."""
        if not self._graph:
            return False
        try:
            self._emit(
                "store_vulnerability",
                title=finding_dict.get("title", "")[:60],
                vuln_type=finding_dict.get("vuln_type", ""),
                severity=finding_dict.get("severity", "info"),
                target=domain or finding_dict.get("target", ""),
                found_at=finding_dict.get("created_at", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")),
            )
            finding_id = finding_dict.get("id", 0)
            title = finding_dict.get("title", "")
            severity = finding_dict.get("severity", "info")
            confidence = finding_dict.get("confidence_score", finding_dict.get("confidence", 0.0))
            bounty = finding_dict.get("estimated_bounty", 0.0)
            vuln_type = finding_dict.get("vuln_type", "unknown")
            target = domain or finding_dict.get("target", "")
            created_at = finding_dict.get("created_at", datetime.now(timezone.utc).isoformat())
            technique = finding_dict.get("technique", "")
            endpoint_path = finding_dict.get("url", finding_dict.get("endpoint", ""))
            endpoint_method = finding_dict.get("method", "GET")

            # Create Finding node
            await self._graph.query(
                "CREATE (f:Finding {id: $id, title: $title, severity: $severity, "
                "confidence: $confidence, bounty: $bounty, accepted: false, "
                "created_at: $created_at})",
                {
                    "id": finding_id, "title": title, "severity": severity,
                    "confidence": float(confidence), "bounty": float(bounty),
                    "created_at": created_at,
                },
            )

            # Link to Domain
            if target:
                await self._graph.query(
                    "MERGE (d:Domain {name: $domain}) "
                    "WITH d "
                    "MATCH (f:Finding {id: $id, created_at: $created_at}) "
                    "MERGE (d)-[:HAS_FINDING]->(f)",
                    {"domain": target, "id": finding_id, "created_at": created_at},
                )

            # Link to VulnType
            if vuln_type:
                vt_name = vuln_type.replace("VulnType.", "")
                await self._graph.query(
                    "MERGE (v:VulnType {name: $vtype}) "
                    "WITH v "
                    "MATCH (f:Finding {id: $id, created_at: $created_at}) "
                    "MERGE (f)-[:IS_TYPE]->(v)",
                    {"vtype": vt_name, "id": finding_id, "created_at": created_at},
                )

            # Link to Technique (if provided)
            if technique:
                await self._graph.query(
                    "MERGE (tc:Technique {name: $technique}) "
                    "WITH tc "
                    "MATCH (f:Finding {id: $id, created_at: $created_at}) "
                    "MERGE (f)-[:USED_TECHNIQUE]->(tc)",
                    {"technique": technique, "id": finding_id, "created_at": created_at},
                )

            # Link to Endpoint (if URL provided)
            if endpoint_path and target:
                # Extract path from URL
                path = endpoint_path
                if "://" in path:
                    from urllib.parse import urlparse
                    parsed = urlparse(path)
                    path = parsed.path or "/"

                await self._graph.query(
                    "MERGE (e:Endpoint {path: $path, domain: $domain}) "
                    "ON CREATE SET e.method = $method "
                    "WITH e "
                    "MATCH (f:Finding {id: $id, created_at: $created_at}) "
                    "MERGE (f)-[:AT_ENDPOINT]->(e) "
                    "WITH e "
                    "MATCH (d:Domain {name: $domain}) "
                    "MERGE (d)-[:HAS_ENDPOINT]->(e)",
                    {
                        "path": path, "domain": target, "method": endpoint_method,
                        "id": finding_id, "created_at": created_at,
                    },
                )

            # Link to Scan
            if scan_id is not None:
                await self._graph.query(
                    "MERGE (s:Scan {id: $scan_id}) "
                    "WITH s "
                    "MATCH (f:Finding {id: $id, created_at: $created_at}) "
                    "MERGE (s)-[:FOUND]->(f)",
                    {"scan_id": scan_id, "id": finding_id, "created_at": created_at},
                )
            return True
        except Exception:
            logger.warning("Failed to store finding in graph", exc_info=True)
            return False

    async def store_scan(
        self, scan_id: int, domain: str, findings_count: int, cost: float,
    ) -> None:
        """MERGE a Scan node and SCANNED edge to Domain."""
        if not self._graph:
            return
        try:
            self._emit("store_scan", domain=domain, findings_count=findings_count, cost=cost)
            now = datetime.now(timezone.utc).isoformat()
            await self._graph.query(
                "MERGE (s:Scan {id: $scan_id}) "
                "SET s.started_at = $now, s.findings_count = $count, s.cost = $cost "
                "WITH s "
                "MERGE (d:Domain {name: $domain}) "
                "SET d.last_scanned = $now "
                "MERGE (s)-[:SCANNED]->(d)",
                {"scan_id": scan_id, "now": now, "count": findings_count,
                 "cost": float(cost), "domain": domain},
            )
        except Exception:
            logger.debug("Failed to store scan in graph", exc_info=True)

    async def store_endpoint(self, domain: str, path: str, method: str = "GET") -> bool:
        """MERGE an Endpoint node linked to a Domain. Returns True on success."""
        if not self._graph:
            return False
        try:
            self._emit("store_endpoint", domain=domain, path=path)
            await self._graph.query(
                "MERGE (d:Domain {name: $domain}) "
                "MERGE (e:Endpoint {path: $path, domain: $domain}) "
                "ON CREATE SET e.method = $method "
                "MERGE (d)-[:HAS_ENDPOINT]->(e)",
                {"domain": domain, "path": path, "method": method},
            )
            return True
        except Exception:
            logger.debug("Failed to store endpoint in graph", exc_info=True)
            return False

    async def store_technique(self, technique: str, vuln_type: str | None = None) -> bool:
        """MERGE a Technique node, optionally linked to a VulnType. Returns True on success."""
        if not self._graph:
            return False
        try:
            await self._graph.query(
                "MERGE (tc:Technique {name: $technique})",
                {"technique": technique},
            )
            return True
        except Exception:
            logger.debug("Failed to store technique in graph", exc_info=True)
            return False

    async def record_outcome(
        self, finding_id: int, accepted: bool, bounty: float = 0.0,
    ) -> None:
        """Update a Finding node with submission outcome."""
        if not self._graph:
            return
        try:
            self._emit("record_outcome", finding_id=finding_id, accepted=accepted, bounty=bounty)
            await self._graph.query(
                "MATCH (f:Finding {id: $id}) "
                "SET f.accepted = $accepted, f.bounty = $bounty",
                {"id": finding_id, "accepted": accepted, "bounty": float(bounty)},
            )
        except Exception:
            logger.debug("Failed to record outcome in graph", exc_info=True)

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    async def get_similar_targets(self, domain: str, limit: int = 10) -> list[dict]:
        """Find domains sharing 2+ technologies, return their finding history."""
        if not self._graph:
            return []
        try:
            self._emit("query_similar_targets", domain=domain)
            result = await self._graph.query(
                "MATCH (d:Domain {name: $domain})-[:USES_TECH]->(t:Technology)"
                "<-[:USES_TECH]-(other:Domain) "
                "WHERE other.name <> $domain "
                "WITH other, collect(t.name) AS shared_techs "
                "WHERE size(shared_techs) >= 2 "
                "OPTIONAL MATCH (other)-[:HAS_FINDING]->(f:Finding) "
                "OPTIONAL MATCH (f)-[:IS_TYPE]->(v:VulnType) "
                "RETURN other.name AS domain, shared_techs, "
                "collect(DISTINCT {title: f.title, severity: f.severity, "
                "vuln_type: v.name, accepted: f.accepted, bounty: f.bounty}) AS findings "
                "ORDER BY size(shared_techs) DESC "
                "LIMIT $limit",
                {"domain": domain, "limit": limit},
            )
            return self._result_to_dicts(result)
        except Exception:
            logger.debug("get_similar_targets query failed", exc_info=True)
            return []

    async def get_effective_techniques(
        self, tech_stack: list[str], limit: int = 10,
    ) -> list[dict]:
        """Vuln types with accepted findings on domains using these techs."""
        if not self._graph or not tech_stack:
            return []
        try:
            self._emit("query_effective_techniques", tech_stack=tech_stack[:5])
            result = await self._graph.query(
                "MATCH (d:Domain)-[:USES_TECH]->(t:Technology) "
                "WHERE t.name IN $techs "
                "WITH d, collect(DISTINCT t.name) AS matched_techs "
                "MATCH (d)-[:HAS_FINDING]->(f:Finding)-[:IS_TYPE]->(v:VulnType) "
                "WHERE f.accepted = true "
                "OPTIONAL MATCH (f)-[:USED_TECHNIQUE]->(tc:Technique) "
                "RETURN v.name AS vuln_type, "
                "count(f) AS accepted_count, "
                "avg(f.bounty) AS avg_bounty, "
                "collect(DISTINCT tc.name) AS techniques, "
                "collect(DISTINCT d.name) AS example_domains "
                "ORDER BY accepted_count DESC, avg_bounty DESC "
                "LIMIT $limit",
                {"techs": tech_stack, "limit": limit},
            )
            return self._result_to_dicts(result)
        except Exception:
            logger.debug("get_effective_techniques query failed", exc_info=True)
            return []

    async def get_domain_context(self, domain: str) -> dict:
        """Full context for a domain: techs, findings, scans, program, endpoints."""
        if not self._graph:
            return {}
        try:
            self._emit("query_domain_context", domain=domain)
            # Technologies
            tech_result = await self._graph.query(
                "MATCH (d:Domain {name: $domain})-[:USES_TECH]->(t:Technology) "
                "RETURN collect(t.name) AS technologies",
                {"domain": domain},
            )
            techs = []
            if tech_result.result_set:
                techs = tech_result.result_set[0][0] or []

            # Findings
            finding_result = await self._graph.query(
                "MATCH (d:Domain {name: $domain})-[:HAS_FINDING]->(f:Finding) "
                "OPTIONAL MATCH (f)-[:IS_TYPE]->(v:VulnType) "
                "OPTIONAL MATCH (f)-[:USED_TECHNIQUE]->(tc:Technique) "
                "RETURN f.title AS title, f.severity AS severity, "
                "v.name AS vuln_type, f.accepted AS accepted, "
                "f.bounty AS bounty, tc.name AS technique "
                "ORDER BY f.created_at DESC LIMIT 20",
                {"domain": domain},
            )
            findings = self._result_to_dicts(finding_result)

            # Scan count
            scan_result = await self._graph.query(
                "MATCH (s:Scan)-[:SCANNED]->(d:Domain {name: $domain}) "
                "RETURN count(s) AS scan_count, max(s.started_at) AS last_scan",
                {"domain": domain},
            )
            scan_info = self._result_to_dicts(scan_result)

            # Program
            prog_result = await self._graph.query(
                "MATCH (p:Program)-[:HAS_DOMAIN]->(d:Domain {name: $domain}) "
                "RETURN p.name AS program, p.platform AS platform, p.max_bounty AS max_bounty",
                {"domain": domain},
            )
            program = self._result_to_dicts(prog_result)

            # Endpoints
            ep_result = await self._graph.query(
                "MATCH (d:Domain {name: $domain})-[:HAS_ENDPOINT]->(e:Endpoint) "
                "OPTIONAL MATCH (f:Finding)-[:AT_ENDPOINT]->(e) "
                "RETURN e.path AS path, e.method AS method, "
                "count(f) AS finding_count "
                "ORDER BY finding_count DESC LIMIT 20",
                {"domain": domain},
            )
            endpoints = self._result_to_dicts(ep_result)

            return {
                "domain": domain,
                "technologies": techs,
                "findings": findings,
                "scan_info": scan_info[0] if scan_info else {},
                "program": program[0] if program else {},
                "endpoints": endpoints,
            }
        except Exception:
            logger.debug("get_domain_context query failed", exc_info=True)
            return {"domain": domain}

    async def get_vulnerable_endpoints(self, tech_stack: list[str], limit: int = 20) -> list[dict]:
        """Find endpoints that had findings on domains with similar tech stacks."""
        if not self._graph or not tech_stack:
            return []
        try:
            self._emit("query_vulnerable_endpoints", tech_stack=tech_stack[:5])
            result = await self._graph.query(
                "MATCH (d:Domain)-[:USES_TECH]->(t:Technology) "
                "WHERE t.name IN $techs "
                "WITH d "
                "MATCH (d)-[:HAS_ENDPOINT]->(e:Endpoint)<-[:AT_ENDPOINT]-(f:Finding) "
                "OPTIONAL MATCH (f)-[:IS_TYPE]->(v:VulnType) "
                "RETURN e.path AS path, e.method AS method, "
                "v.name AS vuln_type, f.severity AS severity, "
                "d.name AS domain, f.accepted AS accepted "
                "ORDER BY f.accepted DESC "
                "LIMIT $limit",
                {"techs": tech_stack, "limit": limit},
            )
            return self._result_to_dicts(result)
        except Exception:
            logger.debug("get_vulnerable_endpoints query failed", exc_info=True)
            return []

    async def get_chain_patterns(
        self, vuln_types: list[str] | None = None, limit: int = 10,
    ) -> list[dict]:
        """Find historical vuln type co-occurrences on the same domain.

        Returns patterns like: "On domains with tech X, cors_misconfiguration + secret_exposure
        appeared together and were accepted."  Useful for informing attack chain synthesis.
        """
        if not self._graph:
            return []
        try:
            self._emit("query_chain_patterns", vuln_types=vuln_types)

            # Find pairs of vuln types that co-occurred on the same domain
            result = await self._graph.query(
                "MATCH (d:Domain)-[:HAS_FINDING]->(f1:Finding)-[:IS_TYPE]->(v1:VulnType), "
                "(d)-[:HAS_FINDING]->(f2:Finding)-[:IS_TYPE]->(v2:VulnType) "
                "WHERE id(f1) < id(f2) "
                "OPTIONAL MATCH (d)-[:USES_TECH]->(t:Technology) "
                "OPTIONAL MATCH (f1)-[:USED_TECHNIQUE]->(tc1:Technique) "
                "OPTIONAL MATCH (f2)-[:USED_TECHNIQUE]->(tc2:Technique) "
                "WITH v1.name AS vuln1, v2.name AS vuln2, "
                "collect(DISTINCT d.name) AS domains, "
                "collect(DISTINCT t.name) AS tech_stack, "
                "collect(DISTINCT tc1.name) + collect(DISTINCT tc2.name) AS techniques, "
                "count(DISTINCT d) AS co_occurrence_count, "
                "sum(CASE WHEN f1.accepted = true THEN 1 ELSE 0 END) + "
                "sum(CASE WHEN f2.accepted = true THEN 1 ELSE 0 END) AS accepted_count, "
                "avg(f1.bounty) + avg(f2.bounty) AS combined_bounty "
                "RETURN vuln1, vuln2, co_occurrence_count, accepted_count, "
                "combined_bounty, domains[0..3] AS example_domains, "
                "tech_stack[0..5] AS common_tech, "
                "techniques[0..3] AS techniques_used "
                "ORDER BY co_occurrence_count DESC, accepted_count DESC "
                "LIMIT $limit",
                {"limit": limit},
            )
            return self._result_to_dicts(result)
        except Exception:
            logger.debug("get_chain_patterns query failed", exc_info=True)
            return []

    async def get_findings_by_tech(
        self, tech_stack: list[str], limit: int = 20,
    ) -> list[dict]:
        """Get historical findings on domains with a matching tech stack.

        Useful for chain synthesis — shows what vuln types have been found
        on similar infrastructure.
        """
        if not self._graph or not tech_stack:
            return []
        try:
            self._emit("query_findings_by_tech", tech_stack=tech_stack[:5])
            result = await self._graph.query(
                "MATCH (d:Domain)-[:USES_TECH]->(t:Technology) "
                "WHERE t.name IN $techs "
                "WITH d, collect(DISTINCT t.name) AS matched_techs "
                "WHERE size(matched_techs) >= 1 "
                "MATCH (d)-[:HAS_FINDING]->(f:Finding)-[:IS_TYPE]->(v:VulnType) "
                "OPTIONAL MATCH (f)-[:AT_ENDPOINT]->(e:Endpoint) "
                "RETURN d.name AS domain, v.name AS vuln_type, "
                "f.severity AS severity, f.accepted AS accepted, "
                "f.bounty AS bounty, e.path AS endpoint, "
                "matched_techs "
                "ORDER BY f.accepted DESC, f.bounty DESC "
                "LIMIT $limit",
                {"techs": tech_stack, "limit": limit},
            )
            return self._result_to_dicts(result)
        except Exception:
            logger.debug("get_findings_by_tech query failed", exc_info=True)
            return []

    async def build_chain_context(self, domains: list[str]) -> str:
        """Build a context string specifically for chain synthesis.

        Queries the graph for:
        1. Historical vuln type co-occurrences (chain patterns)
        2. Findings on similar tech stacks
        3. Known effective attack chains from prior scans

        Returns formatted text to inject into the chain synthesis prompt.
        """
        if not self._graph:
            return ""

        parts = []

        # 1. Historical chain patterns
        patterns = await self.get_chain_patterns(limit=8)
        if patterns:
            parts.append("## Historical Chain Patterns (from prior scans)")
            for p in patterns:
                v1 = p.get("vuln1", "?")
                v2 = p.get("vuln2", "?")
                count = p.get("co_occurrence_count", 0)
                accepted = p.get("accepted_count", 0)
                bounty = p.get("combined_bounty", 0)
                domains_ex = p.get("example_domains", [])
                tech = p.get("common_tech", [])
                domains_str = ", ".join(d for d in domains_ex if d) if domains_ex else "various"
                tech_str = ", ".join(t for t in tech if t) if tech else ""
                parts.append(
                    f"- {v1} + {v2}: seen together {count}x, "
                    f"{accepted} accepted, ~${bounty:.0f} combined bounty"
                    + (f" (on {domains_str})" if domains_str != "various" else "")
                    + (f" [tech: {tech_str}]" if tech_str else "")
                )

        # 2. Gather tech stacks for the current domains
        all_techs = set()
        for domain in domains[:5]:
            ctx = await self.get_domain_context(domain)
            for t in ctx.get("technologies", []):
                all_techs.add(t)

        # 3. Findings on similar tech stacks
        if all_techs:
            similar_findings = await self.get_findings_by_tech(list(all_techs), limit=15)
            if similar_findings:
                parts.append("\n## Findings on Similar Tech Stacks")
                for sf in similar_findings:
                    status = "accepted" if sf.get("accepted") else "reported"
                    parts.append(
                        f"- {sf.get('vuln_type', '?')} [{sf.get('severity', '?')}] "
                        f"on {sf.get('domain', '?')} ({status}, ${sf.get('bounty', 0):.0f})"
                        + (f" at {sf['endpoint']}" if sf.get("endpoint") else "")
                    )

        if not parts:
            return ""

        return (
            "\n=== GRAPH MEMORY: Chain Intelligence ===\n"
            + "\n".join(parts)
            + "\n=== END GRAPH MEMORY ==="
        )

    async def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Run a raw Cypher query. Returns list of dicts."""
        if not self._graph:
            return []
        try:
            # Classify as write or read for dashboard
            cypher_upper = cypher.strip().upper()
            is_write = any(kw in cypher_upper for kw in ("MERGE", "CREATE", "SET", "DELETE"))
            op_type = "store_raw" if is_write else "query_raw"
            self._emit(op_type, cypher=cypher[:80])
            result = await self._graph.query(cypher, params or {})
            return self._result_to_dicts(result)
        except Exception as exc:
            logger.debug("Raw Cypher query failed: %s", exc)
            return [{"error": str(exc)}]

    # ------------------------------------------------------------------
    # Bootstrap: import existing SQLite data
    # ------------------------------------------------------------------

    async def sync_from_sqlite(self, store) -> dict:
        """One-time import of existing PersistentMemory data into graph.

        Args:
            store: PersistentMemory instance

        Returns:
            Summary dict with counts of imported items.
        """
        if not self._graph:
            return {"error": "Graph not connected"}

        counts = {"programs": 0, "scans": 0, "findings": 0}

        # Import programs
        programs = store.get_programs()
        for p in programs:
            data = json.loads(p.get("data", "{}")) if isinstance(p.get("data"), str) else p.get("data", {})
            url = p.get("url", "")
            name = p.get("name", "")
            platform = p.get("platform", "")

            try:
                await self._graph.query(
                    "MERGE (prog:Program {url: $url}) "
                    "SET prog.name = $name, prog.platform = $platform",
                    {"url": url, "name": name, "platform": platform},
                )
                counts["programs"] += 1

                # Import domains from the data blob
                domains = data.get("scope_domains", []) + data.get("scope_wildcards", [])
                for domain in domains:
                    await self._graph.query(
                        "MERGE (d:Domain {name: $domain}) "
                        "WITH d "
                        "MATCH (prog:Program {url: $url}) "
                        "MERGE (prog)-[:HAS_DOMAIN]->(d)",
                        {"domain": domain, "url": url},
                    )
            except Exception:
                logger.debug("Failed to import program %s", name, exc_info=True)

        # Import scans
        scans = store.get_past_scans(limit=500)
        for s in scans:
            scan_id = s.get("id", 0)
            target = s.get("target", "")
            started_at = s.get("started_at", "")
            findings_count = s.get("findings_count", 0)
            cost = s.get("cost", 0.0)

            try:
                for domain in target.split(","):
                    domain = domain.strip()
                    if domain:
                        await self._graph.query(
                            "MERGE (s:Scan {id: $scan_id}) "
                            "SET s.started_at = $started_at, s.findings_count = $count, s.cost = $cost "
                            "WITH s "
                            "MERGE (d:Domain {name: $domain}) "
                            "MERGE (s)-[:SCANNED]->(d)",
                            {"scan_id": scan_id, "started_at": started_at,
                             "count": findings_count, "cost": float(cost), "domain": domain},
                        )
                counts["scans"] += 1
            except Exception:
                logger.debug("Failed to import scan %d", scan_id, exc_info=True)

        # Import findings
        findings = store.get_past_findings(limit=1000)
        for f in findings:
            try:
                finding_dict = {
                    "id": f.get("id", 0),
                    "title": f.get("title", ""),
                    "severity": f.get("severity", "info"),
                    "confidence_score": f.get("confidence", 0.0),
                    "estimated_bounty": f.get("estimated_bounty", 0.0),
                    "vuln_type": f.get("vuln_type", ""),
                    "target": f.get("target", ""),
                    "created_at": f.get("created_at", ""),
                }
                await self.store_finding(finding_dict, scan_id=f.get("scan_id"))

                # Import outcome if available
                if f.get("was_accepted"):
                    await self.record_outcome(
                        f["id"], accepted=True,
                        bounty=f.get("actual_bounty", 0.0),
                    )
                counts["findings"] += 1
            except Exception:
                logger.debug("Failed to import finding %s", f.get("id"), exc_info=True)

        logger.info(
            "Graph sync complete: %d programs, %d scans, %d findings",
            counts["programs"], counts["scans"], counts["findings"],
        )
        return counts

    # ------------------------------------------------------------------
    # Context builder (for scanner prompt injection)
    # ------------------------------------------------------------------

    async def build_scan_context(self, domains: list[str], techs: list[str] | None = None) -> str:
        """Build a context string for scanner prompts with graph intelligence.

        Returns a formatted string like:
          === GRAPH MEMORY ===
          Similar targets: ...
          Effective techniques: ...
          === END GRAPH MEMORY ===
        """
        if not self._graph:
            return ""

        parts = []

        # Similar targets for each domain
        for domain in domains[:5]:  # limit to 5 to keep context small
            similar = await self.get_similar_targets(domain, limit=3)
            if similar:
                for s in similar:
                    shared = ", ".join(s.get("shared_techs", [])[:5])
                    findings_summary = []
                    for f in (s.get("findings") or [])[:3]:
                        if isinstance(f, dict) and f.get("vuln_type"):
                            status = "accepted" if f.get("accepted") else "reported"
                            findings_summary.append(f"{f['vuln_type']} ({status})")
                    findings_str = ", ".join(findings_summary) if findings_summary else "no findings"
                    parts.append(
                        f"Similar target: {s.get('domain', '?')} "
                        f"(shared: {shared}) — {findings_str}"
                    )

        # Effective techniques for tech stack
        all_techs = techs or []
        if not all_techs:
            # Gather techs from graph for these domains
            for domain in domains[:5]:
                ctx = await self.get_domain_context(domain)
                all_techs.extend(ctx.get("technologies", []))
            all_techs = list(set(all_techs))

        if all_techs:
            effective = await self.get_effective_techniques(all_techs, limit=5)
            for e in effective:
                vtype = e.get("vuln_type", "?")
                count = e.get("accepted_count", 0)
                avg_b = e.get("avg_bounty", 0)
                techniques = ", ".join(e.get("techniques", [])[:3]) or "various"
                parts.append(
                    f"Effective: {vtype} ({count} accepted, avg ${avg_b:.0f}) "
                    f"via {techniques}"
                )

        # Vulnerable endpoints
        if all_techs:
            vuln_eps = await self.get_vulnerable_endpoints(all_techs, limit=5)
            for ep in vuln_eps:
                parts.append(
                    f"Vulnerable endpoint pattern: {ep.get('method', 'GET')} {ep.get('path', '/')} "
                    f"— {ep.get('vuln_type', '?')} on {ep.get('domain', '?')}"
                )

        if not parts:
            return ""

        return (
            "\n=== GRAPH MEMORY (cross-session intelligence) ===\n"
            + "\n".join(parts)
            + "\n=== END GRAPH MEMORY ==="
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_to_dicts(result) -> list[dict]:
        """Convert a FalkorDB query result to a list of dicts."""
        if not result or not result.result_set:
            return []
        header = result.header
        rows = []
        for row in result.result_set:
            d = {}
            for i, col_name in enumerate(header):
                # header is list of [type, name] or just names
                name = col_name[1] if isinstance(col_name, (list, tuple)) else col_name
                val = row[i]
                # Convert FalkorDB Node/Edge to plain dicts
                if hasattr(val, "properties"):
                    val = val.properties
                d[name] = val
            rows.append(d)
        return rows
