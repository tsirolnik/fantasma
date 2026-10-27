"""Read-only views of assembled model input and session state."""
from utils.terminal_activity import TerminalActivity
from config import get_settings
from llm.reasoning import reasoning_text


class ContextInspector:
    def __init__(self, session):
        self.session = session
        self.messages = None
        self.tools = []
        self.message_sizes = []

    def capture(self, messages, tools):
        self.messages = [{'role': str(message.get('role', '?')),
                          'content': TerminalActivity.preview(message.get('content', '')),
                          'reasoning_content': TerminalActivity.preview(reasoning_text(message))}
                         for message in messages]
        self.message_sizes = [len(str(m.get('content', ''))) for m in messages]
        self.tools = [{'name': str(t.get('name', '?')), 'schema': TerminalActivity.preview(t)} for t in tools]

    def render(self, view='overview'):
        s = self.session
        agent = s.agent
        history = getattr(agent, 'history', s.saved_history) if agent else s.saved_history
        messages = self.messages if self.messages is not None else history
        label = 'Last assembled model request' if self.messages is not None else 'Saved conversation (no model request yet)'
        if view == 'reasoning':
            entries = [f"Message {i} · provider-returned reasoning\n" + TerminalActivity.preview(reasoning_text(m))
                       for i, m in enumerate(messages, 1) if reasoning_text(m)]
            return label + '\nReasoning is model-generated context, not verified evidence.\n' + ('\n\n'.join(entries) or 'No provider reasoning captured in this context. Older sessions cannot recover discarded reasoning.')
        if view.isdigit():
            index = int(view) - 1
            if not 0 <= index < len(messages):
                return f'No context message {view}. Use /context messages.'
            m = messages[index]
            return f'{label} · message {view} · {m.get("role", "?")}\n' + TerminalActivity.preview(m.get('content', ''))
        if view == 'messages':
            return label + '\n' + ('\n\n'.join(
                f'{i}. {m.get("role", "?")} · {self.message_sizes[i-1] if self.messages is not None else len(str(m.get("content", "")))} chars\n'
                + TerminalActivity.preview(m.get('content', ''))[:180]
                for i, m in enumerate(messages, 1)) or 'No conversation yet.') + '\n/context N opens a message.'
        if view == 'tools':
            return 'Tool schemas from the last assembled request\n' + ('\n\n'.join(t['schema'] for t in self.tools) or 'No request yet; no tool schemas captured.')
        if view == 'prompt':
            return 'Current agent system instructions (also embedded in interactive requests)\n' + (
                TerminalActivity.preview(agent.system_prompt) if agent and hasattr(agent, 'system_prompt') else 'Agent is not initialized. No prompt has been assembled.')
        if view == 'goal':
            return s.goal.briefing()
        if view == 'plan':
            journal = agent.journal.entries if agent and hasattr(agent, 'journal') else s.saved_journal
            return 'Decision journal (agent-recorded observations, not independently verified)\n' + TerminalActivity.preview(journal)
        if view == 'memory':
            return 'Shared findings and recent notes (not necessarily all included in the last model request)\n' + TerminalActivity.preview({
                'findings': [f.model_dump(mode='json') for f in s.memory.get_findings()],
                'notes': [e.content for e in s.memory._entries[-20:]],
            })
        cfg = s.llm_config.get_config('scanner')
        configured_model = cfg.model or getattr(get_settings(), f'{cfg.provider}_model', '') or 'provider default'
        model = getattr(getattr(agent, 'llm', None), 'model', configured_model)
        chars = sum(self.message_sizes) if self.messages is not None else sum(len(str(m.get('content', ''))) for m in history)
        return (f'CONTEXT · {label}\n'
                f'Profile: {s.profile.name} · Session: {s.session_id or "in memory"}\n'
                f'Model: {cfg.provider} / {model}\n'
                f'Scope: {", ".join(s.scope) or "not set"}\n'
                f'Spend: ${s.cost.get_total():.4f} / ${s.budget:.2f}\n'
                f'{len(messages)} messages · {chars:,} content characters · {len(self.tools)} tool schemas\n'
                f'{s.memory.findings_count} findings · {len(s.memory._entries)} shared notes\n'
                'Views: /context messages | /context N | /context tools\n'
                '       /context prompt | /context memory | /context plan | /context reasoning | /context goal\n'
                'Read-only previews; redacted and bounded. Reasoning view shows only provider-returned text.')
