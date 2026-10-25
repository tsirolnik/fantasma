# CLI reference

Generated from [main.py](../../main.py) and [commands/](../../commands/).
Regenerate with `python scripts/generate_reference.py`. Profile and runtime fallback
rules are in [configuration](../configuration.md). Global flags precede the command.

```bash
fantasma --verbose full --profile bugbounty --targets YOUR_AUTHORIZED_DOMAIN
```

## Global options

```text
usage: fantasma [-h] [--version] [--llm {claude,gemini,ollama,openai,vertex}]
                [--output-dir OUTPUT_DIR] [--verbose]
                [--openai-base-url OPENAI_BASE_URL]
                [--openai-api-key OPENAI_API_KEY]
                [--vertex-project VERTEX_PROJECT]
                [--vertex-region VERTEX_REGION] [--vertex-model VERTEX_MODEL]
                [--gemini-api-key GEMINI_API_KEY]
                [--gemini-model GEMINI_MODEL]
                {discover,scan,fuzz,full,report,costs,learning,history,record-outcome,validate,graph-sync,profiles,sessions} ...

Configurable AI-assisted pentesting and security research

positional arguments:
  {discover,scan,fuzz,full,report,costs,learning,history,record-outcome,validate,graph-sync,profiles,sessions}
    discover            Find high-ROI bug bounty programs
    scan                Scan a specific target
    fuzz                Fuzz a binary target (experimental)
    full                Run the full pipeline
    report              Generate reports from findings JSON
    costs               Show session cost breakdown
    learning            Show learning memory insights
    history             Show scan history for a target
    record-outcome      Record a finding submission outcome
    validate            Re-validate findings from a past scan
    graph-sync          Rebuild FalkorDB graph from SQLite data
    profiles            Inspect profiles and list mechanisms without starting
                        a run
    sessions            List saved interactive sessions

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --llm {claude,gemini,ollama,openai,vertex}
                        LLM provider to use
  --output-dir OUTPUT_DIR
                        Output directory for reports
  --verbose, -v         Verbose logging
  --openai-base-url OPENAI_BASE_URL
                        Base URL for OpenAI-compatible API (Novita, vLLM,
                        etc.)
  --openai-api-key OPENAI_API_KEY
                        API key for OpenAI-compatible provider
  --vertex-project VERTEX_PROJECT
                        GCP project ID for Vertex AI
  --vertex-region VERTEX_REGION
                        GCP region for Vertex AI (default: global)
  --vertex-model VERTEX_MODEL
                        Vertex AI model ID (e.g. claude-sonnet-4-6, claude-
                        opus-4-6, claude-haiku-4-5)
  --gemini-api-key GEMINI_API_KEY
                        Google Gemini API key
  --gemini-model GEMINI_MODEL
                        Gemini model ID (e.g. gemini-2.5-flash-preview-05-20,
                        gemini-2.5-pro-preview-06-05)
```

## discover

Source: [commands/discover.py](../../commands/discover.py).

```text
usage: fantasma discover [-h] [--top TOP] [--min-bounty MIN_BOUNTY]

options:
  -h, --help            show this help message and exit
  --top TOP             Number of top programs to show
  --min-bounty MIN_BOUNTY
                        Minimum bounty threshold in USD
```

## scan

Source: [commands/scan.py](../../commands/scan.py).

```text
usage: fantasma scan [-h] --target TARGET

options:
  -h, --help       show this help message and exit
  --target TARGET  Target domain to scan
```

## fuzz

Source: [commands/fuzz.py](../../commands/fuzz.py).

```text
usage: fantasma fuzz [-h] --binary BINARY --corpus CORPUS [--timeout TIMEOUT]
                     [--output OUTPUT]

options:
  -h, --help         show this help message and exit
  --binary BINARY    Path to target binary
  --corpus CORPUS    Path to seed corpus directory
  --timeout TIMEOUT  Fuzzing timeout in seconds
  --output OUTPUT    Fuzzing output directory
```

## full

Source: [commands/full.py](../../commands/full.py).

```text
usage: fantasma full [-h] [--resume-session [ID]] [--diagnostics]
                     [--interaction {batch,terminal,web}] [--top TOP]
                     [--min-bounty MIN_BOUNTY] [--budget BUDGET] [--auto]
                     [--dry-run]
                     [--director-llm {claude,gemini,ollama,openai,vertex}]
                     [--director-model DIRECTOR_MODEL]
                     [--worker-llm {claude,gemini,ollama,openai,vertex}]
                     [--worker-model WORKER_MODEL]
                     [--analyzer-llm {claude,gemini,ollama,openai,vertex}]
                     [--analyzer-model ANALYZER_MODEL] [--pause-at PAUSE_AT]
                     [--cli-tools] [--mode {pentester,interactive,swarm,lab}]
                     [--swarm-agents SWARM_AGENTS] [--num-teams NUM_TEAMS]
                     [--mechanisms MECHANISMS] [--profile PROFILE]
                     [--ollama-per-agent] [--domain-triage]
                     [--no-auth-testing] [--no-browser] [--compact]
                     [--scope-pdf-only] [--keep-recent KEEP_RECENT]
                     [--max-tokens MAX_TOKENS] [--generous-context]
                     [--targets TARGETS] [--program-url PROGRAM_URL] [--web]
                     [--web-port WEB_PORT] [--resume]
                     [--resume-scan NAME_OR_ID] [--list-checkpoints]

options:
  -h, --help            show this help message and exit
  --resume-session [ID]
                        Restore an interactive session (omit ID for latest);
                        waits for your next instruction
  --diagnostics         Show interactive console diagnostics (also enabled by
                        global --verbose)
  --interaction {batch,terminal,web}
                        Override terminal/batch interaction; legacy web
                        enables the dashboard
  --top TOP             Number of top programs to target
  --min-bounty MIN_BOUNTY
                        Minimum bounty threshold in USD
  --budget BUDGET       Maximum session budget in USD
  --auto                Run fully unattended (no human checkpoints)
  --dry-run             Run without LLM calls (tests message flow)
  --director-llm {claude,gemini,ollama,openai,vertex}
                        LLM provider for the Director agent
  --director-model DIRECTOR_MODEL
                        Model override for Director
  --worker-llm {claude,gemini,ollama,openai,vertex}
                        Default LLM provider for worker agents
  --worker-model WORKER_MODEL
                        Model override for workers
  --analyzer-llm {claude,gemini,ollama,openai,vertex}
                        LLM provider for the analyzer agent (overrides
                        --worker-llm)
  --analyzer-model ANALYZER_MODEL
                        Model override for analyzer
  --pause-at PAUSE_AT   Comma-separated phases to pause at (e.g.
                        discover,analyze)
  --cli-tools           Use CLI-style single 'run' tool to reduce context
                        token usage (~45K tokens saved per run)
  --mode {pentester,interactive,swarm,lab}
                        Execution engine: pentester (interactive is a
                        compatibility alias), or swarm (N pen-testers across
                        multiple competing teams — set --num-teams > 1 for
                        that, not a separate mode value) or 'lab' (sequential
                        specialized agents)
  --swarm-agents SWARM_AGENTS
                        Number of concurrent pen-tester agents per team (swarm
                        mode, default: 3, or the --profile's
                        [topology].agents_per_team)
  --num-teams NUM_TEAMS
                        Number of competing teams — multi-team competition is
                        just this > 1 on the same engine, not a separate mode
                        (default: 1, or the --profile's [topology].num_teams)
  --mechanisms MECHANISMS
                        Comma-separated game-theory mechanisms, additive on
                        top of the base scoring (e.g.
                        'first_finder_bonus,duplicate_escalation'); default:
                        none (unmodified baseline). Overrides the --profile's
                        mechanisms if both are given.
  --profile PROFILE     Run configuration: methodology, target source,
                        scoring, mechanisms, topology. Built-in name
                        ('interactive', 'lab', 'bugbounty', 'redteam') or a
                        path to a .toml file (see profiles/bugbounty.toml for
                        the schema). Default: interactive profile, selected
                        from profiles/interactive.toml. Explicit CLI flags
                        (--mode, --mechanisms, etc.) override the profile.
  --ollama-per-agent    Spawn a separate Ollama instance per swarm agent (for
                        models that don't support OLLAMA_NUM_PARALLEL)
  --domain-triage       Enable smart domain triage — Director fingerprints and
                        ranks domains before agents start
  --no-auth-testing     Disable authenticated testing (on by default)
  --no-browser          Disable the Playwright browser pool to cut ~0.5-1GB
                        RAM (agents keep HTTP/docker/code tools)
  --compact             Use compact system prompt (~60% smaller) to save
                        tokens
  --scope-pdf-only      Load program scope from PDFs in data/program_scopes/
                        only — skip browser scraping
  --keep-recent KEEP_RECENT
                        Number of recent message pairs to keep in agent
                        context (default 10)
  --max-tokens MAX_TOKENS
                        Max tokens the LLM may generate per call (default
                        4096; bump for reasoning models like GLM)
  --generous-context    Hold several times more verbatim agent history/tool-
                        results before compressing. Safe ONLY for a REMOTE API
                        provider (Novita/OpenAI-
                        compatible/Claude/Gemini/Vertex) with a large context
                        window — a LOCAL Ollama model shares host RAM/VRAM
                        with the agent process and can OOM under this. Must be
                        set explicitly; never inferred from the provider.
  --targets TARGETS     Comma-separated target domains (skips discovery, e.g.
                        'example.com,api.example.com')
  --program-url PROGRAM_URL
                        Bug bounty program URL (used with --targets for scope
                        enrichment)
  --web                 Launch a real-time web dashboard at
                        http://127.0.0.1:8080
  --web-port WEB_PORT   Port for the web dashboard (default: 8080)
  --resume              Resume from the most recent checkpoint
  --resume-scan NAME_OR_ID
                        Resume a specific run by name (e.g. bold-falcon-42) or
                        scan ID
  --list-checkpoints    Show available checkpoints and exit
```

## report

Source: [commands/report.py](../../commands/report.py).

```text
usage: fantasma report [-h] --findings FINDINGS

options:
  -h, --help           show this help message and exit
  --findings FINDINGS  Path to findings JSON file
```

## costs

Source: [commands/costs.py](../../commands/costs.py).

```text
usage: fantasma costs [-h]

options:
  -h, --help  show this help message and exit
```

## learning

Source: [commands/learning.py](../../commands/learning.py).

```text
usage: fantasma learning [-h]

options:
  -h, --help  show this help message and exit
```

## history

Source: [commands/history.py](../../commands/history.py).

```text
usage: fantasma history [-h] [--target TARGET]

options:
  -h, --help       show this help message and exit
  --target TARGET  Target to show history for
```

## record-outcome

Source: [commands/record_outcome.py](../../commands/record_outcome.py).

```text
usage: fantasma record-outcome [-h] --finding-id FINDING_ID [--accepted]
                               [--bounty BOUNTY]

options:
  -h, --help            show this help message and exit
  --finding-id FINDING_ID
                        Finding ID from database
  --accepted            Was the finding accepted?
  --bounty BOUNTY       Actual bounty received in USD
```

## validate

Source: [commands/validate.py](../../commands/validate.py).

```text
usage: fantasma validate [-h] [--scan SCAN_ID] [--budget BUDGET]
                         [--director-llm {claude,gemini,ollama,openai,vertex}]
                         [--director-model DIRECTOR_MODEL]
                         [--worker-llm {claude,gemini,ollama,openai,vertex}]
                         [--worker-model WORKER_MODEL] [--web]
                         [--web-port WEB_PORT]

options:
  -h, --help            show this help message and exit
  --scan SCAN_ID        Scan ID to validate findings from (default: latest)
  --budget BUDGET       Budget for validation run in USD (default: $5)
  --director-llm {claude,gemini,ollama,openai,vertex}
                        LLM provider for the Director (oversees validator)
  --director-model DIRECTOR_MODEL
                        Model override for Director
  --worker-llm {claude,gemini,ollama,openai,vertex}
                        LLM provider for the validator agent
  --worker-model WORKER_MODEL
                        Model override for validator
  --web                 Launch a real-time web dashboard
  --web-port WEB_PORT   Port for the web dashboard (default: 8080)
```

## graph-sync

Source: [commands/graph_sync.py](../../commands/graph_sync.py).

```text
usage: fantasma graph-sync [-h]

options:
  -h, --help  show this help message and exit
```

## profiles

Source: [commands/profiles.py](../../commands/profiles.py).

```text
usage: fantasma profiles [-h] {inspect,mechanisms} [profile]

positional arguments:
  {inspect,mechanisms}
  profile               Profile name or TOML path for inspect

options:
  -h, --help            show this help message and exit
```

## sessions

Source: [commands/sessions.py](../../commands/sessions.py).

```text
usage: fantasma sessions [-h]

options:
  -h, --help  show this help message and exit
```

## Offline profile tools

```bash
fantasma profiles mechanisms
fantasma profiles inspect NAME_OR_PATH
```

These import trusted extensions but do not start a run. See [mechanisms](../mechanisms.md).
