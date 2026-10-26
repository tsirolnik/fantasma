"""Per-turn context assembly for pentester agents.

Instead of an append-only message history that gets compressed (and loses the
"what I've done / what to avoid" signal), we REBUILD one structured briefing every
turn: Background + Goal + Scope + Avoid + Established + Findings + Leads + the last
raw results. The block IS the agent's memory — nothing gets buried or summarized
away. See the agreed template.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


BACKGROUND = """\
You are an elite bug-bounty pentester on an AUTHORIZED engagement. Real tools hit real \
targets; follow the selected methodology and the authorized assignment. You are a RESEARCHER, \
not a scanner: you reason about how the application works and where its logic breaks. The \
bugs that pay are the ones scanners miss — broken access control, auth flaws, IDOR, injection \
with impact, SSRF, business-logic abuse, account-takeover chains. When you find one \
interesting thread, evaluate whether further work is justified by evidence. Use a tool only \
when it advances the objective; otherwise report completion or a blocker. Prove impact with \
reproducible evidence before calling something a finding."""

# Vuln classes that rarely pay — stated so the agent doesn't sink depth into them.
DEFAULT_LOW_VALUE_CLASSES = (
    "missing rate-limiting on its own, missing security headers, CORS on public APIs, "
    "open redirect without a chain, version disclosure without a working exploit, "
    "unverified secrets, self-XSS, \"endpoint exists / returns 200\" with no impact"
)


@dataclass
class TurnState:
    """Everything needed to assemble one turn's context. All per-turn except where noted."""

    in_scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    budget_spent: float = 0.0
    budget_total: float = 0.0
    dead_ends: list[str] = field(default_factory=list)        # "url — hit N× (RATE-LIMITED)"
    excluded_classes: str = DEFAULT_LOW_VALUE_CLASSES
    established_facts: list[str] = field(default_factory=list)  # distilled bullets
    findings: list[str] = field(default_factory=list)          # "[sev] title (status)"
    ranked_leads: list[str] = field(default_factory=list)      # "why High/Critical + next move"
    recent_results: str = ""                                   # verbatim last tool output(s)


def _bullets(items: list[str], empty: str) -> str:
    if not items:
        return f"  ({empty})"
    return "\n".join(f"  • {x}" for x in items)


# The default structure, as a .format()-style template with named sections —
# every {placeholder} below is produced by build_turn_context() and exists so
# a profile can supply a REPLACEMENT template string (same placeholder names,
# different structure/order/wording) via [methodology] turn_template = "..."
# in TOML, without touching this file. The rendered OUTPUT of this constant is
# byte-identical to the original hardcoded f-string it replaces.
DEFAULT_TURN_TEMPLATE = """\
{background}

## Goal
{goal}
{budget_line}

## Scope — these are your targets. Attack them hard; all your active testing goes here:
{scope_lines}
{scope_limit_line}

## Avoid — do NOT repeat or chase these:
{dead_ends}
Low/no-pay classes, don't spend depth here: {excluded_classes}

## What you've established
{established}

## Findings so far
{findings}

## Open leads — ranked by expected severity
{leads}

────────────────────────────────────────────────────────────────────────
Your last result(s):
{recent_results}

→ Pick ONE lead and reply with your single next action toward a High/Critical."""


def build_turn_context(s: TurnState, template: str | None = None) -> str:
    """Assemble the full per-turn briefing from live state. `template`
    overrides DEFAULT_TURN_TEMPLATE — same {placeholder} names, different
    structure — for a profile's [methodology] turn_template config."""
    pct_left = 0
    if s.budget_total > 0:
        pct_left = max(0, round(100 * (s.budget_total - s.budget_spent) / s.budget_total))

    scope_lines = _bullets(s.in_scope, "no targets set")
    oos = ", ".join(s.out_of_scope) if s.out_of_scope else "everything not listed above"

    sections = {
        "background": BACKGROUND,
        "goal": ("Prioritize the highest-impact plausible vulnerability within the assignment, "
                 "using the shortest practical time and fewest necessary tool calls. "
                 "Verify impact; conclude when useful leads are exhausted. Severity follows evidence."),
        "budget_line": (f"Budget: ${s.budget_spent:.2f} spent / ${s.budget_total:.2f} "
                        f"({pct_left}% left). This is a ceiling, not an instruction to spend the remainder."),
        "scope_lines": scope_lines,
        "scope_limit_line": (f"The ONLY limit: don't launch attacks against hosts NOT listed above "
                             f"({oos}). Looking up a\nrelated or typo domain's registration (whois/dig) "
                             f"to prove a takeover is fine.\n\n"
                             f"CRITICAL — wildcard scope semantics:\n"
                             f"  • '*.example.com' = subdomains ONLY (www.example.com, api.example.com, etc.)\n"
                             f"  • '*.example.com' does NOT include bare apex 'example.com' (blocked if not separately listed)\n"
                             f"  • Bare entries like 'example.com' cover themselves AND all subdomains\n"
                             f"  • If you get blocked on a bare apex, use a subdomain instead (www.*, api.*, console.*, etc.)"),
        "dead_ends": _bullets(s.dead_ends, "nothing yet"),
        "excluded_classes": s.excluded_classes,
        "established": _bullets(s.established_facts, "nothing yet — start by understanding what each target IS"),
        "findings": _bullets(s.findings, "none yet"),
        "leads": _bullets(s.ranked_leads, "none yet — hunt for a high-impact one"),
        "recent_results": s.recent_results or "  (nothing yet — this is your first move)",
    }
    try:
        return (template or DEFAULT_TURN_TEMPLATE).format(**sections)
    except (KeyError, IndexError) as exc:
        logger.warning("Custom turn_template has a bad placeholder (%s); using the default.", exc)
        return DEFAULT_TURN_TEMPLATE.format(**sections)


_DISTILL_PROMPT = """\
You are the intelligence analyst for a bug-bounty pentest. Turn the raw notes below into a
sharp state summary the operator will act on. Be concrete and ruthless: drop noise, keep
what changes the next move.

Targets (in scope): {targets}

Raw discoveries / recon notes:
{notes}

Findings already submitted:
{findings}

Return ONLY JSON, no prose:
{{
  "established": ["<=8 confirmed facts: hosts mapped, endpoints found, auth model, tech,
                   dead/irrelevant surfaces. one crisp bullet each"],
  "leads": ["<=5 open leads RANKED by expected severity, highest first. Each: the lead —
             why it could be High/Critical — the concrete next move. Untested high-value
             surfaces (admin/auth/payments/decisioning/user-data APIs) rank at the top."]
}}"""


async def distill_state(llm, targets: list[str], raw_notes: str, findings: list[str], cost_tracker=None) -> tuple[list[str], list[str]]:
    """Distill raw recon notes into (established_facts, ranked_leads) via a cheap LLM call.

    Returns ([], []) on any failure so the caller can fall back to raw notes.
    """
    if not llm or not raw_notes.strip():
        return [], []
    prompt = _DISTILL_PROMPT.format(
        targets=", ".join(targets) or "(none)",
        notes=raw_notes[:8000],
        findings="\n".join(f"- {f}" for f in findings) or "(none yet)",
    )
    try:
        resp = await llm.analyze(prompt)
        if cost_tracker is not None:
            cost_tracker.add_cost("llm_api", llm.estimate_cost(len(prompt) // 4, len(resp) // 4),
                                  detail="context_distillation estimated_tokens")
    except Exception:
        logger.debug("distill_state: LLM call failed", exc_info=True)
        return [], []
    # Extract the JSON object (models may wrap it in prose/fences).
    text = resp.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return [], []
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return [], []
    established = [str(x).strip() for x in data.get("established", []) if str(x).strip()]
    leads = [str(x).strip() for x in data.get("leads", []) if str(x).strip()]
    return established[:8], leads[:5]
