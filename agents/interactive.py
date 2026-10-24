"""User-led terminal session using the existing pentester tools and guardrails."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys

from agents.messaging import MessageBus, TaskAssignment
from agents.pentester import PenTesterAgent
from agents.llm_config import create_llm_for_agent
from utils.terminal_markdown import MarkdownMessage, terminal_write
from config import get_settings
from cost.tracker import CostTracker
from memory.shared import SharedMemory


class InteractivePentester(PenTesterAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.allow_self_conclude = True
        self.context_mode = False
        self.history: list[dict] = []

    def _build_initial_messages(self, assignment):
        # Keep tool results from earlier turns, not just the final prose answer.
        return [*self.history, {"role": "user", "content": self._build_user_message(assignment)}]

    def _build_user_message(self, assignment):
        return (f"{self.system_prompt}\n\nConfigured scope: {', '.join(self.in_scope) or '(none; tools disabled)'}"
                f"\n\nUser request:\n{assignment.context}")

    def _check_scope_claim(self, tool_name, params):
        if not self._allowed_domains:
            return "No target scope configured. Ask the user to set /scope before using tools."
        return super()._check_scope_claim(tool_name, params)

    async def execute(self, assignment):
        self._cancel_reason = ""
        result = await super().execute(assignment)
        if result.success and self.last_trace:
            self.history = list(self.last_trace.messages)
            if self.last_trace.final_answer:
                self.history.append({"role": "assistant", "content": self.last_trace.final_answer})
        return result


HELP = """Enter a question or investigation request. No work starts until you send one.
/scope HOST[,HOST]  Set authorized hosts (only while idle)
/status            Show scope, spend and finding count
/peek [ID|on|off]  Inspect tool inputs/results; Ctrl+O toggles live details
/steer TEXT        Queue guidance for the next model step
/interrupt TEXT    Cancel current work, then start the replacement request
/stop              Cancel current work and remain in the session
/quit              Cancel current work and close the session
/help              Show this help
While work runs, new text redirects it before a subsequent model call.
"""


async def terminal_lines(session=None):
    """Read terminal or piped lines without an uncancellable input thread."""
    if sys.stdin.isatty() and sys.stdout.isatty() and getattr(session, "live_terminal", True):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style
        from prompt_toolkit.key_binding import KeyBindings
        keys = KeyBindings()
        @keys.add('c-o')
        def inspect_tools(event):
            if session:
                session.activity.toggle()
        prompt = PromptSession(
            key_bindings=keys,
            bottom_toolbar=session.activity.toolbar if session else None,
            refresh_interval=0.1,
            style=Style.from_dict({"bottom-toolbar": "bg:#101b24 #78dcca"}),
        )
        with patch_stdout(raw=True):
            while True:
                try:
                    yield await prompt.prompt_async('you> ')
                except EOFError:
                    return
                except KeyboardInterrupt:
                    yield '/stop'
        return

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    fd = sys.stdin.fileno()

    def readable():
        line = sys.stdin.readline()
        queue.put_nowait(line)
        if not line:
            loop.remove_reader(fd)

    loop.add_reader(fd, readable)
    try:
        while True:
            if sys.stdin.isatty() and not (session and session.task and not session.task.done()):
                print("you> ", end="", flush=True)
            line = await queue.get()
            if not line:
                return
            yield line.rstrip('\n')
    finally:
        loop.remove_reader(fd)


class InteractiveSession:
    def __init__(self, profile, llm_config, args, mechanisms, write=print):
        self.profile = profile
        self.llm_config = llm_config
        self.args = args
        self.write = write
        self.memory = SharedMemory(scoring=profile.scoring, coordination=profile.coordination)
        self.memory.configure_mechanisms(mechanisms)
        self.cost = CostTracker()
        self.scope = self.parse_scope(getattr(args, 'targets', None) or '')
        self.agent = None
        self.browser = None
        self.task = None
        from utils.terminal_activity import TerminalActivity
        self.activity = TerminalActivity(lambda text: self.write(text))
        self.live_terminal = False
        self.pending_steering = []
        self.diagnostics = (getattr(args, "diagnostics", False) or
                            getattr(args, "verbose", False) or profile.interaction.diagnostics)
        self._input_lock = asyncio.Lock()
        self.budget = args.budget if args.budget is not None else get_settings().session_budget

    @staticmethod
    def parse_scope(text):
        from urllib.parse import urlsplit
        hosts = []
        for value in text.split(','):
            value = value.strip().lower()
            if not value:
                continue
            if any(c.isspace() for c in value) or '/' in value or '@' in value:
                raise ValueError('Scope accepts hostnames (optionally *.example.test), not URLs or paths')
            host = urlsplit('//' + value).hostname
            if not host:
                raise ValueError(f'Invalid scope host: {value}')
            hosts.append(host)
        return sorted(set(hosts))

    async def ensure_agent(self):
        if self.agent is not None:
            return
        if self.browser is not None:
            await self.browser.stop()
            self.browser = None
        if getattr(self.args, 'dry_run', False):
            raise ValueError('Interactive --dry-run supports session controls only; no model is created')
        llm = create_llm_for_agent('scanner', self.llm_config)
        if llm is None or not llm.supports_tool_use:
            raise ValueError('Configure a tool-capable worker provider with --worker-llm / --worker-model')
        settings = get_settings()
        browser_session = None
        if settings.enable_browser:
            from utils.browser import BrowserPool
            self.browser = BrowserPool()
            await self.browser.start()
            browser_session = await self.browser.create_session('pentester')
        self.agent = InteractivePentester(
            agent_id='pentester', bus=MessageBus(), llm=llm,
            shared_memory=self.memory, browser_session=browser_session,
            methodology=self.profile.methodology, cost_tracker=self.cost,
            max_iterations=self.profile.interaction.max_iterations,
            max_total_tool_calls=self.profile.interaction.max_tool_calls,
        )
        self.agent.session_budget = self.budget
        self.agent.on_tool_start = self.activity.tool_started
        self.agent.on_tool_call = self.activity.tool_finished
        self.agent.on_llm_start = self.activity.llm_started
        self.agent.on_llm_call = self.activity.llm_finished
        self.set_scope(self.scope)

    def set_scope(self, scope):
        self.scope = scope
        self.memory.allowed_domains = set(scope)
        if self.agent:
            self.agent.in_scope = list(scope)
            self.agent._allowed_domains = set(scope)

    async def ask(self, text):
        try:
            await self.ensure_agent()
            for directive in self.pending_steering:
                self.agent.inject_directive(directive)
            self.pending_steering.clear()
            result = await self.agent.execute(TaskAssignment(
                agent_name='pentester', task_type='interactive', context=text))
            if result.success:
                self.write(MarkdownMessage(result.reflection or json.dumps(result.data, default=str)))
            else:
                self.write(f'Work stopped: {result.error}')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.write(f'Session error: {exc}')
        finally:
            self.pending_steering.clear()
            self.activity.finish()
        if not self.live_terminal and sys.stdin.isatty() and sys.stdout.isatty():
            print('you> ', end='', flush=True)

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.task = None
        self.pending_steering.clear()
        self.activity.finish()
        if self.agent:
            self.agent._sticky_directives.clear()
            while not self.agent._directive_queue.empty():
                self.agent._directive_queue.get_nowait()

    def start_request(self, text):
        self.activity.begin()
        if not self.live_terminal:
            self.write('Thinking… 👻 Consulting the packet spirits. (/stop to cancel)')
        self.task = asyncio.create_task(self.ask(text))

    def steer(self, text):
        if self.agent:
            self.agent.inject_directive(text)
        else:
            self.pending_steering.append(text)
        self.write('Steering queued for the next model step; the current tool continues.')

    async def handle(self, line):
        async with self._input_lock:
            return await self._handle(line)

    async def _handle(self, line):
        text = line.strip()
        if not text:
            return True
        if text in ('/quit', '/exit'):
            await self.stop()
            return False
        if text == '/stop':
            await self.stop()
            self.write('Stopped. Ready for your next request.')
        elif text == '/interrupt' or text.startswith('/interrupt '):
            replacement = text[len('/interrupt'):].strip()
            if not replacement:
                self.write('Usage: /interrupt TEXT — cancel work and start a replacement request')
            else:
                await self.stop()
                self.write('Previous work stopped. Starting your replacement request.')
                self.start_request(replacement)
        elif text == '/steer' or text.startswith('/steer '):
            guidance = text[len('/steer'):].strip()
            if not guidance:
                self.write('Usage: /steer TEXT')
            elif self.task and not self.task.done():
                self.steer(guidance)
            else:
                self.start_request(guidance)
        elif text == '/help':
            self.write(HELP)
        elif text == '/peek' or text.startswith('/peek '):
            argument = text[5:].strip()
            if argument in ('on', 'off'):
                self.activity.expanded = argument == 'on'
                self.write(self.activity.peek())
            elif not argument or argument.isdigit():
                self.write(self.activity.peek(int(argument) if argument else None))
            else:
                self.write('Usage: /peek [ID|on|off]')
        elif text == '/status':
            self.write(f'Scope: {self.scope}; spend: ${self.cost.get_total():.4f}; findings: {self.memory.findings_count}')
        elif text.startswith('/scope') and (text == '/scope' or text[6:7] == ' '):
            if self.task and not self.task.done():
                self.write('Stop active work with /stop before changing scope.')
            else:
                try:
                    self.set_scope(self.parse_scope(text[6:].strip()))
                    self.write(f'Scope: {self.scope}')
                except ValueError as exc:
                    self.write(str(exc))
        elif text.startswith('/'):
            self.write('Unknown command. Use /help.')
        elif self.task and not self.task.done():
            self.steer(text)
        else:
            self.start_request(text)
        return True

    async def run(self, lines):
        cfg = self.llm_config.get_config('scanner')
        self.write(f'Interactive profile: {self.profile.name}\nModel provider: {cfg.provider} ({cfg.model or "configured default model"})\n{HELP}')
        try:
            async for line in lines:
                if not await self.handle(line):
                    break
        finally:
            await self.stop()
            if self.browser:
                await self.browser.stop()
            if self.agent and self.memory.mechanisms:
                from memory.mechanisms import apply_run_end
                apply_run_end([self.memory])
            self.write(f'Session closed. Estimated spend: ${self.cost.get_total():.4f}')


async def run_interactive(profile, llm_config, args, mechanisms):
    session = InteractiveSession(profile, llm_config, args, mechanisms, write=terminal_write)
    session.live_terminal = (sys.stdin.isatty() and sys.stdout.isatty()
                             and os.environ.get("TERM", "") != "dumb")
    web_ui = None
    try:
        if getattr(args, "web", False) or profile.dashboard.enabled:
            from web.interactive import InteractiveWebUI
            web_ui = InteractiveWebUI(session)
            await web_ui.start()
        await session.run(terminal_lines(session))
    finally:
        if web_ui is not None:
            await web_ui.shutdown()
