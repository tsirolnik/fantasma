"""Terminal interaction adapter for swarm and sequential lab pipelines."""
from __future__ import annotations

import asyncio
import contextlib
import math

from agents.interactive import InteractiveSession
from agents.messaging import AgentMessage


HELP = """Pipeline console: configure scope, then /start. No investigation starts before /start.
/start                 Start this run once
/scope HOST[,HOST]      Set scope before starting (otherwise profile target provider applies)
/status                Show lifecycle, scope and estimated cost
/pause /resume         Cooperative execution controls
/budget USD            Change the effective run budget
/tell AGENT MESSAGE    Redirect a registered tool agent or the Director
Plain text             Send a directive/question to the Director
/continue /skip        Resolve a pending human checkpoint
/stop                  Cancel the run (completed actions are not rolled back)
/quit                  Stop and close the console
/help                  Show commands
"""


class PipelineConsole:
    def __init__(self, pipeline, run_kwargs, write=print):
        self.pipeline = pipeline
        self.run_kwargs = dict(run_kwargs)
        self.write = write
        self.task = None
        self.started = False
        self.state = 'idle'
        self.pending_checkpoint = None
        pipeline._terminal_controller = self
        pipeline.bus.subscribe('response', self.on_response)

    def on_response(self, message):
        if message.recipient == 'human':
            self.write(f'{message.sender}: {message.content}')

    async def checkpoint(self, phase, data):
        if self.pending_checkpoint is not None:
            raise RuntimeError('A terminal checkpoint is already pending')
        self.pending_checkpoint = asyncio.get_running_loop().create_future()
        self.write(f'Checkpoint: {phase}. /continue to proceed, /skip to decline, /stop to cancel.')
        try:
            return await self.pending_checkpoint
        finally:
            self.pending_checkpoint = None

    async def execute(self):
        self.state = 'running'
        try:
            await self.pipeline.run(**self.run_kwargs)
            self.state = 'completed'
            self.write('Run completed. Use /status or /quit; start a new process for another run.')
        except asyncio.CancelledError:
            self.state = 'stopped'
            raise
        except Exception as exc:
            self.state = 'failed'
            self.write(f'Run failed: {exc}')

    async def stop(self):
        if self.task and not self.task.done():
            self.pipeline.stop()
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.state = 'stopped'
        if self.pending_checkpoint and not self.pending_checkpoint.done():
            self.pending_checkpoint.cancel()

    def tell(self, recipient, content):
        if not content.strip():
            self.write('Usage: /tell AGENT MESSAGE')
            return
        if recipient != 'director':
            agent = self.pipeline.bus.get_agent(recipient)
            if agent is None or not callable(getattr(agent, 'inject_directive', None)):
                self.write(f'{recipient} is not a registered tool agent; use director for stage-level guidance.')
                return
            agent.inject_directive(content)
        self.pipeline.bus.send(AgentMessage(sender='human', recipient=recipient,
                                           msg_type='directive', content=content, priority='critical'))
        self.write(f'Directive queued for {recipient}.')

    async def handle(self, line):
        text = line.strip()
        if not text:
            return True
        command, _, value = text.partition(' ')
        if command in ('/quit', '/exit'):
            await self.stop()
            return False
        if command == '/start':
            if self.started:
                self.write('This run has already started. Use /resume for a paused run.')
            else:
                self.started = True
                self.state = 'starting'
                self.task = asyncio.create_task(self.execute())
        elif command == '/scope':
            if self.started:
                self.write('Scope is fixed after /start. Start a new session to change it.')
            else:
                try:
                    self.run_kwargs['targets'] = InteractiveSession.parse_scope(value) or None
                    self.write(f"Scope: {self.run_kwargs['targets'] or 'profile target provider'}")
                except ValueError as exc:
                    self.write(str(exc))
        elif command == '/status':
            self.write(f"State: {self.state}; scope: {self.run_kwargs.get('targets')}; "
                       f"spend: ${self.pipeline.cost_tracker.get_total():.4f}; "
                       f"budget: {getattr(self.pipeline, 'effective_budget', None) or self.run_kwargs.get('budget')}")
        elif command in ('/pause', '/resume'):
            if not self.task or self.task.done():
                self.write('No active run.')
            else:
                getattr(self.pipeline, command[1:])()
                self.state = 'paused' if command == '/pause' else 'running'
                self.write(f'{self.state} (cooperative control).')
        elif command == '/budget':
            try:
                budget = float(value)
                if not math.isfinite(budget) or budget <= 0:
                    raise ValueError()
                self.run_kwargs['budget'] = budget
                self.pipeline.effective_budget = budget
                for role in ("discover", "recon", "scanner", "analyzer"):
                    worker = getattr(self.pipeline, role, None)
                    if worker is not None and hasattr(worker, "session_budget"):
                        worker.session_budget = budget
                self.write(f'Budget set to ${budget:.2f}.')
            except ValueError:
                self.write('Budget must be a finite positive number.')
        elif command in ('/continue', '/skip'):
            if self.pending_checkpoint and not self.pending_checkpoint.done():
                self.pending_checkpoint.set_result(command == '/continue')
            else:
                self.write('No checkpoint is waiting.')
        elif command == '/stop':
            await self.stop()
            self.write('Run stopped.')
        elif command == '/help':
            self.write(HELP)
        elif command == '/tell':
            recipient, _, content = value.partition(' ')
            self.tell(recipient, content)
        elif command.startswith('/'):
            self.write('Unknown command. Use /help.')
        else:
            self.tell('director', text)
        return True

    async def run(self, lines):
        self.write(HELP)
        try:
            async for line in lines:
                if not await self.handle(line):
                    break
        finally:
            await self.stop()
