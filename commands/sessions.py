"""List saved interactive sessions without creating models or tools."""
from commands.base import register
from memory.interactive_sessions import InteractiveSessionStore


@register('sessions', 'List saved interactive sessions')
async def run(args, orchestrator):
    records=InteractiveSessionStore().list()
    if not records:
        print('No saved interactive sessions. Start one with fantasma.')
        return
    for record in records:
        scope=', '.join(record.get('scope', [])) or '(no scope)'
        print(f"{record['id']}  {record['updated_at']}  {record.get('status', 'idle')}  {scope}")
    print('\nResume: fantasma full --resume-session ID (omit ID for latest)')
