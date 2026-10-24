"""Render model replies while leaving transcript contents as Markdown text."""
import re
import sys


class MarkdownMessage(str):
    """A reply whose terminal representation should render Markdown."""


def terminal_write(text):
    # Strip terminal control sequences from content before rendering it.
    clean = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', str(text))
    clean = ''.join(c for c in clean if c.isprintable() or c in '\n\t')
    if isinstance(text, MarkdownMessage) and sys.stdout.isatty():
        from rich.console import Console
        from rich.markdown import Markdown
        Console(file=sys.stdout).print(Markdown(clean))
    else:
        print(clean)
