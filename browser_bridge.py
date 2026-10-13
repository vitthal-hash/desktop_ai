"""Local HTTP bridge between VAANI (Python) and the VAANI Browser Bridge Chrome extension.

Design (v2):
  * The extension LONG-POLLS GET /next?wait=15  -> instant delivery, and every poll is a heartbeat.
  * Python calls POST /command (via command()) and gets the result for the SAME command id.
  * If the extension has not polled recently, commands fail in <1 s with a clear message
    instead of hanging for 12 s.
  * Unclaimed commands are removed on timeout, so a stale command can never run later.
  * Every failure says WHICH stage failed: not_connected / not_picked_up / no_result / extension_error.
  * Requests that carry a web-page Origin are rejected, so a random website cannot drive your browser.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST, PORT = "127.0.0.1", 8765
HEARTBEAT_STALE_SECONDS = 45      # extension counts as "connected" if it polled within this window
LONG_POLL_SECONDS = 15            # must stay below Chrome's 30 s service-worker idle limit
DEFAULT_TIMEOUT = 10.0

log = logging.getLogger("vaani.bridge")

_cv = threading.Condition()
_queue: deque = deque()            # commands waiting for the extension
_in_flight: dict = {}              # id -> command picked up by extension, awaiting result
_results: dict = {}                # id -> result body
_last_poll = 0.0                   # monotonic time of the last extension poll
_ever_connected = False


def _origin_ok(origin: str | None) -> bool:
    """Local Python (no Origin) and the extension are allowed. Web pages are not."""
    return origin is None or origin.startswith("chrome-extension://")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence default stderr logging
        pass

    def _send(self, status: int, data: dict | None = None):
        raw = b"" if status == 204 else json.dumps(data or {}).encode("utf-8")
        self.send_response(status)
        origin = self.headers.get("Origin")
        if origin and origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    # ---- routing -------------------------------------------------
    def do_OPTIONS(self):
        if not _origin_ok(self.headers.get("Origin")):
            return self._send(403, {"error": "forbidden origin"})
        self._send(204)

    def do_GET(self):
        global _last_poll, _ever_connected
        if not _origin_ok(self.headers.get("Origin")):
            return self._send(403, {"error": "forbidden origin"})
        url = urlparse(self.path)
        if url.path == "/next":
            wait = min(float(parse_qs(url.query).get("wait", ["0"])[0] or 0), LONG_POLL_SECONDS)
            deadline = time.monotonic() + wait
            with _cv:
                _last_poll = time.monotonic()
                _ever_connected = True
                while not _queue and time.monotonic() < deadline:
                    _cv.wait(timeout=max(0.05, deadline - time.monotonic()))
                    _last_poll = time.monotonic()
                if _queue:
                    cmd = _queue.popleft()
                    _in_flight[cmd["id"]] = cmd
                    log.info("bridge: delivered %s id=%s", cmd.get("action"), cmd["id"][:8])
                else:
                    cmd = {}
            return self._send(200, cmd)
        if url.path == "/health":
            return self._send(200, {"ok": True, "service": "VAANI browser bridge", **status()})
        if url.path == "/status":
            return self._send(200, status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not _origin_ok(self.headers.get("Origin")):
            return self._send(403, {"error": "forbidden origin"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"error": "JSON only"})
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid JSON"})

        if path == "/result":
            cid = str(body.get("id"))
            with _cv:
                _in_flight.pop(cid, None)
                _results[cid] = body
                _cv.notify_all()
            log.info("bridge: result id=%s ok=%s", cid[:8], body.get("ok"))
            return self._send(200, {"ok": True})

        if path == "/command":
            return self._send(200, _run_command(body))

        self._send(404, {"error": "not found"})


def status() -> dict:
    age = None if not _ever_connected else round(time.monotonic() - _last_poll, 1)
    connected = age is not None and age < HEARTBEAT_STALE_SECONDS
    return {"extension_connected": connected, "seconds_since_extension_poll": age,
            "queued": len(_queue), "in_flight": len(_in_flight)}


def _run_command(body: dict) -> dict:
    """Queue one command and wait for ITS result. Returns a dict with ok/stage/error/result."""
    timeout = float(body.pop("timeout", DEFAULT_TIMEOUT))
    cmd = {"id": uuid.uuid4().hex, **body}
    short = cmd["id"][:8]
    with _cv:
        if not status()["extension_connected"]:
            log.warning("bridge: %s id=%s -> extension not connected", cmd.get("action"), short)
            return {"ok": False, "stage": "not_connected", "id": cmd["id"],
                    "error": "Chrome extension isn't connected. Open Chrome, make sure the VAANI Browser "
                             "Bridge extension is enabled (chrome://extensions), then try again."}
        _queue.append(cmd)
        _cv.notify_all()
        deadline = time.monotonic() + timeout
        while cmd["id"] not in _results and time.monotonic() < deadline:
            _cv.wait(timeout=max(0.05, deadline - time.monotonic()))
        result = _results.pop(cmd["id"], None)
        if result is None:
            still_queued = cmd in _queue
            if still_queued:
                _queue.remove(cmd)           # never let a stale command run later
            _in_flight.pop(cmd["id"], None)
            stage = "not_picked_up" if still_queued else "no_result"
            msg = ("The extension didn't pick up the command in time." if still_queued else
                   "The extension received the command but never returned a result (page not responding?).")
            log.warning("bridge: %s id=%s -> %s", cmd.get("action"), short, stage)
            return {"ok": False, "stage": stage, "id": cmd["id"], "error": msg}
    if not result.get("ok", False):
        return {"ok": False, "stage": result.get("stage", "extension_error"), "id": cmd["id"],
                "error": result.get("error", "The extension reported an error.")}
    return {"ok": True, "id": cmd["id"], "result": result.get("result")}


_server = None


def start_server():
    global _server
    if _server is not None:
        return _server
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="VAANI-BrowserBridge").start()
    _server = server
    return server


# ---------------- client helpers used by tools.py -----------------
def command(action: str, timeout: float = DEFAULT_TIMEOUT, **kwargs) -> dict:
    if _server is not None:                               # same process as the server: no HTTP round trip
        try:
            return _run_command({"action": action, "timeout": timeout, **kwargs})
        except Exception as e:
            return {"ok": False, "stage": "bridge_error", "error": f"Bridge error: {e}"}
    import urllib.request
    data = json.dumps({"action": action, "timeout": timeout, **kwargs}).encode()
    req = urllib.request.Request(f"http://{HOST}:{PORT}/command", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout + 4) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "stage": "bridge_unreachable", "error": f"VAANI bridge server unreachable: {e}"}


def health() -> dict:
    """{'ok': bool (server up), 'extension_connected': bool, ...}"""
    if _server is not None:
        return {"ok": True, **status()}
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=1.5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {"ok": False, "extension_connected": False}


def browser_ready() -> bool:
    h = health()
    return bool(h.get("ok") and h.get("extension_connected"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    srv = start_server()
    print(f"VAANI browser bridge listening at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(2)
            print("status:", status(), end="\r")
    except KeyboardInterrupt:
        srv.shutdown()