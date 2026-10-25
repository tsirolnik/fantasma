"""Compact activity status; tool details come only from execution callbacks."""
from collections import Counter
import time
import json
import re
import hashlib
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
        self.calls = []
        self.next_id = 1
        self.expanded = False
        self.live_inspector = False
        self.inspected_id = None
        self.inspect_offset = 0
        self.phase = "Idle"

    def begin(self):
        self.started = time.monotonic()
        self.tools.clear()
        self.phase = "Preparing agent"

    def finish(self):
        self.started = None
        self.tools.clear()
        self.phase = "Idle"
        for call in self.calls:
            if call["status"] == "running":
                call["status"] = "interrupted"
                call["duration"] = time.monotonic() - call["started"]

    def status_line(self):
        if self.started is None:
            return ' 👻 Ready · Ctrl+O / /peek to inspect'
        elapsed = max(0, time.monotonic() - self.started)
        frame = FRAMES[int(elapsed * 10) % len(FRAMES)]
        if self.tools:
            names = ', '.join(self.tools)
            action = f'Running {names}'
        else:
            action = VERBS[int(elapsed // 4) % len(VERBS)] + '…'
        return f' {frame} 👻 {action} · {elapsed:.0f}s · Ctrl+O inspect · /stop'

    def toolbar(self):
        status = self.status_line()
        if not (self.expanded and self.live_inspector):
            return status
        import shutil
        size = shutil.get_terminal_size((80, 24))
        height = max(1, min(12, size.lines - 5))
        lines = self.peek(self.inspected_id).splitlines()
        self.inspect_offset = min(self.inspect_offset, max(0, len(lines) - height))
        visible = lines[self.inspect_offset:self.inspect_offset + height]
        panel = [line[:max(1, size.columns - 1)] for line in visible]
        return status + '\n Ctrl+O / Esc: close · PgUp/PgDn: scroll\n' + '\n'.join(panel)

    def set_inspection(self, enabled, identifier=None):
        self.expanded = enabled
        self.inspected_id = identifier
        self.inspect_offset = 0
        if not self.live_inspector:
            self.write(self.peek(identifier) if enabled else 'Inspector closed. Compact tool output restored.')

    def tool_started(self, agent, name, params):
        name = clean_label(name)
        self.tools[name] += 1
        self.phase = "Executing tools"
        self.calls.append({"id": self.next_id, "name": name, "status": "running",
                           "signature": self.signature(name, params),
                           "input": self.preview(params), "output": "(pending)",
                           "started": time.monotonic(), "duration": None})
        self.next_id += 1
        # Keep running calls, plus the most recent completed calls.
        completed = [c for c in self.calls if c["status"] != "running"][-100:]
        self.calls = sorted(completed + [c for c in self.calls if c["status"] == "running"], key=lambda c: c["id"])
        if self.expanded and not self.live_inspector:
            self.write(self.format_call(self.calls[-1]))
        self.write(f'  ↳ #{self.calls[-1]["id"]} {tool_label(name, params)}')

    def tool_finished(self, call):
        name = clean_label(call.tool_name)
        failed = bool(call.error) or (isinstance(call.output, dict) and bool(call.output.get('error')))
        icon, result = ('✗', 'failed') if failed else ('✓', 'done')
        signature = self.signature(name, call.input)
        entry = next((c for c in self.calls if c["status"] == "running" and c["signature"] == signature), None)
        if entry is None:
            self.tool_started("", name, call.input)
            entry = self.calls[-1]
        entry.update(status=result, duration=call.duration_ms / 1000,
                     output=self.preview(call.error if call.error else call.output))
        self.tools = Counter(c["name"] for c in self.calls if c["status"] == "running")
        self.write(f'  {icon} #{entry["id"]} {name} · {result} · {call.duration_ms / 1000:.1f}s')
        if self.expanded and not self.live_inspector:
            self.write(self.format_call(entry))
        if not self.tools:
            self.phase = "Processing tool results"
        completed = [c for c in self.calls if c["status"] != "running"][-100:]
        self.calls = sorted(completed + [c for c in self.calls if c["status"] == "running"], key=lambda c: c["id"])


    @staticmethod
    def signature(name, params):
        return hashlib.sha256((name + json.dumps(params, sort_keys=True, default=str)).encode()).hexdigest()

    @staticmethod
    def preview(value):
        def redact(item):
            if isinstance(item, dict):
                return {str(k): "[redacted]" if any(word in str(k).lower() for word in
                        ("password", "secret", "token", "authorization", "cookie", "api_key", "apikey"))
                        else redact(v) for k, v in item.items()}
            if isinstance(item, list):
                return [redact(v) for v in item]
            return item
        text = json.dumps(redact(value), ensure_ascii=False, indent=2, default=str)
        text = re.sub(r'(?i)(bearer\s+)[^\s"\\]+', r'\1[redacted]', text)
        text = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', text)
        text = re.sub(r'(?i)([?&](?:token|api_key|key|secret|password)=)[^&\s"\\]+', r'\1[redacted]', text)
        text = ''.join(c for c in text if c.isprintable() or c == '\n')
        return text[:4000] + ('\n… [preview truncated]' if len(text) > 4000 else '')

    def format_call(self, call):
        elapsed = call["duration"] if call["duration"] is not None else time.monotonic() - call["started"]
        return (f'#{call["id"]} {call["name"]} · {call["status"]} · {elapsed:.1f}s\n'
                f'  Input: {call["input"]}\n  Result: {call["output"]}')

    def peek(self, identifier=None):
        if identifier is not None:
            calls = [c for c in self.calls if c["id"] == identifier]
            if not calls:
                return f'No retained tool call #{identifier}.'
        else:
            calls = sorted([c for c in self.calls if c["status"] != "running"][-5:] +
                           [c for c in self.calls if c["status"] == "running"], key=lambda c: c["id"])
        heading = f'Activity: {self.phase}. Expanded tool output: {"on" if self.expanded else "off"}.'
        return heading + '\n' + ('\n\n'.join(self.format_call(c) for c in calls) if calls else 'No tool calls yet.')

    def toggle(self):
        self.set_inspection(not self.expanded)

    def llm_started(self, agent, model, messages, tools):
        self.phase = f'Waiting for model {clean_label(model)} ({messages} messages, {tools} tools available)'

    def llm_finished(self, *args):
        self.phase = "Processing model response"
