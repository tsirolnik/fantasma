"""Recorder — LLM-powered background agent that extracts intelligence from SharedMemory
and persists structured data to the FalkorDB knowledge graph.

Pentesters focus on hacking. The recorder watches their discoveries (posted to
SharedMemory), uses an LLM to extract technologies, endpoints, techniques, and
other intelligence, then stores it in the graph for cross-session learning.

Visible in the dashboard event stream as [recorder] events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

if TYPE_CHECKING:
    from llm.base import LLMProvider
    from memory.graph import GraphMemory
    from memory.shared import SharedMemory

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """\
You are a security intelligence analyst. Extract structured data from these \
pentester discoveries for our knowledge graph.

For each entry, extract ONLY the following if clearly present:
- technologies: SERVER-SIDE software/frameworks ONLY (e.g. nginx, apache, express, django, \
flask, spring, laravel, rails, tomcat, redis, mongodb, mysql, postgres, elasticsearch). \
Also CDNs (cloudflare, akamai, fastly) and frontend frameworks (react, angular, vue, jquery). \
Do NOT include: operating systems (linux, windows, android, ios), programming languages \
(python, java, go), generic terms (api, web, html, css, json), or mobile platforms.
- endpoints: API paths or URLs with security relevance (e.g. /api/v1/users, /admin, /.env, \
/graphql, /actuator). Skip static assets, marketing pages, and generic paths.
- techniques: specific attack techniques used or noted (e.g. parameter pollution, null origin \
CORS bypass, JWT none algorithm, NoSQL operator injection). NOT tool names.
- subdomains: new domains or subdomains discovered
- domain: which target domain this relates to

Respond ONLY with a JSON array. Each element should have:
{
  "domain": "example.com",
  "technologies": ["nginx", "react"],
  "endpoints": [{"path": "/api/foo", "method": "GET"}],
  "techniques": [{"name": "technique name", "vuln_type": "xss"}],
  "subdomains": ["sub.example.com"]
}

Be STRICT: skip entries with no clearly extractable data. Empty array [] is fine. \
Quality over quantity — only extract what is certain.

=== DISCOVERIES ===
"""


class Recorder:
    """LLM-powered recorder that watches SharedMemory and persists to the knowledge graph."""

    def __init__(
        self,
        shared_memory: SharedMemory,
        graph: GraphMemory,
        llm: LLMProvider | None = None,
        broadcast: Callable[[dict], None] | None = None,
        cost_tracker: Any = None,
    ) -> None:
        self.shared_memory = shared_memory
        self.graph = graph
        self.llm = llm
        self._broadcast = broadcast  # bridge._broadcast for dashboard events
        self.cost_tracker = cost_tracker
        self._last_id = 0
        self._stored_count = 0
        self._llm_calls = 0
        self._llm_empty = 0  # LLM calls that returned no extractable data
        self._graph_errors = 0  # graph store failures
        self._processed = 0  # entries sent to LLM (as opposed to still pending)
        self._extracting = False  # True while an LLM batch is in progress
        self._task: asyncio.Task | None = None
        self._batch: list = []  # accumulate entries between LLM calls
        self._batch_size = 15  # entries per LLM call
        self._batch_interval = 10  # seconds between LLM batches
        self._poll_interval = 3  # seconds between SharedMemory polls

    @property
    def stored_count(self) -> int:
        return self._stored_count

    @property
    def llm_calls(self) -> int:
        return self._llm_calls

    def start(self) -> None:
        """Start the recorder as a background task."""
        self._task = asyncio.ensure_future(self._run())
        mode = "LLM-powered" if self.llm else "rule-based"
        logger.info("Recorder started (%s) — watching SharedMemory feed", mode)
        self._emit_event("started", f"Recorder active ({mode})")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        """Main loop — poll SharedMemory, batch entries, periodically run LLM extraction."""
        last_llm_time = time.monotonic()
        try:
            while True:
                await asyncio.sleep(self._poll_interval)

                # Collect new entries
                entries = self.shared_memory.read_since(entry_id=self._last_id, limit=100)
                for entry in entries:
                    self._last_id = entry.id
                    # Always do fast rule-based extraction
                    await self._rule_based_extract(entry)
                    # Accumulate for LLM batch
                    if self.llm and entry.category not in ("credential",):
                        self._batch.append(entry)

                # Periodically run LLM extraction — drain all pending batches
                elapsed = time.monotonic() - last_llm_time
                if self.llm and self._batch and elapsed >= self._batch_interval:
                    while self._batch:
                        await self._llm_extract_batch()
                    last_llm_time = time.monotonic()

        except asyncio.CancelledError:
            # Final flush
            if self._batch and self.llm:
                await self._llm_extract_batch()
            logger.info(
                "Recorder stopped — stored %d items, %d LLM calls",
                self._stored_count, self._llm_calls,
            )
            self._emit_event("stopped", f"Stored {self._stored_count} items to graph ({self._llm_calls} LLM calls)")

    # ------------------------------------------------------------------
    # Rule-based extraction (fast, no LLM cost)
    # ------------------------------------------------------------------

    async def _rule_based_extract(self, entry) -> None:
        """Fast extraction from structured SharedMemory entries."""
        category = entry.category
        content = entry.content or ""

        data = {}
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            data = {"raw": content}

        try:
            if category == "subdomain":
                domain = data.get("domain") or data.get("subdomain") or ""
                if not domain and ":" in entry.key:
                    domain = entry.key.split(":", 1)[1]
                if domain:
                    if await self.graph.store_domain(domain):
                        self._stored_count += 1

            elif category == "technology":
                tech = data.get("technology") or data.get("tech") or data.get("name") or ""
                domain = data.get("domain") or data.get("target") or ""
                if not tech and ":" in entry.key:
                    parts = entry.key.split(":")
                    tech = parts[1] if len(parts) >= 2 else ""
                    domain = parts[2] if len(parts) >= 3 else domain
                if tech and domain:
                    result = await self.graph.store_technology(domain, tech.lower().strip())
                    if result is True:
                        self._stored_count += 1

            elif category == "endpoint":
                url = data.get("url") or data.get("endpoint") or data.get("path") or ""
                domain = data.get("domain") or data.get("target") or ""
                method = data.get("method", "GET")
                if not url and ":" in entry.key:
                    url = entry.key.split(":", 1)[1]
                if url:
                    if "://" in url:
                        parsed = urlparse(url)
                        domain = domain or parsed.hostname or ""
                        path = parsed.path or "/"
                    else:
                        path = url
                    if domain and path:
                        if await self.graph.store_endpoint(domain, path, method):
                            self._stored_count += 1

        except Exception:
            logger.debug("Recorder rule-based: failed on entry %d", entry.id, exc_info=True)

    # ------------------------------------------------------------------
    # LLM-powered batch extraction
    # ------------------------------------------------------------------

    async def _llm_extract_batch(self) -> None:
        """Use the LLM to extract structured intelligence from a batch of entries."""
        if not self._batch or not self.llm:
            return

        batch = self._batch[:self._batch_size]
        self._batch = self._batch[self._batch_size:]
        self._processed += len(batch)

        entries_text = []
        for entry in batch:
            content = entry.content or ""
            if len(content) > 500:
                content = content[:500] + "..."
            entries_text.append(
                f"[{entry.category}] agent={entry.agent_id} key={entry.key}\n{content}"
            )

        prompt = EXTRACT_PROMPT + "\n---\n".join(entries_text)

        try:
            self._extracting = True
            self._llm_calls += 1
            response = await self.llm.analyze(prompt)

            # Track cost
            if self.cost_tracker and self.llm:
                # Rough token estimate: ~4 chars per token
                in_tok = len(prompt) // 4
                out_tok = len(response) // 4
                cost = self.llm.estimate_cost(in_tok, out_tok)
                self.cost_tracker.add_cost("recorder_llm", cost, detail=f"recorder batch ({len(batch)} entries)")

            # Parse JSON response
            extracted = self._parse_llm_response(response)
            if not extracted:
                self._llm_empty += 1
                logger.info("Recorder: LLM returned no extractable data from %d entries (response: %s)",
                            len(batch), response[:200])
                return

            stored_this_batch = 0
            logger.info("Recorder: LLM extracted %d items: %s",
                        len(extracted), json.dumps(extracted, default=str)[:500])
            for item in extracted:
                domain = item.get("domain", "")
                if not domain:
                    continue

                # Store technologies
                for tech in item.get("technologies", []):
                    if isinstance(tech, str) and tech:
                        result = await self.graph.store_technology(domain, tech.lower().strip())
                        if result is True:
                            self._stored_count += 1
                            stored_this_batch += 1
                        elif result is False:
                            self._graph_errors += 1
                            logger.info("Recorder: graph FAILED storing tech '%s' for %s", tech, domain)
                        else:
                            logger.info("Recorder: filtered tech '%s' for %s", tech, domain)

                # Store endpoints
                for ep in item.get("endpoints", []):
                    if isinstance(ep, dict):
                        path = ep.get("path", "")
                        method = ep.get("method", "GET")
                    elif isinstance(ep, str):
                        path = ep
                        method = "GET"
                    else:
                        continue
                    if path:
                        if await self.graph.store_endpoint(domain, path, method):
                            self._stored_count += 1
                            stored_this_batch += 1
                        else:
                            self._graph_errors += 1

                # Store techniques
                for tech in item.get("techniques", []):
                    if isinstance(tech, dict):
                        name = tech.get("name", "")
                        vuln_type = tech.get("vuln_type", "")
                    elif isinstance(tech, str):
                        name = tech
                        vuln_type = ""
                    else:
                        continue
                    if name:
                        if await self.graph.store_technique(name, vuln_type):
                            self._stored_count += 1
                            stored_this_batch += 1
                        else:
                            self._graph_errors += 1

                # Store subdomains
                for sub in item.get("subdomains", []):
                    if isinstance(sub, str) and sub:
                        if await self.graph.store_domain(sub):
                            self._stored_count += 1
                            stored_this_batch += 1
                        else:
                            self._graph_errors += 1

            if stored_this_batch:
                self._emit_event(
                    "extracted",
                    f"Extracted {stored_this_batch} items from {len(batch)} entries "
                    f"(total: {self._stored_count})",
                )
                logger.info(
                    "Recorder LLM batch: extracted %d items from %d entries",
                    stored_this_batch, len(batch),
                )

        except Exception:
            logger.warning("Recorder LLM extraction failed", exc_info=True)
        finally:
            self._extracting = False

    def _parse_llm_response(self, response: str) -> list[dict]:
        """Parse JSON array from LLM response, handling markdown fences."""
        text = response.strip()
        # Strip markdown code fences
        if "```" in text:
            match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return [result]
        except json.JSONDecodeError:
            # Try to find a JSON array in the response
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        logger.warning("Recorder: could not parse LLM response: %s", text[:300])
        return []

    # ------------------------------------------------------------------
    # Dashboard events
    # ------------------------------------------------------------------

    def _emit_event(self, event_type: str, content: str) -> None:
        """Broadcast a recorder event to the dashboard."""
        if not self._broadcast:
            return
        try:
            from agents.messaging import AgentMessage
            msg = AgentMessage(
                sender="recorder",
                recipient="dashboard",
                msg_type="recorder",
                content=f"[{event_type}] {content}",
                data={
                    "stored_count": self._stored_count,
                    "llm_calls": self._llm_calls,
                },
            )
            self._broadcast({
                "type": "agent_message",
                "id": msg.id,
                "sender": msg.sender,
                "recipient": msg.recipient,
                "msg_type": msg.msg_type,
                "content": msg.content,
                "priority": msg.priority,
                "timestamp": msg.timestamp.isoformat(),
                "data": msg.data,
            })
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Return recorder stats."""
        return {
            "stored_count": self._stored_count,
            "llm_calls": self._llm_calls,
            "llm_empty": self._llm_empty,
            "graph_errors": self._graph_errors,
            "processed": self._processed,
            "extracting": self._extracting,
            "batch_size": self._batch_size,
            "last_entry_id": self._last_id,
            "pending_batch": len(self._batch),
        }
