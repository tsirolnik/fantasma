"""Self-registering CLI commands — import this package to populate the
registry (commands.base.REGISTRY), then build/dispatch generically.

To add a command: drop a module in here that calls @register(...) at import
time, then add it to this list. Nothing outside this package needs to change.
"""
from . import (  # noqa: F401
    discover,
    scan,
    fuzz,
    full,
    report,
    costs,
    learning,
    history,
    record_outcome,
    validate,
    graph_sync,
    profiles,
)
