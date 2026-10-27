"""Loopback-only receipt fixture: one access-control flaw and a fixed control."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread


@contextmanager
def receipt_fixture(vulnerable):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get('Authorization') != 'Bearer alice-demo-token':
                status, body = 401, {'error': 'authentication_required'}
            elif self.path not in ('/receipts/1', '/receipts/2'):
                status, body = 404, {'error': 'not_found'}
            elif self.path == '/receipts/2' and not vulnerable:
                status, body = 403, {'error': 'forbidden'}
            else:
                owner = 'alice' if self.path.endswith('/1') else 'bob'
                status, body = 200, {'id': int(self.path[-1]), 'owner': owner,
                                     'amount': 25 if owner == 'alice' else 700,
                                     'note': 'synthetic receipt for ' + owner}
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
