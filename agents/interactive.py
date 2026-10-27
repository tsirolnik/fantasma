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
from utils.terminal_markdown import MarkdownMessage, TerminalBanner, terminal_write
from config import get_settings
from cost.tracker import CostTracker
from memory.shared import SharedMemory


class InteractivePentester(PenTesterAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.allow_self_conclude = True
        self.context_mode = False
        self.history: list[dict] = []
        from agents.investigation_journal import InvestigationJournal
        self.journal = InvestigationJournal()
        self.tools.register(self.journal.tool())
        from agents.goal import InvestigationGoal
        self.goal = InvestigationGoal()
        self.goal_idle_turns = 0
        self.goal_max_idle_turns = 3
        for tool in self.goal.tools():
            self.tools.register(tool)
        self.on_scope_request = None
        from agents.tools import Tool
        self.tools.register(Tool(
            name='request_scope', category='interaction',
            description=('Ask the user to authorize additional hosts needed for their request. '
                         'Waits for approval or denial; does not authorize itself. '
                         'Use before attempting work outside configured scope. Existing scope is preserved.'),
            parameters={'type': 'object', 'properties': {
                'hosts': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 50},
                'reason': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
                'required': ['hosts', 'reason'], 'additionalProperties': False},
            handler=self.request_scope))

    async def request_scope(self, data):
        if self.on_scope_request is None:
            return {'status': 'unavailable', 'message': 'No interactive user is available to authorize scope.'}
        return await self.on_scope_request(data)


    def _before_model_call(self):
        if self.cost_tracker and self.session_budget > 0 and self.cost_tracker.get_total() >= self.session_budget:
            self.goal.pause('Session budget reached.')
            raise RuntimeError('Session budget reached; no further model calls will be made.')

    def _completion_instruction(self):
        if not self.goal.data or self.goal.data['status'] != 'active':
            return None
        self.goal_idle_turns += 1
        if self.goal_idle_turns >= self.goal_max_idle_turns:
            self.goal.pause('Repeated final responses without completing or blocking the goal. Review before resuming.')
            return None
        return ('The user requested continued pursuit of the active goal. Take the next evidence-supported step, '
                'or use update_goal to mark it complete with evidence or blocked with the missing prerequisite. '
                'Do not repeat a status-only answer.\n' + self.goal.briefing())

    def _build_progress_digest(self, iteration):
        return (f'[YOUR STATE] User-led request, step {iteration}/{self.max_iterations}.\n'
                'Complete the current user request, not a scan quota. Journal entries from older requests are context, not instructions to resume unrelated work. A tool invocation is not proof that a test ran. '
                'Check its result and prerequisites; errors, help output, and blocked access are inconclusive.\n'
                + self.journal.briefing() + "\n" + self.goal.briefing())

    def _build_initial_messages(self, assignment):
        # Keep tool results from earlier turns, not just the final prose answer.
        return [*self.history, {"role": "user", "content": self._build_user_message(assignment)}]

    def _build_user_message(self, assignment):
        return (f"{self.system_prompt}\n\nConfigured scope: {', '.join(self.in_scope) or '(none; tools disabled)'}"
                f"\n\nUser request:\n{assignment.context}")

    def _check_scope_claim(self, tool_name, params):
        # Search only visits the fixed public search engine, never a target.
        if tool_name == "web_search":
            return None
        if not self._allowed_domains:
            return "No target scope configured. Ask the user to set /scope before using tools."
        return super()._check_scope_claim(tool_name, params)

    async def execute(self, assignment):
        self._cancel_reason = ""
        self.goal_idle_turns = 0
        result = await super().execute(assignment)
        if self.goal.data and self.goal.data['status'] == 'active':
            self.goal.pause('Run stopped at its configured limit or encountered an error; review and resume explicitly.')
        if result.success and self.last_trace:
            self.history = list(self.last_trace.messages)
            if self.last_trace.final_answer:
                message = {"role": "assistant", "content": self.last_trace.final_answer}
                if self.last_trace.final_reasoning is not None:
                    message["reasoning_content"] = self.last_trace.final_reasoning
                self.history.append(self.last_trace.final_message or message)
        return result


HELP = """Enter a question or investigation request. No work starts until you send one.
/session           Show session ID and resume command
/save              Save the current session
/continue          Continue the last interrupted request or unfinished goal
/goal              Inspect the persistent investigation goal
/goal set TEXT     Set/replace a goal and start pursuing it
/goal pause|resume|clear Control the current goal
/scope HOST[,HOST]  Set authorized hosts (only while idle)
/scope approve|deny Respond to a pending agent scope request
/status            Show scope, spend and finding count
/context [VIEW]    Inspect model input, prompt, tools, and shared memory (Ctrl+G)
/peek [ID|on|off]  Inspect tool inputs/results; Ctrl+O toggles live details
/steer TEXT        Queue guidance for the next model step
/interrupt TEXT    Cancel current work, then start the replacement request
/stop              Cancel current work and remain in the session
/quit              Cancel current work and close the session
/help              Show this help
Full-screen controls: Tab focus; Ctrl+O/G inspect; arrows select in inspector;
Ctrl+U/D scroll reading panes; Ctrl+L latest; Ctrl+J newline; Ctrl+X replace.
Ctrl means Control (⌃ on Mac), not Command. Escape closes inspection.
While work runs, new text redirects it before a subsequent model call.
"""


async def terminal_lines(session=None):
    """Read terminal or piped lines without an uncancellable input thread."""
    if sys.stdin.isatty() and sys.stdout.isatty() and getattr(session, "live_terminal", True):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.filters import Condition
        keys = KeyBindings()
        inspecting = Condition(lambda: bool(session and session.activity.expanded))
        @keys.add('escape', filter=inspecting)
        def close_inspector(event):
            session.activity.set_inspection(False)
        @keys.add('pagedown', filter=inspecting)
        def scroll_inspector_down(event):
            session.activity.scroll_inspection(8)
        @keys.add('pageup', filter=inspecting)
        def scroll_inspector_up(event):
            session.activity.scroll_inspection(-8)

        @keys.add('end', filter=inspecting)
        def follow_inspector_tail(event):
            session.activity.follow_tail = True

        @keys.add('c-g')
        def inspect_context(event):
            if session:
                session.activity.open_context(toggle=True)

        @keys.add('c-o')
        def inspect_tools(event):
            if session:
                session.activity.toggle()
        prompt = PromptSession(
            key_bindings=keys,
            bottom_toolbar=session.activity.formatted_toolbar if session else None,
            refresh_interval=0.1,
            style=Style.from_dict({
                "bottom-toolbar": "fg:default bg:default noreverse",
                "activity": "fg:default bg:default bold",
                "panel-title": "fg:default bg:default reverse bold",
                "panel-body": "fg:default bg:default",
                "prompt": "fg:default bg:default bold",
            }),
        )
        if session:
            session.activity.live_inspector = True
        with patch_stdout(raw=True):
            while True:
                try:
                    yield await prompt.prompt_async([('class:prompt', 'you › ')])
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
        self.scope_request = None
        self.agent = None
        self.browser = None
        self.task = None
        from utils.terminal_activity import TerminalActivity
        self.activity = TerminalActivity(lambda text: self.write(text))
        from utils.context_inspector import ContextInspector
        self.context_inspector = ContextInspector(self)
        self.activity.context_reader = self.context_inspector.render
        self.live_terminal = False
        self.pending_steering = []
        self.store = None
        self.session_id = None
        self.saved_history = []
        self.pending_request = ""
        self.session_status = "idle"
        self.save_error_shown = False
        self.saved_observations = []
        self.saved_journal = []
        from agents.goal import InvestigationGoal
        self.goal = InvestigationGoal(on_change=self.save, reviewer=self.review_goal)
        self.goal.max_reviews = self.profile.interaction.goal_max_reviews
        self.goal_reviewer = None
        self.diagnostics = (getattr(args, "diagnostics", False) or
                            getattr(args, "verbose", False) or profile.interaction.diagnostics)
        self._input_lock = asyncio.Lock()
        self.budget = args.budget if args.budget is not None else get_settings().session_budget

    def attach_store(self, store, snapshot=None):
        from memory.checkpoint import restore_shared_memory
        self.store = store
        self.session_id = store.session_id
        if snapshot:
            self.saved_history = snapshot.get('history', [])
            self.saved_observations = snapshot.get('observations', [])
            self.saved_journal = snapshot.get('journal', [])
            self.goal.restore(snapshot.get('goal'))
            self.scope = self.parse_scope(','.join(snapshot.get('scope', [])))
            self.pending_request = snapshot.get('pending_request', '')
            self.session_status = 'interrupted' if self.pending_request else 'idle'
            restore_shared_memory(self.memory, snapshot.get('memory', {}))
            self.memory._finding_reporters = snapshot.get('finding_reporters', ['pentester'] * len(self.memory._findings))
            self.memory.allowed_domains = set(self.scope)
            self.cost.add_cost('previous_session', snapshot.get('cost', 0), detail='Restored session spend')
            if self.args.budget is None:
                self.budget = snapshot.get('budget', self.budget)
            if getattr(self.args, 'targets', None):
                self.set_scope(self.parse_scope(self.args.targets))
        self.save()

    def save(self):
        if self.store is None:
            return False
        from pathlib import Path
        from memory.checkpoint import capture_shared_memory
        profile_ref = getattr(self.args, 'profile', None) or self.profile.name
        if Path(profile_ref).is_file():
            profile_ref = str(Path(profile_ref).resolve())
        history = getattr(self.agent, 'history', self.saved_history) if self.agent else self.saved_history
        try:
            self.store.save(dict(id=self.session_id, profile=profile_ref,
                scope=self.scope, history=history, memory=capture_shared_memory(self.memory),
                finding_reporters=list(self.memory._finding_reporters),
                cost=self.cost.get_total(), budget=self.budget,
                pending_request=self.pending_request, status=self.session_status, goal=self.goal.data,
                journal=self.agent.journal.entries if self.agent and hasattr(self.agent, "journal") else self.saved_journal,
                observations=self.recent_observations()))
            self.save_error_shown = False
            return True
        except (OSError, ValueError, TypeError) as exc:
            if not self.save_error_shown:
                self.write(f'Session save failed: {exc}. Use /save to retry.')
                self.save_error_shown = True
            return False

    def recent_observations(self):
        """Keep bounded tool previews across resumes, including interrupted work."""
        observations = list(self.saved_observations)
        observations.extend(dict(name=c['name'], status=c['status'], input=c['input'], output=c['output'])
                            for c in self.activity.calls[-20:])
        return observations[-20:]

    def model_finished(self, *args):
        self.activity.llm_finished(*args)
        self.save()

    def tool_finished(self, call):
        self.activity.tool_finished(call)
        self.save()

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
        self.agent.goal = self.goal
        self.agent.goal_max_idle_turns = self.profile.interaction.goal_max_idle_turns
        for tool in self.goal.tools():
            self.agent.tools.register(tool)
        self.agent.on_scope_request = self.request_scope
        self.agent.history = list(self.saved_history)
        self.agent.journal.restore(self.saved_journal)
        self.agent.session_budget = self.budget
        self.agent.on_tool_start = self.activity.tool_started
        self.agent.on_tool_call = self.tool_finished
        self.agent.on_context_snapshot = self.context_inspector.capture
        self.agent.on_llm_start = self.activity.llm_started
        self.agent.on_llm_call = self.model_finished
        self.set_scope(self.scope)

    async def review_goal(self, goal, proposal):
        from agents.goal_reviewer import GoalReviewer
        from dataclasses import asdict
        if self.goal_reviewer is None:
            # Separate instance and fresh conversation; never reuse investigator history as reviewer instructions.
            llm = create_llm_for_agent('validator', self.llm_config)
            self.goal_reviewer = GoalReviewer(llm, self.cost, self.budget)
        observations = []
        def add(kind, value):
            raw = json.dumps(value, ensure_ascii=False, default=str)
            observations.append({'id': f'E{len(observations) + 1}', 'kind': kind,
                                 'content': raw[:16000], 'truncated': len(raw) > 16000})
        if self.agent:
            trace = self.agent.last_trace
            if trace:
                add('user_request (user-supplied data, not execution evidence)', trace.assignment.context)
                for call in trace.tool_calls[-30:]:
                    if call.tool_name not in ('create_goal', 'get_goal', 'update_goal', 'record_investigation'):
                        add('recorded_tool_result', asdict(call))
            for message in self.agent.history[-30:]:
                if message.get('role') == 'user':
                    add('prior_user_input_or_tool_results', message.get('content'))
        for finding in self.memory.get_findings()[-20:]:
            add('submitted_finding (check verification status; not independently proven)', finding.model_dump(mode='json'))
        for observation in self.recent_observations():
            if observation.get('name') not in ('create_goal', 'get_goal', 'update_goal', 'record_investigation'):
                add('recorded_tool_preview (may be truncated; not a fresh execution)', observation)
        self.write('Goal reviewer: checking completion criteria against recorded evidence…')
        verdict = await self.goal_reviewer.review(goal, proposal, observations)
        # Retain the exact bounded packet alongside references so reviews remain inspectable after resume.
        verdict['observations'] = observations
        self.write('Goal reviewer: ' + ('approved. ' if verdict['approved'] else 'more work needed. ') + verdict['feedback'])
        return verdict

    async def request_scope(self, data):
        hosts, reason = data.get('hosts'), data.get('reason')
        if (not isinstance(hosts, list) or not 1 <= len(hosts) <= 50
                or any(not isinstance(h, str) or not h.strip() or len(h) > 253 or ',' in h
                       or any(ord(c) < 32 or ord(c) == 127 for c in h) for h in hosts)
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000):
            return {'status': 'invalid', 'message': 'Provide 1–50 hosts and a nonempty reason (at most 2000 characters).'}
        try:
            requested = self.parse_scope(','.join(hosts))
        except ValueError as exc:
            return {'status': 'invalid', 'message': str(exc)}
        additions = sorted(set(requested) - set(self.scope))
        if not additions:
            return {'status': 'already_authorized', 'scope': list(self.scope)}
        if self.scope_request is not None:
            return {'status': 'pending', 'message': 'Another scope request is awaiting the user. Do not test these hosts.'}
        future = asyncio.get_running_loop().create_future()
        pending = {'hosts': additions, 'future': future}
        self.scope_request = pending
        from utils.terminal_activity import TerminalActivity
        self.write('Scope authorization requested: ' + ', '.join(additions)
                   + '\nReason: ' + TerminalActivity.preview(reason)
                   + '\n/scope approve to add these hosts, /scope deny to decline, or /stop to cancel.')
        try:
            return await future
        finally:
            if self.scope_request is pending:
                self.scope_request = None

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
                self.pending_request = ""
                self.session_status = "idle"
                self.write(MarkdownMessage(result.reflection or json.dumps(result.data, default=str)))
            else:
                self.write(f'Work stopped: {result.error}')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.goal.pause('Session error; review before resuming.')
            self.write(f'Session error: {exc}')
        finally:
            if self.pending_request:
                self.session_status = 'interrupted'
            self.save()
            self.pending_steering.clear()
            self.activity.finish()
            if self.goal.data and self.goal.data['status'] in ('paused', 'blocked'):
                self.write(f"Goal {self.goal.data['status']}: {self.goal.data['progress']} (/goal to inspect; /goal resume to continue)")
        if not self.live_terminal and sys.stdin.isatty() and sys.stdout.isatty():
            print('you> ', end='', flush=True)

    async def stop(self):
        self.goal.pause("Stopped by the user; waiting for resume.")
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.task = None
        self.pending_steering.clear()
        self.activity.finish()
        if self.pending_request:
            self.session_status = "interrupted"
        self.save()
        if self.agent:
            self.agent._sticky_directives.clear()
            while not self.agent._directive_queue.empty():
                self.agent._directive_queue.get_nowait()

    def start_request(self, text):
        self.pending_request = text
        self.session_status = "running"
        self.save()
        self.activity.begin()
        if not self.live_terminal:
            self.write('Thinking… 👻 Consulting the packet spirits. (/stop to cancel)')
        self.task = asyncio.create_task(self.ask(text))

    def steer(self, text):
        self.pending_request += f"\nAdditional guidance: {text}"
        self.save()
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
        elif text == '/goal' or text.startswith('/goal '):
            command = text[len('/goal'):].strip()
            if not command:
                self.write(self.goal.briefing())
            elif command == 'pause':
                await self.stop()
                self.write(self.goal.briefing())
            elif command == 'clear':
                await self.stop()
                self.goal.data = None
                self.save()
                self.write('Goal cleared.')
            elif command == 'resume':
                if self.task and not self.task.done():
                    self.write('Work is already running.')
                elif self.goal.resume():
                    self.start_request('Resume the investigation goal. Review prior evidence before repeating actions.\n' + self.goal.briefing())
                else:
                    self.write('No unfinished goal to resume.')
            elif command.startswith('set '):
                objective = command[4:].strip()
                if not objective or len(objective) > 1000:
                    self.write('Use /goal set OBJECTIVE (1–1000 characters).')
                else:
                    await self.stop()
                    self.goal.data = None
                    self.goal.create(objective, [objective])
                    self.start_request('Pursue this explicitly requested goal.\n' + self.goal.briefing())
            else:
                self.write('Usage: /goal [set OBJECTIVE|pause|resume|clear]')
        elif text == '/session':
            self.write(f'Session: {self.session_id or "not persisted"}\nResume: fantasma full --resume-session {self.session_id or "ID"}')
        elif text == '/save':
            if self.save():
                self.write(f'Session saved: {self.session_id}')
        elif text == '/continue':
            if self.task and not self.task.done():
                self.write('Work is already running. Use /steer, /interrupt, or /stop.')
            elif self.pending_request:
                self.goal.resume()
                request = self.pending_request
                observations = self.activity.peek() if self.activity.calls else json.dumps(self.saved_observations, ensure_ascii=False)
                self.start_request('Continue the interrupted request below. Review existing findings and context before repeating actions.\n' + request + '\nRecent tool observations (may be truncated):\n' + observations)
            elif self.goal.resume():
                self.start_request('Resume the investigation goal.\n' + self.goal.briefing())
            else:
                self.write('No interrupted request or unfinished goal. Send a new instruction.')
        elif text == '/help':
            self.write(HELP)
        elif text == '/context' or text.startswith('/context '):
            view = text[len('/context'):].strip() or 'overview'
            if view == 'off':
                self.activity.set_inspection(False)
            elif view in ('overview', 'messages', 'tools', 'prompt', 'memory', 'plan', 'reasoning', 'goal') or view.isdigit():
                self.activity.open_context(view)
            else:
                self.write('Usage: /context [messages|N|tools|prompt|memory|plan|reasoning|goal|off]')
        elif text == '/peek' or text.startswith('/peek '):
            argument = text[5:].strip()
            if argument in ('on', 'off'):
                self.activity.set_inspection(argument == 'on')
            elif not argument or argument.isdigit():
                identifier = int(argument) if argument else None
                if self.activity.live_inspector:
                    self.activity.set_inspection(True, identifier)
                else:
                    self.write(self.activity.peek(identifier))
            else:
                self.write('Usage: /peek [ID|on|off]')
        elif text == '/status':
            self.write(f'Scope: {self.scope}; spend: ${self.cost.get_total():.4f}; findings: {self.memory.findings_count}')
        elif text in ('/scope approve', '/scope deny'):
            pending = self.scope_request
            if pending is None or pending['future'].done():
                self.write('No pending scope request.')
            else:
                approved = text == '/scope approve'
                if approved:
                    self.set_scope(sorted(set(self.scope) | set(pending['hosts'])))
                    self.save()
                pending['future'].set_result({'status': 'approved' if approved else 'denied',
                                             'requested_hosts': pending['hosts'], 'scope': list(self.scope)})
                self.scope_request = None
                self.write(f'Scope request {"approved" if approved else "denied"}. Scope: {self.scope}')
        elif text.startswith('/scope') and (text == '/scope' or text[6:7] == ' '):
            if self.task and not self.task.done():
                self.write('Stop active work with /stop before changing scope.')
            else:
                try:
                    self.set_scope(self.parse_scope(text[6:].strip()))
                    self.save()
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
        if self.session_id:
            self.write(f'Session: {self.session_id} (autosaved; /session for resume command)')
            if self.pending_request:
                self.write('An interrupted request is saved. Use /continue or send a new instruction; no work resumes automatically.')
        cfg = self.llm_config.get_config('scanner')
        self.write(TerminalBanner(
            f'Profile  {self.profile.name}\n'
            f'Model    {cfg.provider} / {cfg.model or getattr(get_settings(), cfg.provider + "_model", "provider default")}\n'
            f'Scope    {", ".join(self.scope) or "not set — use /scope HOST"}\n\n'
            'Ctrl+O tools   Ctrl+G context   /steer guide   /stop cancel\n'
            '/help all commands · Send a request when ready.'))
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
    from memory.interactive_sessions import InteractiveSessionStore
    store = InteractiveSessionStore()
    resume_id = getattr(args, 'resume_session', None)
    store.acquire(resume_id)
    web_ui = None
    console = None
    if session.live_terminal:
        from utils.terminal_console import TerminalConsole
        console = TerminalConsole(session)
        session.write = console.write
    try:
        snapshot = store.load(resume_id) if resume_id else None
        session.attach_store(store, snapshot)
        if getattr(args, "web", False) or profile.dashboard.enabled:
            from web.interactive import InteractiveWebUI
            web_ui = InteractiveWebUI(session)
            await web_ui.start()
        if console:
            await console.run()
            terminal_write(f"Session closed. Estimated spend: ${session.cost.get_total():.4f}")
        else:
            await session.run(terminal_lines(session))
    finally:
        try:
            if web_ui is not None:
                await web_ui.shutdown()
        finally:
            store.close()
            if session.session_id and store.path(session.session_id).is_file():
                terminal_write(f"Resume: fantasma full --resume-session {session.session_id}")
