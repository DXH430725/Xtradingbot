#!/usr/bin/env python3
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


STATE = {
    # name -> {
    #   'last_at': epoch_seconds,
    #   'count': int,
    #   'last_payload': dict,
    # }
}
STATE_LOCK = threading.Lock()


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "TelemetryReceiver/1.0"

    def log_message(self, fmt, *args):
        # keep output concise
        return

    def _send(self, code=200, body: bytes = b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            with STATE_LOCK:
                now = time.time()
                status = {}
                for name, info in STATE.items():
                    last_at = info.get("last_at") or 0
                    age = now - last_at
                    status[name] = {
                        "last_at": last_at,
                        "age_sec": round(age, 2),
                        "count": int(info.get("count") or 0),
                        "ok": age < 2 * 60,  # heartbeat in last 2 minutes
                    }
            self._send(200, _json_bytes({"ok": True, "bots": status}))
            return
        self._send(404, _json_bytes({"ok": False, "error": "not found"}))

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/ingest/"):
            name = parsed.path.split("/", 2)[-1]
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._send(400, _json_bytes({"ok": False, "error": "invalid json"}))
                return
            with STATE_LOCK:
                prev = STATE.get(name) or {"count": 0}
                info = {
                    "last_at": time.time(),
                    "count": int(prev.get("count") or 0) + 1,
                    "last_payload": payload,
                }
                STATE[name] = info
            # minimal ack
            self._send(200, _json_bytes({"ok": True}))
            return
        self._send(404, _json_bytes({"ok": False, "error": "not found"}))


def run(host="0.0.0.0", port=10123):
    httpd = HTTPServer((host, port), Handler)
    print(f"Telemetry receiver listening on http://{host}:{port}  (GET /health, POST /ingest/<name>)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()

