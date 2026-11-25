"""The CLI's command registry.

main.py has exactly one hardcoded thing: a generic dispatcher that builds an
argparse subcommand from a Command's declared Arg list and calls its handler.
Everything else — what commands exist, what flags they take, what they do —
lives in its own module under commands/ and self-registers via @register.
Adding a new command (or changing one's flags) never touches main.py.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Arg:
    """One argparse argument — flags plus whatever kwargs add_argument takes."""
    flags: tuple[str, ...]
    kwargs: dict[str, Any] = field(default_factory=dict)

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(*self.flags, **self.kwargs)


@dataclass
class Command:
    name: str
    help: str
    args: list[Arg] = field(default_factory=list)
    handler: Callable[[argparse.Namespace, Any], Awaitable[None]] | None = None


REGISTRY: dict[str, Command] = {}


def register(name: str, help: str, args: list[Arg] | None = None):
    """Decorator: registers `fn` as the handler for CLI subcommand `name`."""
    def deco(fn: Callable[[argparse.Namespace, Any], Awaitable[None]]):
        REGISTRY[name] = Command(name=name, help=help, args=list(args or []), handler=fn)
        return fn
    return deco
