# Writing custom mechanisms

New to the project? Start with [setup and your first custom mechanism](getting-started.md)
for installation, an external profile directory and a complete test-to-run walkthrough.

A mechanism is Python code responding to run events. TOML selects the class
and supplies constructor arguments; it does not define the behavior. You can
keep a profile and its Python extension outside the repository. No registry
edit or fork is needed.

Use mechanisms for additional rewards, penalties and agent feedback. To change
investigation instructions or tools, use a methodology; to change how targets
are obtained, use a target provider. Base acceptance, deduplication and scoring
are separate from mechanisms.

## Start with a working example

[`examples/mechanisms/coverage.py`](../examples/mechanisms/coverage.py) rewards
each worker's first accepted finding on each target. Its adjacent
[`profile.toml`](../examples/mechanisms/profile.toml) loads it:

```toml
[[mechanisms.items]]
class = "./coverage.py:CoverageBonus"
bonus = 7
```

Paths ending in `.py` resolve relative to the TOML file. For an installed Python
package, use `class = "my_package.incentives:CoverageBonus"`. Package references
use the active Python environment's import path. A standalone file should import
shared helpers through installed packages; package-relative imports require the
package form. Modules are cached for the process lifetime; restart after editing.

Copy both example files to your own directory to experiment independently. With
the dependencies and credentials from the [README](../README.md) installed, run
from the repository root against an authorized target:

```bash
fantasma full --profile examples/mechanisms/profile.toml \
  --targets YOUR_AUTHORIZED_DOMAIN --budget 1 \
  --director-llm claude --worker-llm claude
```

This is a real run and may incur provider charges. Test the mechanism without
models or network access first:

```bash
python - <<'PY'
from types import SimpleNamespace
from profiles.loader import load_profile
from memory.mechanisms import build_mechanisms

profile = load_profile('examples/mechanisms/profile.toml')
mechanism, = build_mechanisms(profile.mechanisms)
finding = SimpleNamespace(target='authorized.test')
assert mechanism.on_submit('worker-1', finding, True, None).score_delta == 7
assert mechanism.on_submit('worker-1', finding, True, None).score_delta == 0
print('Custom mechanism loaded and exercised')
PY
```

## Implement the hooks you need

Subclass `memory.mechanisms.Mechanism`, give it a unique `name`, and override any
of these synchronous methods. Other hooks default to no-ops.

| Hook | Input and return contract | When it runs |
| --- | --- | --- |
| `on_claim(agent_id, key, success)` | Return `MechanismEffect` | Claim attempts; check `success` before rewarding a claim |
| `on_submit(agent_id, finding, accepted, reason)` | Return `MechanismEffect` | After base submission handling and scoring |
| `on_turn(agent_id)` | Return `MechanismEffect` | Once when a worker's next turn briefing/digest is built, in either context mode |
| `on_director_review(scoreboard)` | Return a string or `None` | When the Director's mechanism guidance is assembled |
| `hides_rank_info()` | Return a bool | When shared memory formats rank visibility |
| `on_run_end(memories)` | Return `None` | After agents finish, before debrief; receives all team memories |

`MechanismEffect(score_delta=0, reason="", message=None)` carries an additive
score adjustment, its audit reason, and optional feedback. SharedMemory applies
claim/submit/turn effects. Do not apply the same delta directly yourself.
Run-end hooks instead use `memory.add_score(agent_id, delta, reason)` explicitly.

`accepted=True` means accepted into shared memory, **not independently verified**
by the Validator. A duplicate has `accepted=False, reason=None`; other rejected
submissions have a reason. Findings expose fields such as `target`, `vuln_type`,
`severity`, and `url`; the finding model is in `models/finding.py`. Claim keys can be
namespaced (for example `scan:example.test`); do not assume they are URLs.

## State, order and boundaries

Each team gets fresh mechanism instances, shared by its workers. Store per-worker
state keyed by `agent_id` and per-target state keyed by target, as the example
does. State is in memory; it is not restored by checkpoints. Hooks run
synchronously: avoid blocking I/O, slow work, and asynchronous hook definitions.

`[mechanisms].enabled` names are constructed first, followed by
`[[mechanisms.items]]` tables in order. Effects add to base scoring and earlier
effects. Avoid configuring the same mechanism twice: it will run twice for
ordinary events. Run-end dispatch deduplicates by `name`, using the first
instance and passing all team memories. Consequently, run-end hooks must not
assume they receive every instance's private state. Use unique names and inspect
the supplied memories for aggregate results. Run-end bonuses can change rankings
seen by later hooks.

These hooks do not replace the scheduler, validation, target allocation, or
termination policy. There is no arbitrary event subscription or mechanism hot
reload. Hook failures may be caught and logged by callers; exercise your hooks
in isolation before a paid run.

Extension code executes in the host Python process with its permissions. Only
load profiles and extensions you trust. Importing an extension can execute code;
constructor arguments are not sandboxed or automatically type-checked. Validate
your own arguments, as `CoverageBonus` does.

## Configure and combine

```toml
[mechanisms]
enabled = ["visibility_toggle"]

[[mechanisms.items]]
name = "first_finder_bonus"
bonus = 20

[[mechanisms.items]]
class = "./coverage.py:CoverageBonus"
bonus = 7
```

All table keys except `name`, `class`, `enabled`, and `when` become constructor keyword arguments.
Nested TOML tables become Python dictionaries. Use `class` for custom extensions
and `name` for built-ins. Unknown constructor arguments fail at construction;
custom classes must inherit `Mechanism`.

| Built-in | Constructor parameters (defaults) |
| --- | --- |
| `first_finder_bonus` | `bonus = 15` |
| `duplicate_escalation` | `step = 5` |
| `target_bidding` | `stake = 8`, `payoff = 12` (total payout, including stake) |
| `idle_tax` | `grace_seconds = 90`, `tax = 2` |
| `team_rivalry_pool` | `pool = 60`; meaningful only with multiple teams |
| `relative_rank_bonus` | `multipliers = { "1" = 0.5, "2" = 0.2 }` (one-based ranks) |
| `visibility_toggle` | None |

`--mechanisms` replaces the profile's entire list and accepts built-in names
only. Use a profile for custom classes and parameters. An empty profile list explicitly disables mechanisms. Without a profile or
`--mechanisms`, the `MECHANISMS` environment setting supplies the list.

See [architecture](architecture.md) for the component flow and
[`memory/mechanisms.py`](../memory/mechanisms.py) for complete built-in behavior.

## Control when a mechanism applies

Use `enabled = false` to keep an entry in a profile without importing or
constructing that mechanism. Use a `when` table to gate individual hook calls:

```toml
[[mechanisms.items]]
class = "./coverage.py:CoverageBonus"
bonus = 12
enabled = true

[mechanisms.items.when]
hooks = ["on_submit"]
accepted = true
severities = ["high", "critical"]
targets = ["authorized.test"]
```

The complete [conditional example](../examples/mechanisms/conditional.toml)
is runnable with the same command as the basic example, replacing the profile
path. Replace `authorized.test` with the exact target value you expect.

| Condition | Meaning |
| --- | --- |
| `hooks` | Hook method names from the lifecycle table; omitted means all hooks; `[]` calls none |
| `agent_ids` | Exact runtime worker IDs; requires hooks limited to `on_submit`, `on_claim`, `on_turn` |
| `accepted` | Boolean submission outcome filter |
| `targets` | Exact `finding.target` strings, with no wildcard or URL normalization |
| `severities` | Lowercase finding severity values, e.g. `high`, `critical` |

Submission filters (`accepted`, `targets`, `severities`) require
`hooks = ["on_submit"]`. All supplied conditions must match. Empty lists match
nothing. Conditions are checked **before** calling the extension, so skipped
events cannot update its state. Filters select events, not authorized scope;
`targets` here never grants permission to test a host.

Hook selection also suppresses state maintenance in unselected hooks. For
example, an idle tax needs `on_claim` and `on_submit` to reset its timer: selecting
only `on_turn` intentionally prevents those resets. For rules involving elapsed
time, counts, budgets or custom state, implement the condition inside Python
rather than embedding expressions in TOML.

`name`, `class`, `enabled`, and `when` are reserved framework keys, not constructor
arguments. Existing classes using the new reserved names as constructor keywords
must rename those parameters. Do not list the same mechanism twice to model
multiple conditions: ordinary events would execute both instances, while run-end
hooks deduplicate by name. Put complex branching in one custom mechanism.

## Discover and inspect without launching a run

```bash
fantasma profiles mechanisms
fantasma profiles inspect bugbounty
fantasma profiles inspect examples/mechanisms/conditional.toml
```

The first command lists built-ins, Python constructor signatures, descriptions
and implemented hooks. The inspector resolves profile defaults and extension
paths, constructs enabled mechanisms, and prints JSON with active hook names and
conditions. Invalid filters, missing extension classes, or unexpected constructor
arguments fail with a nonzero exit code. This validates configuration and
construction, not the behavior of arbitrary extension code.

Inspection does not create the orchestrator, call a model, or start a target run.
It **does** import configured Python classes and invoke constructors, so only
inspect trusted profiles. Disabled entries are shown in the profile but omitted
from `active_mechanisms` and are not imported or validated. The output describes
the profile before CLI overrides and provider/environment defaults; see the
[configuration reference](configuration.md) for precedence.
