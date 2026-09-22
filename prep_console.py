"""
prep_console.py - a browser cockpit for the Hamilton Microlab Prep, served from your laptop.

    python -m pip install requests websocket-client pillow
    python prep_console.py                 # then open http://localhost:8765
    python prep_console.py --ip 192.168.100.101 --port 8765

Everything goes through this small local server (so there are no CORS issues and
the login token never lives in the browser). It binds to 127.0.0.1 by default;
pass --host 0.0.0.0 only if you really want others on your network to drive the robot.
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from prep import Prep, PrepError, run_state_name

try:
    from PIL import Image
except ImportError:  # camera still works, just sends the full 5 MB PNG
    Image = None

HERE = Path(__file__).parent
PAGE = HERE / "prep_console.html"


class Hub:
    """Fans the Prep's websocket events out to any number of browser SSE clients."""

    def __init__(self, prep: Prep):
        self.prep = prep
        self.history = collections.deque(maxlen=300)
        self.cond = threading.Condition()
        self.seq = 0
        self.ws_up = {"events": False, "errors": False}
        prep.stream(self._on_event)

    def _on_event(self, channel, payload):
        if isinstance(payload, dict) and payload.get("code") == "ws-disconnected":
            self.ws_up[payload["channel"]] = False
        elif channel in self.ws_up:
            self.ws_up[channel] = True
        self.publish(channel, payload)

    def publish(self, channel, payload):
        with self.cond:
            self.seq += 1
            self.history.append((self.seq, {"t": time.time(), "ch": channel, "msg": payload}))
            self.cond.notify_all()

    def since(self, seq, wait=15.0):
        with self.cond:
            if not self.history or self.history[-1][0] <= seq:
                self.cond.wait(wait)
            return [(s, e) for s, e in self.history if s > seq]


class Console:
    def __init__(self, ip: str):
        self.prep = Prep(ip)
        self.hub = Hub(self.prep)
        self.lock = threading.Lock()
        self._instrument = None
        self._cam_cache = (0.0, None, None)

    def instrument(self):
        if self._instrument is None:
            self._instrument = self.prep.instrument()
        return self._instrument

    def state(self):
        p = self.prep
        out = {"ip": p.ip, "user": p.user, "ws": self.hub.ws_up, "time": time.time()}
        for key, fn in [("global", p.global_state), ("run", p.status), ("errors", p.pending_errors),
                        ("lighting", p.lighting), ("connection", p.connection), ("hhc", p.hhc_temperature),
                        ("ready", p.system_ready)]:
            try:
                out[key] = fn()
            except Exception as e:  # noqa: BLE001
                out[key] = None
                out.setdefault("problems", {})[key] = str(e)
        if out.get("run"):
            out["run"]["stateName"] = run_state_name(out["run"].get("protocolRunState"))
        try:
            out["instrument"] = self.instrument()
        except Exception:  # noqa: BLE001
            out["instrument"] = None
        return out

    def camera(self, rectify: bool, width: int | None):
        # Coalesce: several tabs asking at once share one 1.3 s capture.
        with self.lock:
            t, key, data = self._cam_cache
            if key == (rectify, width) and time.time() - t < 0.8:
                return data
            png = self.prep.frame(rectify)
            if Image is not None:
                im = Image.open(io.BytesIO(png))
                if width and im.width > width:
                    im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
                buf = io.BytesIO()
                im.convert("L").save(buf, "JPEG", quality=82)
                data = ("image/jpeg", buf.getvalue())
            else:
                data = ("image/png", png)
            self._cam_cache = (time.time(), (rectify, width), data)
            return data


def make_handler(console: Console):
    prep = console.prep

    actions = {
        "login":        lambda b: prep.login(b["username"], b["password"]),
        "logout":       lambda b: prep.logout(),
        "run":          lambda b: prep.create_run(int(b["id"]), simulate=bool(b.get("simulate", True))),
        "load-complete": lambda b: prep.load_complete(int(b["id"]), simulate=bool(b.get("simulate", True)),
                                                      action=b.get("action", "Run")),
        "pause":        lambda b: prep.pause(),
        "resume":       lambda b: prep.resume(),
        "abort":        lambda b: prep.abort(),
        "cleanup":      lambda b: prep.cleanup(),
        "light":        lambda b: prep.set_enclosure_rgbw(int(b["r"]), int(b["g"]), int(b["b"]), int(b.get("w", 0))),
        "light-auto":   lambda b: prep.auto_lighting(),
        "close-view":   lambda b: prep.close_view(b["viewId"], b.get("result", "Ok"), b.get("input", "")),
        "error-respond": lambda b: prep.respond_to_error(b["error"], b["response"]),
        "clear-errors": lambda b: prep.clear_errors(),
        "sim-speed":    lambda b: prep.simulation_speed(b["speed"]),
    }

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if "/api/state" not in args[0] and "/api/camera" not in args[0] and "/api/events" not in args[0]:
                super().log_message(fmt, *args)

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _guard(self, fn):
            try:
                return fn()
            except PrepError as e:
                self._send(e.status if 400 <= e.status < 600 else 502, {"error": str(e.body), "status": e.status})
            except Exception as e:  # noqa: BLE001
                self._send(502, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._guard(lambda: self._send(200, console.state()))
            if path == "/api/protocols":
                return self._guard(lambda: self._send(200, prep.protocols()))
            if path == "/api/load-instructions":
                return self._guard(lambda: self._send(200, prep.load_instructions()))
            if path == "/api/runs":
                return self._guard(lambda: self._send(200, prep.run_history()))
            if path == "/api/versions":
                return self._guard(lambda: self._send(200, prep.versions()))
            if path == "/api/camera":
                def cam():
                    ctype, data = console.camera(q.get("rectify", "1") == "1", int(q.get("w", 1400)))
                    self._send(200, data, ctype)
                return self._guard(cam)
            if path.startswith("/api/runs/") and path.endswith("/pdf"):
                rid = path.split("/")[3]
                return self._guard(lambda: self._send(200, prep.get(f"run-data/{rid}/pdf", raw=True, timeout=60).content,
                                                      "application/pdf"))
            if path == "/api/events":
                return self._sse(int(q.get("since", 0)))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            name = urlparse(self.path).path.removeprefix("/api/")
            if name not in actions:
                return self._send(404, {"error": f"unknown action {name}"})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")

            def act():
                result = actions[name](body)
                if name != "login":
                    console.hub.publish("console", {"code": "action", "action": name,
                                                    "args": {k: v for k, v in body.items() if k != "error"}})
                else:
                    result = {"user": prep.user}
                self._send(200, {"ok": True, "result": result})
            self._guard(act)

        def _sse(self, since):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    items = console.hub.since(since)
                    if not items:
                        self.wfile.write(b": ping\n\n")
                    for seq, ev in items:
                        self.wfile.write(f"id: {seq}\ndata: {json.dumps(ev, default=str)}\n\n".encode())
                        since = seq
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.100.101")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    console = Console(a.ip)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(console))
    srv.daemon_threads = True
    url = f"http://localhost:{a.port}"
    print(f"Prep console for {a.ip} -> {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
