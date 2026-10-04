"""A loopback bridge must remain local even when HTTP proxy variables are set."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from test_companion import state_data

from companion.bridge import BridgeClient


def test_loopback_bridge_bypasses_environment_proxy(tmp_path, monkeypatch):
    received = []

    class LocalBridge(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization")))
            data = json.dumps(state_data()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), LocalBridge)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    config = tmp_path / "bridge.local.json"
    config.write_text(json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}",
                                  "token": "synthetic-loopback-token-only"}), encoding="utf-8")

    async def scenario():
        client = BridgeClient(config)
        try:
            assert (await client.state()).ready
        finally:
            await client.close()

    try:
        asyncio.run(scenario())
        assert received == [("/state", "Bearer synthetic-loopback-token-only")]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
