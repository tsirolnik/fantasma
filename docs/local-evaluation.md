# Test an AI security investigation against a local access-control fixture

This walkthrough exercises Fantasma's interactive agent runtime, persistent goal,
and separate completion reviewer. The task is concrete: determine whether Alice
can read Bob's receipt, and cite the responses supporting that conclusion.

The fixture supplies two receipt IDs and their owners in advance. It is a test of
framework integration and evidence handling, not endpoint discovery or a measure
of real-world pentesting effectiveness. It does not exercise team coordination,
Docker tools, browser search, or MCP.

## Install and run without model credentials

Use Python 3.13+ on macOS or Linux. Clone the repository rather than installing
only the wheel: this development harness lives under `benchmark/`.

```bash
git clone https://github.com/tsirolnik/fantasma.git
cd fantasma
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python benchmark/launch/run.py --offline --output benchmark/results/first-check
python benchmark/launch/audit_framework.py benchmark/results/first-check
```

The output directory must not already exist. For another run, change its name.
Installation downloads Python dependencies. The offline evaluation itself uses
scripted model responses and a temporary HTTP service on `127.0.0.1`; it makes
no model API calls and needs neither Docker nor Chromium.

## Follow the request and evidence flow

1. The harness creates a goal with three criteria: retrieve Alice's receipt,
   test the other receipt, and record an assessment with response references.
2. The investigator calls `lab_request` with receipt IDs 1 and 2. The tool makes
   real HTTP requests using the synthetic Alice token and records observations.
3. `report_assessment` records `confirmed`, `not_reproduced`, or `inconclusive`
   with the observation IDs supporting it.
4. `update_goal` requests completion. A separate reviewer checks the evidence
   packet against the criteria. In offline mode, this reviewer is also scripted.
5. The harness grades the assessment independently of the reviewer verdict and
   saves the session, results, timing, errors, and estimated model cost.

The fixture starts once per case on an ephemeral loopback port and stops afterward.

## Understand the vulnerable and fixed controls

These are the fixture's defined responses, not measured model performance:

| Request using Alice's token | Vulnerable fixture | Fixed fixture |
| --- | --- | --- |
| `GET /receipts/1` | HTTP 200, owner `alice` | HTTP 200, owner `alice` |
| `GET /receipts/2` | HTTP 200, owner `bob` | HTTP 403, `forbidden` |

A passing vulnerable-case assessment must cite both the successful baseline and
Bob's receipt returned to Alice. A passing fixed-case assessment must cite the
baseline and the denied cross-account request. Calling the protected response a
vulnerability is a false positive. Missing or inconclusive assessments fail the
case; a reviewer approval alone does not make it pass.

The default run executes four cases: vulnerable and fixed fixtures, each with
`retain_thinking` enabled and disabled. This is not enough data to compare models
or establish that either retention setting improves investigations.

## Inspect your results

Open the following files in your chosen output directory:

| File | What to inspect |
| --- | --- |
| `report.md` | Each case's grade, false-positive flag, goal status, tool count, elapsed time, estimated cost, and run error |
| `results.json` and per-case JSON | Recorded HTTP observations, submitted assessment, reviewer feedback, and source version |
| `framework-audit.md` | Persisted history and scope, evidence references, completion checks, and disabled-retention behavior |
| `sessions/` | Saved conversation, goal, and review evidence packet |
| `demo.html` | Local replay of harness output, labeled by execution mode |

Expect offline output to be labeled `offline_scripted` with zero model cost.
Passing offline checks establish that the scripted integration path works;
they are not evidence that an LLM made the correct decision. Check the run error
and case count as well as individual grades. Provider errors and incomplete runs
must remain part of any evaluation you share.

Generated files are ignored by Git. Keep them local unless you have reviewed
conversation content, provider responses, and any sensitive data before sharing.

## Run the same task with a real model

Create `.env` if needed and configure a provider using the
[provider setup guide](getting-started.md#first-interactive-session) and
[configuration reference](configuration.md). The harness uses the interactive
profile's `scanner` and `validator` roles. Paid compatible endpoints need accurate
input/output token prices configured; local endpoints must be explicitly configured.

```bash
test -f .env || cp .env.example .env
# Edit .env with your provider, model, credentials, and token prices first.
python benchmark/launch/run.py --budget 1 --repeats 1 --timeout 180 \
  --output benchmark/results/first-live
python benchmark/launch/audit_framework.py benchmark/results/first-live
```

This makes real model calls and labels output `real_model`. The USD 1 budget is
an estimate across all cases and reviewer calls, not a hard billing cap: an
in-flight call can overshoot. Each case has a 180-second timeout, 12 model
iterations, and 12 tool calls. Review the actual cost and errors in your output;
no live success rate or dollar estimate is promised here.

The toolset remains restricted to the local fixture. In live mode the model
provider is the only external service contacted by the harness. A failed live
run is not silently replaced with scripted output.

For an additional paid test that submits an unsupported claim to the reviewer,
see the [completion-gate negative control](../benchmark/launch/README.md#live-completion-gate-negative-control).
The reviewer evaluates observations; it cannot rerun an exploit. A correctly
handled fixture is useful integration evidence, not proof of reliable autonomous
vulnerability discovery.

## Extend the investigation

- [Interactive investigations](interactive.md): scope, steering, tool inspection,
  persistent goals, and session resume.
- [Profiles and configuration](configuration.md): coordinated or competing teams,
  model roles, methodology, and settings precedence.
- [Python extension reference](reference/extensions.md): custom tools and workflows.
- [MCP setup](mcp.md): connect an external tool server.
- [Harness reference](../benchmark/launch/README.md): grading rules and audit details.
