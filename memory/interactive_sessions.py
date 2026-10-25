"""Atomic, local interactive session snapshots with a single-writer lease."""
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from uuid import uuid4


class InteractiveSessionStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('FANTASMA_SESSIONS_DIR', 'data/sessions'))
        self.lease = None
        self.session_id = None

    def path(self, identifier):
        if not re.fullmatch(r'[a-f0-9]{16}', identifier):
            raise ValueError('Invalid session ID; use fantasma sessions to list saved sessions')
        return self.directory / f'{identifier}.json'

    def list(self):
        records=[]
        for path in self.directory.glob('*.json'):
            try:
                data=json.loads(path.read_text())
                if isinstance(data, dict) and data.get('version') == 1 and isinstance(data.get('updated_at'), str) and self.path(data['id']) == path:
                    records.append(data)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(records, key=lambda d: d['updated_at'], reverse=True)

    def load(self, identifier):
        if identifier == 'latest':
            records=self.list()
            if not records:
                raise ValueError('No saved interactive sessions. Start one with fantasma.')
            identifier=records[0]['id']
        try:
            data=json.loads(self.path(identifier).read_text())
        except FileNotFoundError as exc:
            raise ValueError(f'No saved session {identifier}') from exc
        if data.get('version') != 1 or data.get('id') != identifier:
            raise ValueError('Unsupported or invalid interactive session snapshot')
        return data

    def acquire(self, identifier=None):
        identifier=identifier or uuid4().hex[:16]
        path=self.path(identifier)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd=os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        lease=os.fdopen(fd,'w')
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lease.close()
            raise ValueError('This session is already open in another Fantasma process') from exc
        self.lease=lease
        self.session_id=identifier
        return identifier

    def save(self, data):
        if not self.lease or data['id'] != self.session_id:
            raise ValueError('Acquire the session before saving')
        payload=dict(data, version=1, updated_at=datetime.now(timezone.utc).isoformat())
        fd, name=tempfile.mkstemp(dir=self.directory, suffix='.tmp')
        try:
            with os.fdopen(fd,'w') as file:
                json.dump(payload,file,ensure_ascii=False,default=lambda obj: obj.model_dump(mode="json") if hasattr(obj,"model_dump") else str(obj))
                file.flush()
                os.fsync(file.fileno())
            os.replace(name,self.path(self.session_id))
        finally:
            if os.path.exists(name): os.unlink(name)

    def close(self):
        if self.lease:
            self.lease.close()
            self.lease=None
