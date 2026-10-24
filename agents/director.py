"""Director Agent — Research Lead that oversees the pipeline using LLM reasoning."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from llm.base import LLMProvider
from models.program import BountyProgram

from agents.messaging import (
    AgentMessage,
    DirectorDecision,
    MessageBus,
    TaskResult,
)

logger = logging.getLogger(__name__)


class Director:
    """Strategic overseer that evaluates worker results and makes decisions.

    Uses a capable LLM (e.g. Opus) to read worker reflections, concerns,
    observations, and inter-worker consultations — then makes informed
    strategic decisions about how to proceed.
    """

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        dry_run: bool = False,
        fallback_llm: LLMProvider | None = None,
    ) -> None:
        self.bus = bus
        self.llm = llm
        self.dry_run = dry_run
        self._fallback_llm = fallback_llm
        self.name = "director"
        self._pending_directives: list[str] = []
        bus.register_agent(self.name, self)
        bus.subscribe("directive", self._on_directive)

    # ------------------------------------------------------------------
    # Phase evaluation
    # ------------------------------------------------------------------

    async def evaluate_phase_result(
        self,
        task_result: TaskResult,
        phase: str,
        *,
        budget_remaining: float | None = None,
        learning_insights: str = "",
    ) -> DirectorDecision:
        """Evaluate a worker's phase result and decide how to proceed."""
        if self.dry_run or self.llm is None:
            decision = self._default_decision(task_result, phase)
            self.bus.send(
                AgentMessage(
                    sender=self.name,
                    recipient="all",
                    msg_type="decision",
                    content=decision.reasoning,
                    data={"decision": decision.decision, "phase": phase},
                )
            )
            return decision

        # Gather all messages from this phase
        concerns = self.bus.get_log(agent=task_result.agent_name, msg_type="concern")
        observations = self.bus.get_log(agent=task_result.agent_name, msg_type="observation")
        consultations = [
            m for m in self.bus.get_log(msg_type="question")
            if m.sender == task_result.agent_name or m.recipient == task_result.agent_name
        ]
        answers = [
            m for m in self.bus.get_log(msg_type="answer")
            if m.sender == task_result.agent_name or m.recipient == task_result.agent_name
        ]

        prompt = self._build_evaluation_prompt(
            task_result=task_result,
            phase=phase,
            concerns=concerns,
            observations=observations,
            consultations=consultations + answers,
            budget_remaining=budget_remaining,
            learning_insights=learning_insights,
        )

        try:
            response = await self.llm.analyze(prompt)
            decision = self._parse_decision(response)
        except Exception:
            if self._fallback_llm is not None and self._fallback_llm is not self.llm:
                try:
                    response = await self._fallback_llm.analyze(prompt)
                    decision = self._parse_decision(response)
                except Exception:
                    decision = self._default_decision(task_result, phase)
            else:
                logger.exception("Director LLM evaluation failed, using default")
                decision = self._default_decision(task_result, phase)

        # Broadcast the decision
        self.bus.send(
            AgentMessage(
                sender=self.name,
                recipient="all",
                msg_type="decision",
                content=decision.reasoning,
                data={"decision": decision.decision, "phase": phase, **decision.parameters},
            )
        )

        return decision

    # ------------------------------------------------------------------
    # Program selection
    # ------------------------------------------------------------------

    async def decide_programs(
        self,
        programs: list[BountyProgram],
        discover_reflection: str,
        top_n: int = 5,
    ) -> list[BountyProgram]:
        """Select which programs to target based on discovery results."""
        if self.dry_run or self.llm is None or not programs:
            return programs[:top_n]

        summaries = []
        for i, p in enumerate(programs[:top_n * 2]):
            parts = [
                f"{i+1}. {p.name} ({p.platform}): "
                f"bounty=${p.min_bounty}-${p.max_bounty}, "
                f"ROI={p.roi_score:.2f}, "
                f"response_time={p.response_time_days:.0f}d, "
                f"scope={len(p.all_domains)} domains"
            ]
            if p.out_of_scope:
                parts.append(f"   out_of_scope: {len(p.out_of_scope)} entries")
            if p.scope_domains[:5]:
                parts.append(f"   domains: {', '.join(p.scope_domains[:5])}")
            if p.scope_wildcards[:3]:
                parts.append(f"   wildcards: {', '.join(p.scope_wildcards[:3])}")
            summaries.append("\n".join(parts))

        prompt = (
            "You are the Director (Research Lead) of an authorized bug bounty "
            "security research team. Your goal is to select targets where your team "
            "of security researchers can find exploitable vulnerabilities and earn bounties.\n\n"
            f"The discover worker analyzed programs and reports:\n"
            f'"{discover_reflection}"\n\n'
            f"Available programs (pre-ranked by ROI):\n"
            + "\n".join(summaries) + "\n\n"
            f"Select the best {top_n} programs to target. Consider:\n"
            "- BOUNTY AMOUNT is the #1 factor — high-paying programs ($5K+) are worth more than large-scope low-paying ones\n"
            "- ROI score (already factors in bounty, scope, and recency — higher = better)\n"
            "- Actual domains listed (look for interesting web apps, APIs, SaaS — not just marketing sites)\n"
            "- Wildcard scopes (*.domain.com = many potential subdomains to discover)\n"
            "- Platform reputation (HackerOne/Bugcrowd programs tend to pay more reliably than smaller platforms)\n"
            "- Response time (faster = quicker payouts)\n"
            "- AVOID programs with tiny bounties (<$500 max), obscure platforms, or no interesting domains\n\n"
            f"Respond with:\n"
            f"SELECTED: <comma-separated program numbers, e.g. 1,3,5>\n"
            f"REASONING: <your strategic reasoning>"
        )

        try:
            response = await self.llm.analyze(prompt)
            selected_indices = self._parse_selection(response, len(programs))
            selected = [programs[i] for i in selected_indices][:top_n]
            if not selected:
                selected = programs[:top_n]
        except Exception:
            # Try fallback LLM if primary fails (e.g. no Claude API key)
            if self._fallback_llm is not None and self._fallback_llm is not self.llm:
                try:
                    logger.warning("Director primary LLM failed, trying fallback")
                    response = await self._fallback_llm.analyze(prompt)
                    selected_indices = self._parse_selection(response, len(programs))
                    selected = [programs[i] for i in selected_indices][:top_n]
                    if not selected:
                        selected = programs[:top_n]
                except Exception:
                    logger.exception("Director fallback LLM also failed, using default ranking")
                    selected = programs[:top_n]
            else:
                logger.exception("Director program selection failed, using default ranking")
                selected = programs[:top_n]

        self.bus.send(
            AgentMessage(
                sender=self.name,
                recipient="all",
                msg_type="decision",
                content=(
                    f"Selected {len(selected)} programs: "
                    + ", ".join(p.name for p in selected)
                ),
                data={"selected_programs": [p.name for p in selected]},
            )
        )

        return selected

    # ------------------------------------------------------------------
    # Swarm active supervision
    # ------------------------------------------------------------------

    async def review_swarm_progress(
        self,
        shared_memory_summary: dict[str, Any],
        agent_activity: dict[str, dict[str, Any]],
        elapsed_minutes: float,
        budget_spent: float,
        budget_total: float,
        findings_count: int,
        cost_tracker: Any = None,
        shared_memory: Any = None,
        findings: list | None = None,
    ) -> list[dict[str, str]]:
        """Review swarm agent progress and return strategic redirects.

        Args:
            shared_memory_summary: Output of SharedMemory.summary()
            agent_activity: Per-agent activity dict:
                {agent_id: {"recent_calls": [...], "claims": [...], "findings": N}}
            elapsed_minutes: Minutes since swarm started
            budget_spent: Total budget spent so far
            budget_total: Total budget available
            findings_count: Total deduplicated findings
            cost_tracker: Optional CostTracker for recording review cost
            findings: Actual VulnFinding objects for chain analysis

        Returns:
            List of {"agent": agent_id, "instruction": redirect_text}
        """
        if self.dry_run or self.llm is None:
            return [], [], ""

        # Build per-agent activity summaries
        agent_sections = []
        for agent_id, activity in agent_activity.items():
            recent = activity.get("recent_calls", [])
            claims = activity.get("claims", [])
            agent_findings = activity.get("findings", 0)
            iterations = activity.get("iterations", 0)

            calls_text = "\n".join(
                f"    - {c}" for c in recent[-10:]
            ) if recent else "    (no recent calls)"

            # Include current score if available
            score_str = ""
            if shared_memory:
                score_obj = shared_memory.get_score(agent_id)
                score_str = f", Score: {score_obj.total}"

            agent_sections.append(
                f"### {agent_id}\n"
                f"  Iterations: {iterations}, Findings submitted: {agent_findings}{score_str}\n"
                f"  Claimed targets: {', '.join(claims) if claims else 'none'}\n"
                f"  Recent tool calls:\n{calls_text}"
            )

        # Build findings section for chain awareness
        findings_section = ""
        if findings:
            finding_lines = []
            for i, f in enumerate(findings):
                finding_lines.append(
                    f"  [{i}] [{f.severity.value.upper()}] {f.vuln_type.value} on {f.target}"
                    + (f" — {f.title}" if f.title else "")
                    + (f" (by {f.found_by})" if hasattr(f, "found_by") and f.found_by else "")
                )
            findings_section = (
                "## Current Findings\n"
                + "\n".join(finding_lines) + "\n\n"
                "## Chain Opportunities\n"
                "Look at the findings above. Can any be COMBINED into a higher-impact chain?\n"
                "Common chains that multiply bounty value:\n"
                "- CORS misconfig + token/secret leak → account takeover\n"
                "- Open redirect + OAuth callback → session hijack\n"
                "- SSRF + internal API access → data exfiltration\n"
                "- XSS + CSRF → stored account takeover\n"
                "- IDOR + PII endpoint → mass data leak\n"
                "- Info disclosure + auth bypass → privilege escalation\n\n"
                "If you see a PARTIAL chain (one half found), redirect an agent to look for "
                "the OTHER HALF on the same target. Example: if agent-0 found CORS misconfig "
                "on api.example.com, tell another agent to hunt for token leaks or sensitive "
                "endpoints on api.example.com that could be exfiltrated via the CORS flaw.\n\n"
            )

        prompt = (
            "You are the Director of an AUTHORIZED bug bounty red team. "
            "All targets are in-scope programs on HackerOne/Bugcrowd/Intigriti/YesWeHack "
            "that explicitly invite security testing. The program owners have granted "
            "permission for active scanning, fuzzing, and exploitation within their "
            "published scope. Your agents MUST actively test these targets — that is "
            "their job. Do NOT tell agents to stop testing or switch to \"theoretical\" "
            "analysis. Active verification and reproduction of vulnerabilities is required.\n\n"
            f"## Current Status\n"
            f"- Elapsed: {elapsed_minutes:.0f}min\n"
            f"- Budget: ${budget_spent:.2f} / ${budget_total:.2f}\n"
            f"- Total findings so far: {findings_count}\n"
            f"- Shared memory: {json.dumps(shared_memory_summary)}\n\n"
            + findings_section +
            f"## Agent Activity\n"
            + "\n\n".join(agent_sections) + "\n\n"
            "## The Validator Agent\n"
            "The 'validator' agent is YOUR quality-assurance assistant. It independently re-tests "
            "findings submitted by pentesters to catch false positives. Do NOT:\n"
            "- Tell it to claim targets or start pentesting — that's not its job\n"
            "- Score or penalize it — it's exempt from the scoring system\n"
            "- Tell it to stop or sleep — it manages its own workflow\n"
            "DO: redirect it if it's re-running the same tests as the pentester (it should test "
            "differently — preconditions, different payloads, impact assessment).\n\n"
            "## Authentication\n"
            "Agents have permission to create test accounts on targets with signup flows. "
            "If an agent hits an authentication wall (403, 401, login redirect), DO NOT tell them "
            "to give up or move on. Instead, redirect them to:\n"
            "1. Look for signup/registration pages (/register, /signup, /create-account)\n"
            "2. Create a test account using the browser tools\n"
            "3. Share credentials via post_discovery(category=\"credential\") so other agents can reuse them\n"
            "4. Then re-test the protected endpoints with the authenticated session\n"
            "Authenticated surfaces (IDOR, privilege escalation, auth bypass) are where the highest bounties are.\n\n"
            "## Your Task\n"
            "Review each agent's strategy. If an agent is:\n"
            "- Stuck repeating similar failed scans → redirect to different tools or targets\n"
            "- Only using one tool type → suggest diversifying\n"
            "- Scanning out-of-scope targets → correct them\n"
            "- Spending too long on recon without moving to vuln testing → push them forward\n"
            "- Hitting auth walls without trying to create accounts → tell them to register and authenticate\n"
            "- Being productive → leave them alone\n"
            "- Finding one half of a chain → redirect them (or a free agent) to test the other half\n\n"
            "## Redirect phrasing — escalate, don't repeat yourself\n"
            "If this is the FIRST time you're redirecting an agent on a given mistake, a plain "
            "instruction is fine. If you already redirected this agent for the SAME mistake last "
            "review and they're still doing it, mild phrasing ('stop', 'don't') clearly isn't "
            "landing — open the instruction with '⚠️ ALERT: you are repeating a mistake I already "
            "flagged.' and state the one concrete action they must take next, instead of restating "
            "the same soft correction a third time.\n\n"
            "## Scoring\n"
            "You can award or penalize agents with score adjustments (PENTESTERS ONLY — not the validator). "
            "Agents see their score and are incentivized to improve. Use scores to reinforce good "
            "behavior and discourage bad behavior. Guidelines:\n"
            "- Deep, thorough testing of one target: +5 to +15\n"
            "- Creative attack approach or unusual technique: +10\n"
            "- Finding a chain component that completes an attack chain: +20\n"
            "- Shallow recon hopping between targets: -10 to -20\n"
            "- Ignoring your previous redirects: -15\n"
            "- Productive agent making progress: +5\n"
            "- Wasting budget on pointless scans: -10\n\n"
            "Return JSON:\n"
            '{"redirects": [{"agent": "pentester-0", "instruction": "..."}], '
            '"scores": [{"agent": "pentester-0", "delta": 10, "reason": "deep XSS testing"}]}\n'
            "Both redirects and scores are optional — omit either if not needed.\n"
            "IMPORTANT: Return ONLY the JSON object, no other text."
        )

        if shared_memory and shared_memory.mechanisms:
            mech_lines = shared_memory.director_lines()
            if mech_lines:
                prompt += "\n\n## Active mechanisms\n" + "\n".join(mech_lines)

        # Inject human directives if any are pending
        if self._pending_directives:
            directive_block = (
                "\n\n=== HUMAN OPERATOR DIRECTIVES (HIGH PRIORITY) ===\n"
                + "\n".join(f"  >> {d}" for d in self._pending_directives)
                + "\n=== END DIRECTIVES ===\n"
                "Incorporate these human directives into your review. "
                "If the human asks a question, include your answer in a "
                '"human_response" field in your JSON output.'
            )
            prompt += directive_block
            self._pending_directives.clear()

        try:
            llm = self.llm
            response = await llm.analyze(prompt)

            # Record cost
            if cost_tracker and hasattr(llm, "estimate_cost"):
                # Rough token estimate: ~4 chars/token
                in_tokens = len(prompt) // 4
                out_tokens = len(response) // 4
                cost = llm.estimate_cost(in_tokens, out_tokens)
                model_name = getattr(llm, "model", "unknown")
                provider_name = type(llm).__name__
                cost_tracker.add_cost(
                    "llm_api", cost,
                    detail=f"director_review {provider_name}/{model_name}",
                )

            redirects, scores, human_response = self._parse_review_response(response)
            return redirects, scores, human_response

        except Exception:
            # Try fallback LLM
            if self._fallback_llm is not None and self._fallback_llm is not self.llm:
                try:
                    response = await self._fallback_llm.analyze(prompt)
                    if cost_tracker is not None:
                        cost_tracker.add_cost("llm_api", self._fallback_llm.estimate_cost(len(prompt) // 4, len(response) // 4),
                                              detail="director_review_fallback estimated_tokens")
                    return self._parse_review_response(response)
                except Exception:
                    logger.exception("Director fallback swarm review failed")
            else:
                logger.exception("Director swarm review failed")
            return [], [], ""

    @staticmethod
    def _parse_review_response(response: str) -> tuple[list[dict[str, str]], list[dict[str, Any]], str]:
        """Parse LLM response into redirects, score adjustments, and human response.

        Returns:
            (redirects, scores, human_response)
        """
        text = response.strip()

        # Try ```json blocks
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()

        # Find JSON object
        if "{" in text:
            start = text.index("{")
            end = text.rfind("}") + 1
            if end > start:
                text = text[start:end]

        try:
            parsed = json.loads(text)

            # Parse redirects
            redirects = []
            for r in parsed.get("redirects", []):
                if isinstance(r, dict) and "agent" in r and "instruction" in r:
                    redirects.append({"agent": r["agent"], "instruction": r["instruction"]})

            # Parse scores
            scores = []
            for s in parsed.get("scores", []):
                if isinstance(s, dict) and "agent" in s and "delta" in s:
                    try:
                        delta = int(s["delta"])
                        # Clamp to reasonable range
                        delta = max(-30, min(30, delta))
                        scores.append({
                            "agent": s["agent"],
                            "delta": delta,
                            "reason": s.get("reason", "director review"),
                        })
                    except (ValueError, TypeError):
                        continue

            # Parse human response (director's reply to human operator)
            human_response = parsed.get("human_response", "")

            return redirects, scores, human_response
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Director: could not parse swarm review response as JSON")
            return [], [], ""

    @staticmethod
    def _parse_redirects(response: str) -> list[dict[str, str]]:
        """Parse LLM response into a list of redirect instructions (legacy compat)."""
        redirects, _, _ = Director._parse_review_response(response)
        return redirects

    # ------------------------------------------------------------------
    # Post-swarm debrief
    # ------------------------------------------------------------------

    async def debrief_swarm(
        self,
        shared_memory,
        agent_traces: list[dict[str, Any]],
        targets: list[str],
        findings_count: int,
        cost_spent: float,
        cost_budget: float,
        cost_tracker: Any = None,
    ) -> str:
        """Compile a post-swarm debrief summarizing all agent activity.

        Returns a structured summary string for CLI/dashboard display.
        """
        if self.dry_run or self.llm is None:
            return f"Swarm complete: {findings_count} findings, ${cost_spent:.2f} spent."

        # Build discovery data from shared memory
        discoveries = shared_memory.read_all(category="discovery") if shared_memory else []
        notes = shared_memory.read_all(category="note") if shared_memory else []
        findings = shared_memory.get_findings() if shared_memory else []

        discovery_summary = []
        for e in discoveries[:50]:
            discovery_summary.append(f"- [{e.agent_id}] {e.entry_type}: {e.content[:120]}")

        findings_summary = []
        for f in findings[:20]:
            findings_summary.append(
                f"- [{f.severity.value}] {f.title or f.vuln_type.value} on {f.target} ({f.url})"
            )

        trace_summary = []
        scoreboard = shared_memory.get_scoreboard() if shared_memory else []
        score_map = {s["agent"]: s["score"] for s in scoreboard}
        for t in agent_traces:
            agent_score = score_map.get(t["agent"], 0)
            trace_summary.append(
                f"- {t['agent']}: {t['iterations']} iterations, "
                f"{t['tool_calls']} tool calls, score={agent_score}, exit={t['exit_reason']}"
            )

        prompt = (
            "You are the Director. The swarm pen-test is complete. Write a debrief.\n\n"
            f"## Targets ({len(targets)})\n"
            + ", ".join(targets[:20])
            + (f" ... +{len(targets)-20} more" if len(targets) > 20 else "")
            + f"\n\n## Cost\n${cost_spent:.2f} / ${cost_budget:.2f}\n\n"
            f"## Agent Performance\n"
            + ("\n".join(trace_summary) or "No agent data")
            + f"\n\n## Discoveries ({len(discoveries)})\n"
            + ("\n".join(discovery_summary[:30]) or "None")
            + f"\n\n## Findings ({findings_count})\n"
            + ("\n".join(findings_summary) or "No confirmed findings")
            + "\n\n## Task\n"
            "Write a concise debrief covering:\n"
            "1. **Findings summary** — what was found, severity, exploitability\n"
            "2. **Coverage assessment** — what was tested vs what was missed\n"
            "3. **Agent performance** — who did well, who got stuck, why\n"
            "4. **Recommendations** — what to try next time, unexplored attack surface\n\n"
            "Be specific and actionable. Reference actual targets and findings."
        )

        try:
            llm = self.llm
            response = await llm.analyze(prompt)

            # Record cost
            if cost_tracker and hasattr(llm, "estimate_cost"):
                in_tokens = len(prompt) // 4
                out_tokens = len(response) // 4
                cost = llm.estimate_cost(in_tokens, out_tokens)
                model_name = getattr(llm, "model", "unknown")
                provider_name = type(llm).__name__
                cost_tracker.add_cost(
                    "llm_api", cost,
                    detail=f"director_debrief {provider_name}/{model_name}",
                )

            return response

        except Exception:
            logger.exception("Director debrief failed")
            return f"Swarm complete: {findings_count} findings, ${cost_spent:.2f} spent. (debrief generation failed)"

    # ------------------------------------------------------------------
    # Attack chain synthesis
    # ------------------------------------------------------------------

    async def synthesize_attack_chains(
        self,
        findings: list,
        discoveries: list,
        cost_tracker: Any = None,
        graph_context: str = "",
    ) -> list[dict]:
        """Analyze findings for chainable combinations that create higher-impact attacks.

        Returns a list of chain dicts:
            {"title": str, "severity": str, "description": str, "evidence": str,
             "target": str, "chain_components": [finding_indices], "estimated_bounty": float}
        """
        if self.dry_run or self.llm is None or len(findings) < 2:
            return []

        # Build findings summary
        findings_text = []
        for i, f in enumerate(findings):
            sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
            vtype = f.vuln_type.value if hasattr(f.vuln_type, "value") else str(f.vuln_type)
            findings_text.append(
                f"[{i}] [{sev.upper()}] {f.title or vtype} on {f.target} ({f.url})\n"
                f"    Type: {vtype}\n"
                f"    Evidence: {str(f.evidence)[:200]}\n"
                f"    Description: {str(f.description)[:200]}"
            )

        # Build discoveries summary (endpoints, tech, etc.)
        disc_text = []
        for d in discoveries[:50]:
            disc_text.append(f"- [{d.category}] {d.key}: {d.content[:120]}")

        prompt = (
            "You are a senior penetration tester reviewing findings from an automated scan.\n"
            "Your task: identify combinations of findings that can be CHAINED together to create\n"
            "a higher-impact attack than any single finding alone.\n\n"
            "## Common Attack Chains\n"
            "- Open Redirect + OAuth callback = Account Takeover\n"
            "- SSRF + Internal service discovery = Internal network access\n"
            "- API key exposure + Admin API endpoint = Privilege escalation\n"
            "- XSS + CSRF token = Session hijack\n"
            "- CORS misconfiguration + Sensitive API = Data exfiltration\n"
            "- IDOR + PII endpoint = Mass data breach\n"
            "- SQL injection + Admin credentials table = Full compromise\n"
            "- Subdomain takeover + Session cookie scope = Cookie theft\n"
            "- Secret exposure + Cloud API = Infrastructure compromise\n\n"
            + (graph_context + "\n\n" if graph_context else "")
            + f"## Individual Findings ({len(findings)})\n"
            + "\n\n".join(findings_text)
            + "\n\n## Discovered Endpoints/Context\n"
            + ("\n".join(disc_text) if disc_text else "(none)")
            + "\n\n## Instructions\n"
            "Analyze these findings and identify chains where combining 2+ findings creates a\n"
            "significantly higher-impact attack. Only report chains that are REALISTIC and\n"
            "ACTIONABLE — not theoretical. Each chain should escalate severity (e.g., two mediums\n"
            "become a critical).\n\n"
            "For each chain, provide:\n"
            "- A clear attack narrative: step 1 → step 2 → impact\n"
            "- Which findings are combined (by index number)\n"
            "- The escalated severity (higher than individual findings)\n"
            "- Estimated bounty for the chain\n\n"
            "Return JSON:\n"
            '{"chains": [\n'
            '  {"title": "Account Takeover via Open Redirect + OAuth",\n'
            '   "severity": "critical",\n'
            '   "target": "example.com",\n'
            '   "description": "Step 1: ... Step 2: ... Impact: ...",\n'
            '   "evidence": "Finding [0] provides... Finding [2] enables...",\n'
            '   "chain_components": [0, 2],\n'
            '   "estimated_bounty": 5000}\n'
            "]}\n"
            "Return empty chains [] if no realistic chains exist. Do NOT force chains.\n"
            "IMPORTANT: Return ONLY the JSON object."
        )

        try:
            llm = self.llm
            response = await llm.analyze(prompt)

            if cost_tracker and hasattr(llm, "estimate_cost"):
                in_tokens = len(prompt) // 4
                out_tokens = len(response) // 4
                cost = llm.estimate_cost(in_tokens, out_tokens)
                model_name = getattr(llm, "model", "unknown")
                provider_name = type(llm).__name__
                cost_tracker.add_cost(
                    "llm_api", cost,
                    detail=f"chain_synthesis {provider_name}/{model_name}",
                )

            chains = self._parse_chains(response)
            return chains

        except Exception:
            if self._fallback_llm is not None and self._fallback_llm is not self.llm:
                try:
                    response = await self._fallback_llm.analyze(prompt)
                    return self._parse_chains(response)
                except Exception:
                    logger.exception("Director fallback chain synthesis failed")
            else:
                logger.exception("Director chain synthesis failed")
            return []

    @staticmethod
    def _parse_chains(response: str) -> list[dict]:
        """Parse chain synthesis LLM response."""
        text = response.strip()

        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()

        if "{" in text:
            start = text.index("{")
            end = text.rfind("}") + 1
            if end > start:
                text = text[start:end]

        try:
            parsed = json.loads(text)
            chains = parsed.get("chains", [])
            valid = []
            for c in chains:
                if not isinstance(c, dict):
                    continue
                if not c.get("title") or not c.get("chain_components"):
                    continue
                # Validate severity
                sev = c.get("severity", "high").lower()
                if sev not in ("critical", "high", "medium", "low", "info"):
                    sev = "high"
                valid.append({
                    "title": c["title"],
                    "severity": sev,
                    "target": c.get("target", ""),
                    "description": c.get("description", ""),
                    "evidence": c.get("evidence", ""),
                    "chain_components": c["chain_components"],
                    "estimated_bounty": float(c.get("estimated_bounty", 0)),
                })
            return valid
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Director: could not parse chain synthesis response")
            return []

    # ------------------------------------------------------------------
    # Domain triage
    # ------------------------------------------------------------------

    async def triage_domains(
        self,
        domains: list[str],
        *,
        graph_context: str = "",
        cost_tracker: Any = None,
    ) -> list[dict[str, str]]:
        """Fingerprint and rank domains by attack potential before spawning agents.

        Returns a list of dicts:
            [{"domain": str, "priority": "high"|"medium"|"low", "notes": str}, ...]
        """
        if self.dry_run or self.llm is None:
            return [{"domain": d, "priority": "medium", "notes": "dry-run"} for d in domains]

        # ── Quick parallel HTTP fingerprinting ──────────────────────────
        import httpx

        fingerprints: dict[str, dict[str, Any]] = {}
        sem = asyncio.Semaphore(20)

        async def _probe(client: httpx.AsyncClient, domain: str) -> None:
            async with sem:
                url = f"https://{domain}" if not domain.startswith(("http://", "https://")) else domain
                try:
                    resp = await client.get(url, follow_redirects=True, timeout=5.0)
                    interesting_headers = {}
                    for hdr in (
                        "access-control-allow-origin",
                        "content-security-policy",
                        "x-frame-options",
                        "strict-transport-security",
                        "x-content-type-options",
                    ):
                        val = resp.headers.get(hdr)
                        if val:
                            interesting_headers[hdr] = val[:120]

                    fingerprints[domain] = {
                        "status": resp.status_code,
                        "server": resp.headers.get("server", ""),
                        "x_powered_by": resp.headers.get("x-powered-by", ""),
                        "content_type": resp.headers.get("content-type", ""),
                        "size": len(resp.content),
                        "interesting_headers": interesting_headers,
                    }
                except Exception as exc:
                    fingerprints[domain] = {"status": 0, "error": str(exc)[:120]}

        async with httpx.AsyncClient(
            verify=False,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            await asyncio.gather(*[_probe(client, d) for d in domains])

        # ── Build LLM prompt ────────────────────────────────────────────
        fp_lines = []
        for d in domains:
            fp = fingerprints.get(d, {"status": 0, "error": "no probe"})
            if fp.get("error"):
                fp_lines.append(f"- {d}: ERROR ({fp['error'][:80]})")
            else:
                parts = [
                    f"status={fp['status']}",
                    f"server={fp.get('server', '?')}",
                ]
                if fp.get("x_powered_by"):
                    parts.append(f"x-powered-by={fp['x_powered_by']}")
                parts.append(f"content_type={fp.get('content_type', '?')}")
                parts.append(f"size={fp.get('size', 0)}")
                if fp.get("interesting_headers"):
                    parts.append(f"security_headers={list(fp['interesting_headers'].keys())}")
                fp_lines.append(f"- {d}: {', '.join(parts)}")

        prompt = (
            "You are the Director of an authorized bug bounty pen-test team.\n"
            "Below are HTTP fingerprints for target domains. Rank them by attack potential.\n\n"
            "## Fingerprints\n"
            + "\n".join(fp_lines)
            + "\n"
            + (f"\n## Historical Graph Context\n{graph_context}\n" if graph_context else "")
            + "\n## Ranking Criteria\n"
            "- Attack surface: web apps > static sites > CDNs\n"
            "- Technology stack: known-vulnerable frameworks > modern hardened stacks\n"
            "- Historical findings: domains similar to ones with accepted findings\n"
            "- Interesting behaviors: 403 pages, redirects, API endpoints, missing security headers\n"
            "- Errors/unreachable: lowest priority but still include them\n\n"
            "Return JSON with ALL domains ranked:\n"
            '{"ranked": [{"domain": "example.com", "priority": "high", "notes": "React SPA with API, no CSP"}, ...]}\n'
            "priority must be one of: high, medium, low\n"
            "IMPORTANT: Return ONLY the JSON object, no other text."
        )

        try:
            llm = self.llm
            response = await llm.analyze(prompt)

            # Record cost
            if cost_tracker and hasattr(llm, "estimate_cost"):
                in_tokens = len(prompt) // 4
                out_tokens = len(response) // 4
                cost = llm.estimate_cost(in_tokens, out_tokens)
                model_name = getattr(llm, "model", "unknown")
                provider_name = type(llm).__name__
                cost_tracker.add_cost(
                    "llm_api", cost,
                    detail=f"domain_triage {provider_name}/{model_name}",
                )

            ranked = self._parse_triage_response(response, domains)
            return ranked

        except Exception:
            if self._fallback_llm is not None and self._fallback_llm is not self.llm:
                try:
                    response = await self._fallback_llm.analyze(prompt)
                    return self._parse_triage_response(response, domains)
                except Exception:
                    logger.exception("Director fallback triage failed")
            else:
                logger.exception("Director domain triage failed")
            # Heuristic fallback: alive = high, errors = low
            return self._heuristic_triage(domains, fingerprints)

    def _parse_triage_response(
        self, response: str, all_domains: list[str],
    ) -> list[dict[str, str]]:
        """Parse LLM triage response, ensuring all input domains are represented."""
        text = response.strip()

        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()

        if "{" in text:
            start = text.index("{")
            end = text.rfind("}") + 1
            if end > start:
                text = text[start:end]

        try:
            parsed = json.loads(text)
            ranked_raw = parsed.get("ranked", [])
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Director: could not parse triage response as JSON")
            return [{"domain": d, "priority": "medium", "notes": "parse error"} for d in all_domains]

        # Build result, validating priorities
        result = []
        seen = set()
        for entry in ranked_raw:
            if not isinstance(entry, dict) or "domain" not in entry:
                continue
            domain = entry["domain"]
            priority = entry.get("priority", "medium").lower()
            if priority not in ("high", "medium", "low"):
                priority = "medium"
            result.append({
                "domain": domain,
                "priority": priority,
                "notes": entry.get("notes", ""),
            })
            seen.add(domain)

        # Ensure domains not in LLM response are still included as medium
        for d in all_domains:
            if d not in seen:
                result.append({"domain": d, "priority": "medium", "notes": "not ranked by LLM"})

        return result

    @staticmethod
    def _heuristic_triage(
        domains: list[str],
        fingerprints: dict[str, dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Simple heuristic fallback when LLM triage fails."""
        result = []
        for d in domains:
            fp = fingerprints.get(d, {})
            if fp.get("error") or fp.get("status", 0) == 0:
                result.append({"domain": d, "priority": "low", "notes": "unreachable"})
            elif fp.get("status", 0) in (200, 301, 302, 403):
                result.append({"domain": d, "priority": "high", "notes": f"alive (HTTP {fp['status']})"})
            else:
                result.append({"domain": d, "priority": "medium", "notes": f"HTTP {fp.get('status', '?')}"})
        return result

    # ------------------------------------------------------------------
    # Concern handling
    # ------------------------------------------------------------------

    async def handle_worker_concern(
        self,
        concern_msg: AgentMessage,
    ) -> DirectorDecision:
        """Evaluate a worker's concern and decide how to respond."""
        if self.dry_run or self.llm is None:
            # In dry-run, critical concerns → skip, others → continue
            if concern_msg.priority == "critical":
                return DirectorDecision(
                    decision="skip",
                    reasoning=f"Dry-run: skipping due to critical concern from {concern_msg.sender}.",
                )
            return DirectorDecision(
                decision="continue",
                reasoning=f"Dry-run: continuing despite {concern_msg.priority} concern from {concern_msg.sender}.",
            )

        prompt = (
            "You are the Director (Research Lead) of an authorized bug bounty "
            "security research team. A worker has raised a concern.\n\n"
            f"Worker: {concern_msg.sender}\n"
            f"Priority: {concern_msg.priority}\n"
            f"Concern: {concern_msg.content}\n"
            f"Data: {concern_msg.data}\n\n"
            "How should we proceed? Options:\n"
            "1. CONTINUE — acknowledge but proceed anyway\n"
            "2. SKIP — skip the current target and move on\n"
            "3. ABORT — stop the entire pipeline\n"
            "4. REDIRECT — change strategy (explain what to do differently)\n\n"
            "Respond with:\n"
            "DECISION: <continue|skip|abort|redirect>\n"
            "REASONING: <your reasoning>"
        )

        try:
            response = await self.llm.analyze(prompt)
            decision = self._parse_decision(response)
        except Exception:
            logger.exception("Director concern handling failed")
            decision = DirectorDecision(
                decision="continue",
                reasoning="Failed to evaluate concern, proceeding with caution.",
            )

        self.bus.send(
            AgentMessage(
                sender=self.name,
                recipient=concern_msg.sender,
                msg_type="decision",
                content=decision.reasoning,
                data={"decision": decision.decision},
                in_reply_to=concern_msg.id,
            )
        )

        return decision

    # ------------------------------------------------------------------
    # God mode directives
    # ------------------------------------------------------------------

    def _on_directive(self, message: AgentMessage) -> None:
        """Accumulate incoming directives for injection into evaluations."""
        if message.recipient in (self.name, "all"):
            self._pending_directives.append(message.content)
            logger.info("Director received directive: %s", message.content[:120])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_evaluation_prompt(
        self,
        task_result: TaskResult,
        phase: str,
        concerns: list[AgentMessage],
        observations: list[AgentMessage],
        consultations: list[AgentMessage],
        budget_remaining: float | None,
        learning_insights: str,
    ) -> str:
        parts = [
            "You are the Director (Research Lead) of an authorized bug bounty security "
            "research team. Your team's mission is to find EXPLOITABLE SECURITY "
            "VULNERABILITIES in targets and earn bounties. You are NOT looking for "
            "software bugs — only security issues with real impact.",
            f"\nPhase just completed: {phase}",
            "",
            f"Worker '{task_result.agent_name}' reports:",
            f'"{task_result.reflection}"',
        ]

        if concerns:
            parts.append("\nConcerns raised during this phase:")
            for c in concerns[-5:]:
                parts.append(f'  [{c.priority}] {c.sender}: "{c.content}"')

        if observations:
            parts.append("\nObservations:")
            for o in observations[-5:]:
                parts.append(f'  {o.sender}: "{o.content}"')

        if consultations:
            parts.append("\nInter-worker consultations:")
            for m in consultations[-6:]:
                parts.append(f'  [{m.msg_type}] {m.sender} → {m.recipient}: "{m.content[:150]}"')

        if budget_remaining is not None:
            parts.append(f"\nBudget remaining: ${budget_remaining:.2f}")

        if learning_insights:
            parts.append(f"\nHistorical insights: {learning_insights}")

        # Inject god-mode directives (high priority human overrides)
        if self._pending_directives:
            parts.append("\n=== HUMAN OPERATOR DIRECTIVES (HIGH PRIORITY) ===")
            for d in self._pending_directives:
                parts.append(f"  >> {d}")
            parts.append("=== END DIRECTIVES ===")
            self._pending_directives.clear()

        parts.extend([
            "\nBased on all this information, decide how to proceed.",
            "DECISION: <continue|skip|abort|redirect>",
            "REASONING: <your strategic reasoning>",
        ])

        return "\n".join(parts)

    def _default_decision(
        self,
        task_result: TaskResult,
        phase: str,
    ) -> DirectorDecision:
        """Default decision when no LLM is available (dry-run mode)."""
        if not task_result.success:
            return DirectorDecision(
                decision="skip",
                reasoning=f"Phase '{phase}' failed: {task_result.error}. Skipping.",
            )
        return DirectorDecision(
            decision="continue",
            reasoning=f"Phase '{phase}' completed. {task_result.reflection or 'Proceeding.'}",
        )

    def _parse_decision(self, response: str) -> DirectorDecision:
        """Parse LLM response into a DirectorDecision."""
        decision = "continue"
        reasoning = response

        for line in response.split("\n"):
            line = line.strip()
            upper = line.upper()
            if upper.startswith("DECISION:"):
                raw = line.split(":", 1)[1].strip().lower()
                if raw in ("continue", "skip", "abort", "redirect"):
                    decision = raw
            elif upper.startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()

        return DirectorDecision(decision=decision, reasoning=reasoning)

    def _parse_selection(self, response: str, max_count: int) -> list[int]:
        """Parse program selection indices from LLM response."""
        for line in response.split("\n"):
            if line.strip().upper().startswith("SELECTED:"):
                raw = line.split(":", 1)[1].strip()
                indices = []
                for part in raw.replace(",", " ").split():
                    try:
                        idx = int(part.strip()) - 1  # 1-based → 0-based
                        if 0 <= idx < max_count:
                            indices.append(idx)
                    except ValueError:
                        continue
                return indices
        return []
