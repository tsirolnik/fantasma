# How Fantasma compares to ARTEX

ARTEX is the best-known open-source multi-agent pentesting orchestrator of 2026.
Cloudflare estimates that ARTEX and its forks make up most of the AI pentest
orchestrators it sees. This page compares the two projects' design decisions.
It is not a benchmark: we have not run them side by side.

The ARTEX column comes from public reporting (listed below), not from reading
the ARTEX source code. The original repository was taken down in October 2026,
forks differ from one another, and some reported details conflict. Corrections
are welcome.

## At a glance

| | Fantasma | ARTEX (as publicly reported) |
| --- | --- | --- |
| Language / license | Python 3.13, MIT | Go backend, embedded Next.js UI, PostgreSQL; AGPL-3.0 |
| Default way of working | One interactive investigator that the user steers; teams and staged runs are opt-in profiles | An autonomous planner and parallel workers pursue goals until a time or step limit |
| Coordination | Shared feed, exclusive claims and deduplicated findings; a Director oversees swarm runs | A shared asset graph plus a per-task exploration graph (goals, intents, facts, findings) |
| Scope | Hosts recognized in tool calls are checked against the configured scope. The agent can request more hosts but cannot approve its own request | The operator sets scope and targets. Reports describe no technical scope check |
| Guarding actions | Scope approval and human checkpoints in swarm and lab runs | Operator-written regex rules on tool calls (approve or deny), with an approval log |
| Where tools run | CLI tools and generated code run in resource-limited Docker containers with no host mounts. HTTP and browser tools run in the host process | Reports describe agents running on the host or in a container with network and root access, and installing tools at runtime |
| Deciding a task is done | A separate reviewer checks each completion criterion against recorded tool output and must cite observation IDs | The goal-based loop ends when its goals are met or a time or step limit is reached |
| Cost control | Session budget checked before each model call; tool-call and iteration limits | Reports mention a step or time limit and fail-over across a pool of providers and keys |
| Extensibility | TOML profiles plus Python mechanism hooks; MCP servers | MCP over HTTP or SSE; recording MITM proxy |
| Models | Claude, Gemini/Vertex, OpenAI-compatible endpoints, Ollama; different models per role | Anthropic and OpenAI keys documented; any compatible endpoint in practice |

## Where ARTEX is stronger

- **Breadth of autonomous attack.** Reporting describes workers that install and
  write their own tooling and chain recon, credential attacks and exploitation.
  Fantasma is built for user-led investigation and gives up some of that autonomy.
- **Structured attack state.** ARTEX's exploration graph links goals, intents,
  facts and findings to assets. Fantasma's shared memory is flatter. Its optional
  graph memory (FalkorDB) records programs, endpoints and findings across runs
  rather than the reasoning inside one task.
- **Packaging.** ARTEX ships as a single binary or a Docker Compose stack.
  Fantasma is currently installed from source.

## Where the designs differ on purpose

- **The agent cannot widen its own authorization.** A `request_scope` call
  waits for `/scope approve`, and setting a goal never adds hosts.
- **"Done" requires evidence.** A completion claim goes to a fresh reviewer.
  If the reviewer's output is missing, malformed or contradictory, the goal pauses
  instead of completing.
- **Isolation by default for CLI tools.** These limits are documented in the
  [README boundaries](../README.md#boundaries). Scope checks are application
  checks, not egress control, and HTTP and browser tools run on the host. Use
  network controls when you need a hard boundary.

## A note on misuse

Microsoft, CrowdStrike, Unit 42 and Cloudflare have all reported threat actors
using ARTEX in real intrusions. Fantasma's controls are there to keep authorized
testers inside their scope. A determined operator can remove them from any
open-source tool, Fantasma included, so do not treat them as a defense against
abuse.

## Sources

- Microsoft Threat Intelligence, [What defenders need to know about ARTEX](https://www.microsoft.com/en-us/security/blog/2026/11/25/what-defenders-need-to-know-artex-agentic-ai-framework-threat-actors-abuse/) (2026-11-25)
- Unit 42, [ARTEX and AI-powered red teaming tools](https://unit42.paloaltonetworks.com/artex-and-ai-powered-red-teaming-tools/) (2026-11-11)
- Cloudflare, [Adapting to AI penetration testing orchestrators](https://blog.cloudflare.com/adapting-to-ai-penetration-testing-orchestrators) (2026-11-13)
- CrowdStrike, [Unknown threat actor uses AI-driven ARTEX to target South Korean finance](https://www.crowdstrike.com/en-us/blog/unknown-threat-actor-uses-artex-to-target-south-korean-finance/)
- ai-tldr, [ARTEX tool summary](https://ai-tldr.dev/tools/artex/) (third-party directory, 2026-10-18)
