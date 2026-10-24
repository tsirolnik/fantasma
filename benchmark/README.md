# swarmhound benchmark

A reproducible benchmark that measures how well swarmhound autonomously finds
vulnerabilities, against a lab of **intentionally-vulnerable, legal-to-attack**
targets.

> These targets exist to be hacked (OWASP Juice Shop, DVWA, VAmPI). Run them on
> an isolated machine. Ports are published on `127.0.0.1` only — never expose
> them to a network.

## What it measures

For each target the runner reports:

- **Recall** — of the vulnerability *classes* known to exist in the target, how
  many did the swarm surface at least once.
- **Class-match rate** (`precision` in output) — the fraction of findings whose
  type or text matches a known class. This does not distinguish true instances
  from false positives within that class.
- **Cost** (USD) and **wall-clock time** per target.

Scoring is **class-level and taxonomy-generous**: a finding counts for a class
if its `vuln_type` *or* its title/description/evidence text matches the class or
any alias (see `targets.json`). This is the honest way to grade an autonomous
agent — it gets credit for finding an XSS even if it labelled it
`misconfiguration`, but only for classes actually present in the target.

## Requirements

- Docker (for the targets)
- swarmhound installed and configured (`.env` with a working LLM provider)

## First run: local Juice Shop

Complete the root README installation first, including the tool image build.
Configure `ANTHROPIC_API_KEY` and an available `CLAUDE_MODEL` in `.env`.
The command below explicitly selects Claude for all three roles. Model calls
are paid; the target itself runs locally. Set your provider's spending limit as
well as the application's estimated budget.

**Do not start a separate Juice Shop container.** The runner creates its Compose
network, starts the pinned `bkimminich/juice-shop:v17.1.1` service, waits for
health, then stops it after the run. Port 3000 must be available. Compose binds
it to `127.0.0.1`, rather than all host interfaces.

```bash
# Inspect the command first: no Docker or model calls.
python benchmark/run_benchmark.py --targets juice-shop --dry-run \
    --director-llm claude --worker-llm claude --analyzer-llm claude \
    --budget 8 --agents 3 --timeout 2400

# Run the local target with paid model calls.
python benchmark/run_benchmark.py --targets juice-shop \
    --director-llm claude --worker-llm claude --analyzer-llm claude \
    --budget 8 --agents 3 --timeout 2400
```

The run has a 40-minute subprocess timeout, plus target startup/health-check
time. Runtime and memory depend on tooling and concurrency; the example is not
a measured resource guarantee. Findings, cost and coverage vary with model and
configuration, including the possibility of zero findings.

Look for `benchmark/results/benchmark_<timestamp>.json` and `.md`, containing
findings count, detected/missed classes, recall, class-match rate (`precision` in
the harness), cost and wall time. This documentation does not promise a score
range. Class matching alone does not confirm a PoC.

To remove stopped lab containers and the network after evaluation:

```bash
docker compose -f benchmark/docker-compose.yml down
```

## Other runs

```bash
# Sanity check — prints command, costs nothing
python benchmark/run_benchmark.py --targets juice-shop --dry-run

# Multi-target lab (all three targets, ~60–90 min total)
python benchmark/run_benchmark.py \
    --targets juice-shop,dvwa,vampi \
    --director-llm claude \
    --worker-llm claude \
    --analyzer-llm claude \
    --budget 5 \
    --agents 3 \
    --timeout 1800

# Local Ollama (requires a running Ollama server and a pulled OLLAMA_MODEL)
python benchmark/run_benchmark.py \
    --targets juice-shop \
    --director-llm ollama \
    --worker-llm ollama \
    --analyzer-llm ollama \
    --budget 1 \
    --agents 2 \
    --timeout 1200
```

The runner brings each target up, waits for health, runs the swarm, scores the
findings, then stops the target. Results are written to
`benchmark/results/benchmark_<timestamp>.{json,md}`.

### Useful flags

| Flag | Meaning |
|---|---|
| `--dry-run` | Print the swarmhound command and exit; spend nothing. |
| `--budget N` | Per-target USD budget cap. |
| `--timeout N` | Per-target run timeout in seconds. |
| `--no-lifecycle` | Targets are already up; don't start/stop them. |
| `--keep-up` | Leave targets running after the run (for demos). |
| `--extra "..."` | Extra args appended to the swarmhound command. |

## How targets are scoped

Each target runs with scope set to `localhost`. Host-side HTTP/browser tools use
its localhost port; the runner sets a Docker network and host-rewrite mapping so
container tools reach the corresponding `bench-<target>` service on that network.
Use the supplied Compose setup, including with `--no-lifecycle`, so those names
and networks exist. Findings are attributed by time window, so avoid concurrent
runs using the same database.

## Notes

- On Apple Silicon, VAmPI runs under amd64 emulation (slower but works).
- The vulnerability-class taxonomy in `targets.json` is intentionally broader
  than swarmhound's internal `VulnType` enum — see the project's product notes.
