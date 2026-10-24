# Setup and your first custom mechanism

This walkthrough starts with a fresh checkout, tests a custom mechanism without
Docker or model calls, and then shows how to run it against your own authorized
target. Run commands from the repository root unless a step says otherwise.

## 1. Install the Python environment

Install Python 3.13 or newer, then:

```bash
git clone https://github.com/tsirolnik/fantasma.git
cd fantasma
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. The remaining
shell examples use Bash/zsh. In a new terminal, activate the environment again.

The installation adds `fantasma` to the active environment. Check it with
`fantasma --version` and `fantasma --help`. Bare `fantasma` opens the
interactive pentester. Keep the environment activated when using the command.

You can now inspect profiles and test mechanism logic. These steps do not need
LLM credentials, a running Docker daemon, or a target service:

```bash
fantasma profiles mechanisms
fantasma profiles inspect examples/mechanisms/profile.toml
```

The inspector prints JSON with resolved profile values, active mechanisms and
hook names. It does not start an investigation. It imports and constructs
extensions, so use profiles you trust.

## 2. Keep your extension outside the repository

Copy the working example to a sibling directory:

```bash
mkdir -p ../my-fantasma-profile
cp examples/mechanisms/coverage.py ../my-fantasma-profile/
cp examples/mechanisms/profile.toml ../my-fantasma-profile/
fantasma profiles inspect ../my-fantasma-profile/profile.toml
```

The two files have different jobs:

- `coverage.py` implements `CoverageBonus`, a subclass of `Mechanism`. It tracks
  which targets each worker has covered and rewards the first accepted finding
  on each target.
- `profile.toml` selects the class and passes a `bonus` argument to its constructor.

The relevant configuration is:

```toml
[[mechanisms.items]]
class = "./coverage.py:CoverageBonus"
bonus = 7
```

`./coverage.py` resolves beside the profile, regardless of your shell's current
directory. `CoverageBonus` is the class inside that file. No registry edit or
package installation is needed for this standalone extension.

## 3. Exercise its behavior without an LLM

From the repository root, run:

```bash
python - <<'PY'
from types import SimpleNamespace
from profiles.loader import load_profile
from memory.mechanisms import build_mechanisms

profile = load_profile('../my-fantasma-profile/profile.toml')
mechanism, = build_mechanisms(profile.mechanisms)
finding = SimpleNamespace(target='authorized.test')

first = mechanism.on_submit('worker-1', finding, True, None)
repeat = mechanism.on_submit('worker-1', finding, True, None)
other_worker = mechanism.on_submit('worker-2', finding, True, None)
assert (first.score_delta, repeat.score_delta, other_worker.score_delta) == (7, 0, 7)
print('PASS: first finding +7, repeat +0, another worker +7')
PY
```

This calls the hook directly with a minimal finding object. It does not change
shared-memory scores: during a real run, SharedMemory applies the returned
`MechanismEffect`. `accepted=True` means admitted to shared memory, not confirmed
by independent validation.

Edit `coverage.py` to implement your rule. Constructor parameters belong in TOML;
stateful logic belongs in Python. Restart a running process after editing an
extension because imported modules are cached.

## 4. Add conditions

Copy the conditional example into the same directory:

```bash
cp examples/mechanisms/conditional.toml ../my-fantasma-profile/
fantasma profiles inspect ../my-fantasma-profile/conditional.toml
```

It sets a bonus of 12 and only invokes `on_submit` when a finding is accepted,
high or critical severity, and has `finding.target == "authorized.test"`:

```toml
[mechanisms.items.when]
hooks = ["on_submit"]
accepted = true
severities = ["high", "critical"]
targets = ["authorized.test"]
```

Replace that target with the exact value appropriate to your run, or remove
`targets` to allow all submissions that match the other conditions. This filter
does not define or expand authorized scope. The example also keeps an idle-tax
entry disabled with `enabled = false`.

See the [mechanism guide](mechanisms.md) for all hooks, agent filters, composition
rules and examples of state management.

## 5. Prepare for a real run

Install and start Docker with Compose support. Verify it and build the tool image:

```bash
docker info
docker compose version
docker build -t bugbounty-tools:latest -f docker/Dockerfile.tools .
python -m playwright install chromium
```

On Linux, if Playwright reports missing system libraries, use
`python -m playwright install --with-deps chromium` (system package installation
may require elevated permissions).

Create a local configuration file if you do not already have one:

```bash
cp -n .env.example .env
```

Edit `.env` and set:

```dotenv
ANTHROPIC_API_KEY=YOUR_API_KEY
CLAUDE_MODEL=YOUR_AVAILABLE_CLAUDE_MODEL_ID
FALKORDB_ENABLED=false
```

Replace both placeholders with your credentials and an available model ID.
Disabling the optional graph simplifies the first run. The command below selects
Claude explicitly for all three role flags; it does not rely on `LLM_PROVIDER`
to configure every worker. Other providers and their settings are described in
the [README](../README.md#install) and [configuration reference](configuration.md).

## 6. Run the custom profile

Replace the target placeholder with a system you own or are authorized to test:

```bash
fantasma full --profile ../my-fantasma-profile/profile.toml \
  --targets YOUR_AUTHORIZED_DOMAIN \
  --director-llm claude --worker-llm claude --analyzer-llm claude \
  --budget 1 --web
```

This starts a real investigation and makes paid model calls. The budget is an
estimate and can overshoot; it is not a guarantee of completion or findings.
To use the filters from step 4, select `conditional.toml` instead.

Open the dashboard address printed in the terminal and enter its access token.
Inspect the activity and resulting findings; a custom bonus only appears after
its event occurs. Logs are written under `data/logs/`; reports use `OUTPUT_DIR`
(default `./output`). Review findings before relying on them.

For a managed local target instead, follow the [benchmark walkthrough](../README.md#start-with-the-local-lab).
That runner has its own configuration; the command above is the custom-profile
path. Do not assume the benchmark automatically loads your profile.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `No module named ...` | Activate `.venv`, install requirements, and run from the repo root; package extensions must be importable in this environment |
| Missing Python extension file | The `.py` path is relative to the TOML file; keep the copied files together |
| Constructor error | Match TOML parameter names to `__init__`; `name`, `class`, `enabled`, `when` are reserved |
| No custom bonus | Inspect `active_hooks` and conditions; check the target/agent ID matches and that the relevant event occurred |
| Different mechanisms than expected | `--mechanisms` replaces the profile list; omit it when using custom entries |
| Model/authentication failure | Check `.env`, role flags, model availability and provider access |
| Docker connection failure | Start the daemon and confirm `docker info` succeeds |
| Profile settings seem ignored | Use swarm mode; lab does not consume every profile component |

Continue with the [configuration reference](configuration.md),
[mechanism API guide](mechanisms.md), and [architecture](architecture.md).
