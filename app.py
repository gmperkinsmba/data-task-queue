import argparse
import json
import os
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from engine import Store, SAMPLE, propose_cleanup


def make_server(root=None, port=0):
    root = Path(root or Path.home() / '.local/share/ultimate-agent-harness').expanduser().resolve()
    if root.is_relative_to(Path(__file__).parent.resolve()):
        raise ValueError('Runtime data must be outside the checkout.')
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(root)
    public = Path(__file__).parent / 'static'

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def reply(self, value, status=200, mime='application/json'):
            data = json.dumps(value).encode() if mime == 'application/json' else value
            self.send_response(status)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def boundary(self):
            expected_host = f'127.0.0.1:{self.server.server_port}'
            origin = self.headers.get('Origin')
            try:
                parsed_origin = urlsplit(origin) if origin else None
            except ValueError:
                parsed_origin = None
                origin = '<invalid>'
            valid_origin = origin is None or (parsed_origin is not None and
                parsed_origin.scheme == 'http'
                and parsed_origin.hostname == '127.0.0.1'
                and parsed_origin.netloc == expected_host
                and parsed_origin.username is None
                and parsed_origin.password is None
            )
            if self.headers.get('Host') != expected_host or not valid_origin:
                self.reply({'error': 'Local origin required.'}, 403)
                return False
            return True

        def do_GET(self):
            if not self.boundary():
                return
            url = urlsplit(self.path)
            try:
                if url.path == '/api/state':
                    self.reply(store.state())
                elif url.path == '/api/settings':
                    self.reply(store.settings())
                elif url.path == '/api/search':
                    query = parse_qs(url.query)
                    self.reply(store.search(query.get('q', [''])[0], query.get('scope', [''])[0]))
                elif url.path in ('/', '/app.js', '/style.css', '/desktop.css'):
                    name = 'index.html' if url.path == '/' else url.path[1:]
                    mime = {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css', 'desktop.css': 'text/css'}[name]
                    self.reply((public / name).read_bytes(), mime=mime)
                else:
                    self.reply({'error': 'Not found.'}, 404)
            except ValueError as exc:
                self.reply({'error': str(exc)}, 400)

        def do_POST(self):
            if not self.boundary():
                return
            if self.headers.get('X-Local-Request') != '1' or self.headers.get('Content-Type') != 'application/json':
                return self.reply({'error': 'Local request header and JSON required.'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 262144:
                    raise ValueError('Request must contain at most 256 KiB.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Request body must be an object.')
                if self.path == '/api/import':
                    result = store.ingest(body.get('sources', SAMPLE), body.get('failpoint'))
                elif self.path == '/api/retry':
                    result = store.project()
                elif self.path == '/api/edit':
                    result = store.edit_task(body['id'], body['version'], body['title'], body['owner'], body['acceptance'])
                elif self.path == '/api/approve':
                    result = store.approve(body['id'], body['version'])
                elif self.path == '/api/complete':
                    result = store.complete(body['id'], body['version'], body['evidence'])
                elif self.path == '/api/maintenance':
                    result = store.maintenance()
                elif self.path == '/api/settings':
                    result = store.settings(body['scope'])
                elif self.path == '/api/cleanup':
                    result = propose_cleanup(body['text'], body['required'])
                elif self.path == '/api/pipeline':
                    from pipeline_adapter import run_pipeline
                    result = run_pipeline(store, body.get('recover', False))
                else:
                    return self.reply({'error': 'Not found.'}, 404)
                self.reply(result)
            except (ValueError, KeyError, TypeError, RuntimeError) as exc:
                self.reply({'error': str(exc)}, 400)

    return LocalHTTPServer(('127.0.0.1', port), Handler)


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = '127.0.0.1'
        self.server_port = self.socket.getsockname()[1]


def serve(root, port=8333):
    server = make_server(root, port)
    print(f'Ultimate Agent Harness http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8333)
    parser.add_argument('--data-dir', default=str(Path.home() / '.local/share/ultimate-agent-harness'))
    args = parser.parse_args()
    data = Path(args.data_dir).resolve()
    if data.is_relative_to(Path(__file__).parent.resolve()):
        parser.error('Runtime data must be outside the checkout.')
    os.umask(0o077)
    server = make_server(data, args.port)
    print(f'Ultimate Agent Harness http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
