"""Compact activity status; tool details come only from execution callbacks."""
from collections import Counter
import time
from urllib.parse import urlsplit

VERBS = (
    "Consulting the packet spirits", "Untangling the spaghetti",
    "Negotiating with the bits", "Summoning a hypothesis",
    "Connecting the suspicious dots", "Polishing the tinfoil hat",
    "Following the white rabbit", "Asking the rubber duck",
)
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def clean_label(value):
    return ''.join(c for c in str(value) if c.isprintable())[:100]


def tool_label(name, params):
    label = clean_label(name)
    # Show only a URL's host, never credentials, query strings or request bodies.
    url = params.get('url') if isinstance(params, dict) else None
    if isinstance(url, str):
        try:
            host = urlsplit(url).hostname
            if host:
                label += ' · ' + clean_label(host)
        except ValueError:
            pass
    return label


class TerminalActivity:
    def __init__(self, write):
        self.write = write
        self.started = None
        self.tools = Counter()

    def begin(self):
        self.started = time.monotonic()
        self.tools.clear()

    def finish(self):
        self.started = None
        self.tools.clear()

    def toolbar(self):
        if self.started is None:
            return ' 👻 Ready · /help for commands'
        elapsed = max(0, time.monotonic() - self.started)
        frame = FRAMES[int(elapsed * 10) % len(FRAMES)]
        if self.tools:
            names = ', '.join(self.tools)
            action = f'Running {names}'
        else:
            action = VERBS[int(elapsed // 4) % len(VERBS)] + '…'
        return f' {frame} 👻 {action} · {elapsed:.0f}s · /stop to cancel'

    def tool_started(self, agent, name, params):
        name = clean_label(name)
        self.tools[name] += 1
        self.write(f'  ↳ {tool_label(name, params)}')

    def tool_finished(self, call):
        name = clean_label(call.tool_name)
        if self.tools[name] > 1:
            self.tools[name] -= 1
        else:
            self.tools.pop(name, None)
        failed = bool(call.error) or (isinstance(call.output, dict) and bool(call.output.get('error')))
        icon, result = ('✗', 'failed') if failed else ('✓', 'done')
        self.write(f'  {icon} {name} · {result} · {call.duration_ms / 1000:.1f}s')
