"""Test helper: serve a CRMStore over real HTTP using only the stdlib, so the
extractor is tested against a live socket (retries, status codes, headers)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from mock_crm_api.store import CRMStore


def start_server(store: CRMStore):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            resource = url.path.strip("/")
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                limit = int(q.get("limit", 100))
            except ValueError:
                return self._send(400, {"error": "limit must be an integer"}, {})
            result = store.api_list(resource, q.get("updated_since"), q.get("after"), limit)
            self._send(result.status, result.body, result.headers)

        def do_POST(self):
            parts = urlparse(self.path).path.strip("/").split("/")
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "invalid JSON"}, {})
            if len(parts) == 2 and parts[1] == "batch_update":
                result = store.batch_update(parts[0], body.get("inputs"))
                return self._send(result.status, result.body, result.headers)
            self._send(404, {"error": "not found"}, {})

        def _send(self, status, body, headers):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # keep test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
