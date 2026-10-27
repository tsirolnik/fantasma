# Reproducible framework integration evaluation

This evaluates Fantasma’s integration behavior with a model in the loop, not
the model in isolation. The real-model run checks whether framework components
work together under actual provider responses; deterministic tests separately
exercise rejection, cancellation, persistence, and budget gates.

This harness exercises the real `InteractivePentester` tool loop, persistent goal
state and separate `GoalReviewer`. It runs a local synthetic receipt service in
two variants: an ownership-check flaw and its fixed control. The agent receives
the same task in both cases; the vulnerability flag and implementation are not
included in its prompt. It is told the two receipt IDs and owners, so this tests
evidence-based access-control assessment, **not endpoint discovery**.

Two configurations run by default: `retain_thinking = true` and `false`. One
repetition produces four cases. The model, configured token prices, goal review,
errors, tool counts and wall time are recorded. No results are preselected or
removed. Very small samples cannot establish which configuration is better.

## Reproduce

From the repository root with its dependencies installed:

```bash
# Costs nothing. Scripted models check wiring, not model effectiveness.
python benchmark/launch/run.py --offline --output benchmark/results/launch-offline

# REAL MODEL CALLS, using .env and the interactive profile's scanner/validator roles.
# Choose a fresh output directory for each run; existing directories are never overwritten.
python benchmark/launch/run.py --budget 1 --repeats 1 --timeout 180 \
  --output benchmark/results/launch-live
```

`--budget` is cumulative across all cases, including reviewer calls. Spending is
estimated using configured prices; the provider invoice is authoritative. One
call may exceed the remaining estimated budget. `--timeout` is per case; each
case also has 12 model iterations, 12 tool calls and a 2,048 output-token limit
per investigator call. The separate reviewer has its normal response limit.
No fallback from failed live runs to scripted results occurs.

Only `lab_request`, `report_assessment`, goal tools and the decision journal are
available. `lab_request` can request only receipt 1 or 2 on the ephemeral
`127.0.0.1` service, with a synthetic Alice token. There are no arbitrary URLs,
shell tools, Docker execution, browser/search tools or MCP tools in this harness.
The LLM provider is the only external service contacted in live mode. Docker is
not needed. The service shuts down at the end of every case.

## Independent grading

The harness—not the goal reviewer—grades the recorded assessment:

- **Vulnerable:** the report must say `confirmed` and cite both Alice's successful
  baseline and a successful response containing Bob's receipt under Alice's identity.
- **Fixed:** the report must say `not_reproduced` and cite Alice's successful
  baseline and a 403 for Bob's receipt.
- A missing/inconclusive report or absent cited evidence fails the case.
- A `confirmed` report on the fixed variant is a false positive.

A reviewer approval is reported separately. It does not determine the grade.
The grader does not match vulnerability keywords or treat confidence as evidence.
This remains a small, prompted, synthetic test—not a blind benchmark or proof of
real-world vulnerability discovery, safety, or bounty performance.

## Local outputs

Each output directory contains:

- `report.md`: all cases, costs, timing and outcomes.
- `results.json` and per-case JSON: raw assessment, recorded HTTP evidence,
  reviewer feedback, errors and base Git commit / dirty-worktree status.
- `sessions/`: saved goal, review packet and conversation for inspection.
- `demo.cast`: timestamped actual harness output in asciicast v2 format.
- `demo.html`: a standalone readable transcript with 4× replay, clearly labeled
  `real_model` or `offline_scripted`. Open it locally in a browser.

Generated outputs stay local and are ignored by Git. Session snapshots contain
conversation and provider response data; review them before sharing any results.

## Audit framework behavior

```bash
python benchmark/launch/audit_framework.py benchmark/results/launch-live
```

This produces `framework-audit.json` and `.md`: saved history, persisted scope and
goal status, assessment-to-response references, review evidence for completed
goals, and the disabled retention setting. A correct fixture conclusion is a
functional integration outcome, not a standalone model benchmark. Stored metadata
alone does not prove wire replay; dedicated mocked-provider tests cover that path.

Negative runtime tests cover rejected completion, malformed verdicts, unavailable
reviewers, cancellation during review, scope denial, explicit session resume, and
exhausted budgets without making paid API calls.

### Live completion-gate negative control

After a real-model run has produced a passing fixed case, this optional command
submits a deliberately unsupported completion claim to a fresh reviewer. It uses
the recorded fixed-case responses (including the 403), not a new target request.

```bash
python benchmark/launch/reviewer_control.py benchmark/results/launch-live --budget 4
```

This makes one additional paid reviewer call. Its budget includes the main run's
recorded spend. The control passes only if review explicitly rejects the claim
and the goal remains active; errors/pauses do not count as a successful rejection.
It saves the verdict, references, goal state and additional cost without overwriting
an existing control result. This checks one concrete integration path; it does
not establish reviewer accuracy in general.
