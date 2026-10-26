"""Full-screen interactive workspace. All actions go through session.handle."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import sys

from prompt_toolkit import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Layout, ConditionalContainer, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea, Frame
from rich.console import Console
from rich.markdown import Markdown

from utils.terminal_markdown import MarkdownMessage


class RenderedLexer(Lexer):
    def __init__(self):
        self.lines = []

    def lex_document(self, document):
        lines = self.lines
        return lambda number: lines[number] if number < len(lines) else []


class TerminalConsole:
    CONTEXT_VIEWS = ('overview', 'plan', 'reasoning', 'messages', 'memory', 'tools', 'prompt')
    COMMANDS = ('/help', '/scope', '/status', '/session', '/save', '/continue',
                '/context', '/peek', '/steer', '/interrupt', '/stop', '/quit')

    def __init__(self, session, *, input=None, output=None):
        self.session = session
        self.is_macos = sys.platform == "darwin"
        self.queue = asyncio.Queue()
        self.entries = []
        self.revision = 0
        self.rendered_key = None
        self.follow = True
        self.lexer = RenderedLexer()
        self.transcript = TextArea(read_only=True, scrollbar=True, wrap_lines=True,
                                   height=self.transcript_height,
                                   lexer=self.lexer, focusable=True)
        self.details = TextArea(read_only=True, scrollbar=True, wrap_lines=True, focusable=True)
        self.composer = TextArea(height=self.composer_height, multiline=True, wrap_lines=True,
            prompt='you › ', completer=WordCompleter(self.COMMANDS, sentence=True),
            complete_while_typing=False)
        self.detail_key = None
        keys = KeyBindings()
        composing = Condition(lambda: self.app.layout.has_focus(self.composer))
        inspecting = Condition(lambda: self.app.layout.has_focus(self.details))

        @keys.add('enter', filter=composing)
        def send(event):
            if event.current_buffer.complete_state:
                event.current_buffer.complete_state = None
            self.submit()

        @keys.add('escape', 'enter', filter=composing)
        @keys.add('c-j', filter=composing)
        def newline(event):
            event.current_buffer.insert_text('\n')

        @keys.add('c-x', filter=composing)
        def replace(event):
            self.submit(interrupt=True)

        @keys.add('c-c')
        def stop(event):
            self.enqueue('/stop')

        @keys.add('c-d', filter=composing)
        def quit_session(event):
            # A nonempty draft is never silently discarded by Ctrl+D.
            if not self.composer.text:
                self.enqueue('/quit')

        @keys.add('tab')
        def focus_or_complete(event):
            if composing() and self.composer.text.startswith('/'):
                event.current_buffer.start_completion(select_first=False)
            else:
                event.app.layout.focus_next()

        @keys.add('s-tab')
        def previous_focus(event):
            event.app.layout.focus_previous()

        @keys.add('f2')
        @keys.add('c-o')
        def tools(event):
            session.activity.toggle()
            self.detail_key = None
            self.app.layout.focus(self.details if session.activity.expanded else self.composer)

        @keys.add('f3')
        @keys.add('c-g')
        def context(event):
            session.activity.open_context(toggle=True)
            self.detail_key = None
            self.app.layout.focus(self.details if session.activity.expanded else self.composer)

        @keys.add('escape')
        def close(event):
            session.activity.expanded = False
            event.app.layout.focus(self.composer)

        @keys.add('left', filter=inspecting)
        @keys.add('f5')
        def previous(event):
            self.select_detail(-1)

        @keys.add('right', filter=inspecting)
        @keys.add('f6')
        def next_item(event):
            self.select_detail(1)

        @keys.add('c-u', filter=~composing)
        @keys.add('c-d', filter=~composing)
        @keys.add('pageup')
        @keys.add('pagedown')
        def page(event):
            area = self.details if event.app.layout.has_focus(self.details) else self.transcript
            if area is self.transcript:
                self.follow = False
            event.app.layout.focus(area)
            rows = max(1, self.app.output.get_size().rows - 12)
            if event.key_sequence[0].key in ('pageup', 'c-u'):
                area.buffer.cursor_up(count=rows)
            else:
                area.buffer.cursor_down(count=rows)

        @keys.add('c-l')
        @keys.add('c-end')
        def follow(event):
            self.follow = True
            self.transcript.buffer.cursor_position = len(self.transcript.text)
            event.app.layout.focus(self.composer)

        def line(text, style):
            return Window(FormattedTextControl(text), height=1, style=style)

        # History and the live input share a column. Spare height belongs BELOW
        # the input, never between the latest reply and the user's draft.
        conversation = Frame(HSplit([
            self.transcript,
            self.composer,
            line(self.composer_title, 'class:hint'),
            Window(height=Dimension(min=0, preferred=0, weight=1)),
        ]), title='Conversation')
        inspector = ConditionalContainer(Frame(self.details, title=self.detail_title),
                                          filter=Condition(lambda: session.activity.expanded))
        body = VSplit([HSplit([conversation], width=Dimension(weight=1)),
                       ConditionalContainer(HSplit([inspector], width=Dimension(weight=1)),
                                            filter=Condition(lambda: session.activity.expanded))], padding=1)
        stacked = HSplit([conversation, inspector])
        wide = Condition(lambda: self.app.output.get_size().columns >= 100)
        root = HSplit([
            line(self.header, 'class:header'),
            ConditionalContainer(body, wide), ConditionalContainer(stacked, ~wide),
            line(self.status, 'class:status'),
            line(self.shortcuts, 'class:hint'),
        ])
        self.app = Application(layout=Layout(root, focused_element=self.composer),
            # Leave mouse selection to the terminal so Cmd+C/native copy works.
            key_bindings=keys, full_screen=True, mouse_support=False,
            refresh_interval=.15, before_render=self.refresh,
            input=input, output=output,
            style=Style.from_dict({
                '': 'fg:default bg:default',
                'header': 'reverse bold',
                'status': 'bold', 'hint': '', 'frame.border': '',
                'frame.label': 'bold', 'text-area': '',
                'selected-text': 'reverse',
                'completion-menu': 'fg:default bg:default',
                'completion-menu.completion': 'fg:default bg:default',
                'completion-menu.completion.current': 'reverse bold',
                'completion-menu.meta.completion': 'fg:default bg:default',
                'completion-menu.meta.completion.current': 'reverse',
            }))
        self.app.timeoutlen = .3
        self.app.ttimeoutlen = .05
        session.activity.live_inspector = True

    def transcript_height(self):
        from prompt_toolkit.utils import get_cwidth
        width = self.conversation_width()
        rows = sum(max(1, (get_cwidth(line) + width - 1) // width) for line in self.transcript.text.split('\n')) if hasattr(self, 'transcript') else 1
        return Dimension(min=1, preferred=rows, max=rows)

    def composer_height(self):
        # Include soft wraps and let long drafts scroll within a six-line input.
        from prompt_toolkit.utils import get_cwidth
        width = max(10, self.conversation_width() - 6)
        rows = sum(max(1, (get_cwidth(line) // width) + 1) for line in self.composer.text.split('\n')) if hasattr(self, 'composer') else 1
        rows = min(6, rows)
        return Dimension(min=1, preferred=rows, max=rows)

    def conversation_width(self):
        if not hasattr(self, 'app'):
            return 80
        size = self.app.output.get_size()
        return max(10, (size.columns // 2 if self.session.activity.expanded and size.columns >= 100 else size.columns) - 5)

    def shortcuts(self):
        if self.is_macos:
            return ('Tab focus · Ctrl+O tools · Ctrl+G context · ←/→ select · Ctrl+U/D scroll · Ctrl+L latest · Esc close'
                    if self.app.output.get_size().columns >= 100 else 'Tab focus · Ctrl+O/G inspect · Ctrl+L latest · /help')
        return ('Tab focus · F2 tools · F3 context · F5/F6 select · PgUp/Dn scroll · Ctrl+End latest · Esc close'
                if self.app.output.get_size().columns >= 100 else 'Tab focus · F2/F3 inspect · Esc close · /help')

    def header(self):
        s = self.session
        scope = ', '.join(s.scope) or 'no scope'
        return f' 👻 FANTASMA  ·  {s.profile.name}  ·  ${s.cost.get_total():.3f}/${s.budget:.2f}  ·  {scope}'

    def status(self):
        return self.session.activity.status_line() + ('  ·  scroll paused' if not self.follow else '')

    def composer_title(self):
        busy = self.session.task and not self.session.task.done()
        mode = 'Steer current work' if busy else 'Message'
        if self.conversation_width() < 100:
            return mode + ' · Enter send · Ctrl+J newline · Ctrl+C stop'
        newline = 'Ctrl+J' if self.is_macos else 'Alt+Enter'
        return mode + f' · Enter send · {newline} newline · Ctrl+X replace · Ctrl+C stop'

    def detail_title(self):
        a = self.session.activity
        return ('Context / ' + a.context_view if a.inspector_kind == 'context'
                else f'Tools / {"latest" if a.inspected_id is None else "#" + str(a.inspected_id)}') + (' · ←/→ select' if self.is_macos else ' · F5/F6 select')

    def select_detail(self, direction):
        a = self.session.activity
        a.expanded = True
        if a.inspector_kind == 'context':
            index = self.CONTEXT_VIEWS.index(a.context_view) if a.context_view in self.CONTEXT_VIEWS else 0
            a.context_view = self.CONTEXT_VIEWS[(index + direction) % len(self.CONTEXT_VIEWS)]
        else:
            ids = [c['id'] for c in a.calls] + [None]
            index = ids.index(a.inspected_id) if a.inspected_id in ids else len(ids) - 1
            a.inspected_id = ids[(index + direction) % len(ids)]
        self.detail_key = None
        self.app.invalidate()

    def write(self, value):
        text = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', str(value))
        text = ''.join(c for c in text if c.isprintable() or c in '\n\t')
        self.entries.append((text, isinstance(value, MarkdownMessage)))
        del self.entries[:-500]
        self.revision += 1
        self.app.invalidate()

    def enqueue(self, text):
        self.queue.put_nowait(text)
        self.app.invalidate()

    def submit(self, interrupt=False):
        text = self.composer.text.strip()
        if not text:
            return
        command = '/interrupt ' + text if interrupt else text
        self.session.write('you › ' + command)
        self.composer.buffer.append_to_history()
        self.composer.text = ''
        self.enqueue(command)

    def refresh(self, app):
        width = self.conversation_width()
        key = (self.revision, width)
        if key != self.rendered_key:
            stream = io.StringIO()
            console = Console(file=stream, width=width, force_terminal=True, no_color=True, color_system='standard')
            for text, markdown in self.entries:
                console.print(Markdown(text) if markdown else text, markup=False, highlight=False)
                console.print()
            fragments = to_formatted_text(ANSI(stream.getvalue()))
            # Rich pads styled blocks. Strip that padding from both the text and
            # lexer so it cannot create invisible soft-wrapped rows.
            self.lexer.lines = []
            for parts in split_lines(fragments):
                parts = list(parts)
                while parts:
                    trimmed = parts[-1][1].rstrip(' ')
                    if trimmed:
                        parts[-1] = (parts[-1][0], trimmed)
                        break
                    parts.pop()
                self.lexer.lines.append(parts)
            while len(self.lexer.lines) > 1 and not self.lexer.lines[-1]:
                self.lexer.lines.pop()
            plain = '\n'.join(''.join(part[1] for part in parts) for parts in self.lexer.lines)
            position = len(plain) if self.follow and not app.layout.has_focus(self.transcript) else min(self.transcript.buffer.cursor_position, len(plain))
            self.transcript.buffer.set_document(Document(plain, position), bypass_readonly=True)
            self.rendered_key = key
        if self.follow and not app.layout.has_focus(self.transcript):
            # Recompute the tail against the current viewport after resize/reflow.
            self.transcript.window.vertical_scroll = 0
        a = self.session.activity
        if a.expanded:
            text = a.context_reader(a.context_view) if a.inspector_kind == 'context' else a.peek(a.inspected_id)
            selection = (a.inspector_kind, a.context_view, a.inspected_id)
            if text != self.details.text or selection != self.detail_key:
                position = 0 if a.inspector_kind == 'context' else len(text)
                if selection == self.detail_key and app.layout.has_focus(self.details):
                    position = min(self.details.buffer.cursor_position, len(text))
                self.details.buffer.set_document(Document(text, position), bypass_readonly=True)
                self.detail_key = selection
        if not a.expanded and app.layout.has_focus(self.details):
            app.layout.focus(self.composer)

    async def lines(self):
        while True:
            text = await self.queue.get()
            if text is None:
                return
            yield text

    async def run(self):
        # Keep opt-in diagnostics inside the workspace, not over its screen.
        owner = self
        class DiagnosticStream:
            def write(self, text):
                if text.strip():
                    owner.write(text.rstrip())
            def flush(self):
                pass
        streams = [(h, h.stream) for h in logging.getLogger().handlers
                   if h.name == 'fantasma.console' and hasattr(h, 'stream')]
        for handler, _ in streams:
            handler.setStream(DiagnosticStream())
        worker = asyncio.create_task(self.session.run(self.lines()))
        def finished(task):
            if self.app.is_running:
                self.app.exit()
        worker.add_done_callback(finished)
        try:
            await self.app.run_async()
        finally:
            for handler, stream in streams:
                handler.setStream(stream)
            self.queue.put_nowait(None)
            if not worker.done():
                worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
