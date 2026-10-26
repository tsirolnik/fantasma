# Contributing to Fantasma

Contributions to tool integrations, workflows, documentation, and reproducible
agent evaluations are welcome. For a substantial change, open an issue describing
the problem and proposed behavior before implementing it. For a small fix, send a
pull request directly.

## Development setup

Use Python 3.13 on Linux or macOS. From your checkout:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e . build twine
python -m playwright install chromium
python -m unittest discover -s tests
python scripts/generate_reference.py --check
```

Tests use mocks, a local MCP demo process, and Chromium with intercepted local
search fixtures. Install Chromium once using the command above. Tests do not
require model API keys, Docker, or external scan targets. Runtime setup is described
in [Getting started](docs/getting-started.md).

## Before submitting a pull request

Explain the problem, resulting behavior, and how you verified the change. Add
regression coverage for behavior changes and update affected documentation.
If CLI options, settings, or finding models change, regenerate the references:

```sh
python scripts/generate_reference.py
python -m build
python -m twine check dist/*
python scripts/check_wheel.py dist/fantasma-0.1.0-py3-none-any.whl
git diff --check
```

CI runs tests, documentation checks, distribution builds and wheel smoke tests
on Linux and macOS. Windows terminal interaction is not currently supported.

Keep credentials, personal targets, logs, generated reports, and model traces
out of commits. Use synthetic fixtures for tests. Test security functionality
only against systems you own or have explicit permission to assess.

## Extensions

Start with the [mechanism guide](docs/mechanisms.md),
[extension reference](docs/reference/extensions.md), or [MCP guide](docs/mcp.md).
Prefer documented extension points when the behavior does not need a core change.

## Security reports

Follow [SECURITY.md](SECURITY.md) for vulnerabilities in Fantasma. Do not publish
sensitive reproduction details in an issue. Contributions are distributed under
the project's [MIT license](LICENSE).
