"""
prep.py - Python client + CLI for the Hamilton Microlab Prep REST API.

Discovered on the instrument itself (software 3.4.0):
    REST      http://<ip>/NimbusLite/api/v1/...
    OpenAPI   http://<ip>/NimbusLite/swagger/v1/swagger.json   (saved as prep_openapi.json)
    Events    ws://<ip>/NimbusLite/instinctevents   (run status, message boxes, finished, time remaining)
    Errors    ws://<ip>/NimbusLite/instincterrors   (runtime errors that need a response)

Many read-only endpoints work without logging in; anything that changes state
needs a Bearer token from POST /authenticate (same account as the touchscreen).

Library:
    from prep import Prep
    p = Prep()                      # default IP 192.168.100.101
    p.login("user", "pw")
    p.status()                      # RunStatusDto
    p.snapshot("deck.png")          # deck camera
    p.run(1002, simulate=True)      # create -> wait for Loading -> load-complete

CLI:
    python prep.py status
    python prep.py protocols
    python prep.py snap deck.png
    python prep.py watch                     # live event + error stream
    python prep.py run 1002 --simulate       # dry run (default)
    python prep.py run 1002 --real           # moves hardware; asks for confirmation
    python prep.py pause | resume | abort
    python prep.py light 0 80 255 0          # enclosure RGBW (0-255)
    python prep.py axes                      # home-sensor state of every axis
    python prep.py home                      # home all axes (asks for confirmation)
    python prep.py home --allow-door-open    # ...even with the enclosure door open
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
import time
from typing import Any, Callable, Iterable

import requests

DEFAULT_IP = os.environ.get("PREP_IP", "192.168.100.101")
RUN_STATES = ["Idle", "Scheduling", "Scheduled", "Loading", "Running",
              "Pausing", "Paused", "Aborting", "Reloading", "ErrorHandling"]


class PrepError(RuntimeError):
    def __init__(self, resp: requests.Response):
        self.status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        self.body = body
        super().__init__(f"{resp.request.method} {resp.url} -> {resp.status_code}: {str(body)[:300]}")


def run_state_name(value: Any) -> str:
    """The API returns ProtocolRunState as an int in some places and a string in others."""
    if isinstance(value, int) and 0 <= value < len(RUN_STATES):
        return RUN_STATES[value]
    return str(value)


class Prep:
    def __init__(self, ip: str = DEFAULT_IP, timeout: float = 15):
        self.ip = ip
        self.base = f"http://{ip}/NimbusLite/api/v1"
        self.ws_base = f"ws://{ip}/NimbusLite"
        self.timeout = timeout
        self.s = requests.Session()
        self.user: str | None = None
        self.token_expires_at: float | None = None

    # ---------------------------------------------------------------- plumbing
    def _req(self, method: str, path: str, *, raw: bool = False, timeout: float | None = None, **kw):
        r = self.s.request(method, f"{self.base}/{path.lstrip('/')}", timeout=timeout or self.timeout, **kw)
        if not r.ok:
            raise PrepError(r)
        if raw:
            return r
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def get(self, path, **kw):    return self._req("GET", path, **kw)
    def post(self, path, **kw):   return self._req("POST", path, **kw)
    def put(self, path, **kw):    return self._req("PUT", path, **kw)
    def delete(self, path, **kw): return self._req("DELETE", path, **kw)

    # ---------------------------------------------------------------- auth
    def login(self, username: str, password: str) -> dict:
        r = self.post("authenticate", json={"Username": username, "Password": password})
        self.s.headers["Authorization"] = f"Bearer {r['token']}"
        self.user = r.get("userName") or username
        if r.get("expiresIn"):
            self.token_expires_at = time.time() + float(r["expiresIn"])
        return r

    def use_token(self, token: str) -> None:
        self.s.headers["Authorization"] = f"Bearer {token}"

    @property
    def token(self) -> str | None:
        h = self.s.headers.get("Authorization", "")
        return h[7:] if h.startswith("Bearer ") else None

    def renew(self) -> dict:
        r = self.post("authenticate/renew-token", json={})
        self.s.headers["Authorization"] = f"Bearer {r['token']}"
        if r.get("expiresIn"):
            self.token_expires_at = time.time() + float(r["expiresIn"])
        return r

    def is_authenticated(self) -> bool:
        if not self.token:
            return False
        try:
            self.get("authenticate/check-authentication")
            return True
        except PrepError:
            return False

    def logout(self) -> None:
        try:
            self.delete("authenticate")
        finally:
            self.s.headers.pop("Authorization", None)
            self.user = None

    # ---------------------------------------------------------------- read-only state
    def system_ready(self):        return self.get("system-ready")
    def versions(self):            return self.get("software-versions")
    def instrument(self):          return self.get("instruments")
    def connection(self):          return self.get("instruments/connection-status")
    def global_state(self) -> str: return self.get("instruments/global-run-state")
    def status(self):              return self.get("protocol-run")
    def protocols(self):           return self.get("protocols/names")
    def protocol(self, pid: int, full: bool = False):
        return self.get(f"protocols/{pid}", params={"includeFullStepData": full})
    def validate(self, pid: int):  return self.get(f"protocols/validate/{pid}")
    def pending_errors(self):      return self.get("errors/check-for-pending-errors")
    def error_log(self, limit=50): return self.get("errors", params={"offset": 0, "limit": limit})
    def lighting(self):            return self.get("lighting")
    def hhc_temperature(self):     return self.get("thermal-device/hhc/temperature-status")
    def load_instructions(self):   return self.get("protocol-run/load-instructions")
    def run_history(self):         return self.get("run-data")
    def run_report_pdf(self, run_id: str, path: str):
        return self._save(f"run-data/{run_id}/pdf", path)
    def run_pipetting_csv(self, run_id: str, path: str):
        return self._save(f"run-data/{run_id}/pipetting-csv", path)

    # ---------------------------------------------------------------- camera
    def frame(self, rectify: bool = True) -> bytes:
        """Full-resolution PNG (2592x1944 grayscale) of the deck."""
        return self.get(f"camera/{str(rectify).lower()}", raw=True, timeout=30).content

    def position_frame(self, position: int, padding: int = 20, z: float = 0) -> bytes:
        """Cropped PNG of a single deck position (1-8)."""
        return self.get(f"camera/position/{position}/{padding}/{z}", raw=True, timeout=30).content

    def snapshot(self, path: str = "deck.png", rectify: bool = True) -> str:
        with open(path, "wb") as f:
            f.write(self.frame(rectify))
        return path

    def scan_deck(self, matches: int = 3):
        """Camera-based labware recognition across the deck."""
        return self.get(f"deck/scan/{matches}", timeout=120)

    # ---------------------------------------------------------------- run control
    def create_run(self, pid: int, simulate: bool = True, recompile: bool = False):
        return self.post("protocol-run/create",
                         json={"ProtocolId": pid, "Simulate": simulate, "Recompile": recompile}, timeout=120)

    def load_complete(self, pid: int, simulate: bool = True, action: str = "Run", **extra):
        body = {"ProtocolId": pid, "Simulate": simulate, "LoadCompleteAction": action,
                "ResidualTips": [], "Barcodes": [], "LiquidVolumes": []}
        body.update(extra)
        return self.put("protocol-run/load-complete", json=body, timeout=60)

    def pause(self):   return self.put("protocol-run/pause")
    def resume(self):  return self.put("protocol-run/resume")
    def abort(self):   return self.put("protocol-run/abort")
    def cleanup(self): return self.put("protocol-run/cleanup-unloading")
    def initialize(self): return self.post("instruments/initialize", timeout=180)

    # ---------------------------------------------------------------- axes / homing
    def sensors(self):     return self.get("service-software-api/sensor-status")
    def is_parked(self):   return self.get("service-software-api/is-parked")
    def has_tips(self):    return self.get("service-software-api/has-tips")
    def power_ready(self): return self.get("power/is-initialized")

    def axes(self, sensors: dict | None = None) -> dict[str, bool]:
        """Home-sensor state per axis, e.g. {'X': True, 'Front Y': False, ...}.
        Only heads that are fitted (MPH and/or independent channels) are listed."""
        s = sensors if sensors is not None else self.sensors()
        out = {"X": bool(s.get("isXHome"))}
        parts = ("yHome", "Y"), ("zHome", "Z"), ("squeezeHome", "Squeeze"), ("dispenserHome", "Dispenser")
        mph = s.get("mphSensorState") or {}
        if mph.get("present"):
            out.update({f"MPH {name}": bool(mph.get(k)) for k, name in parts})
        ind = s.get("independentChannelsSensorState") or {}
        if ind.get("present"):
            for side in ("front", "rear"):
                ch = ind.get(f"{side}Channel") or {}
                out.update({f"{side.title()} {name}": bool(ch.get(k)) for k, name in parts})
        return out

    def home_preflight(self, allow_door_open: bool = False) -> list[str]:
        """Reasons it is not safe to home right now (empty list = OK).
        allow_door_open skips the door check (the instrument may still enforce its own interlock)."""
        problems = []
        if self.global_state() != "Idle":
            problems.append(f"instrument is not Idle ({self.global_state()})")
        s = self.sensors()
        if s.get("isEnclosurePresent") and not s.get("isDoorClosed") and not allow_door_open:
            problems.append("enclosure door is open")
        if self.pending_errors():
            problems.append("there are pending errors - handle them first")
        return problems

    def home_all(self, wait: bool = True, timeout: float = 180, allow_door_open: bool = False,
                 on_tick: Callable[[dict[str, bool]], None] | None = None) -> dict[str, bool]:
        """Home every axis (the API only exposes a whole-instrument initialize, not per-axis homing).
        Raises RuntimeError if preflight fails. Returns the final axis map."""
        problems = self.home_preflight(allow_door_open)
        if problems:
            raise RuntimeError("not homing: " + "; ".join(problems))
        done = threading.Event()
        err: list[BaseException] = []

        def go():
            try:
                self.initialize()
            except BaseException as e:  # noqa: BLE001
                err.append(e)
            finally:
                done.set()

        threading.Thread(target=go, daemon=True).start()
        if not wait:
            return self.axes()
        deadline = time.time() + timeout
        while not done.wait(1.0):
            if on_tick:
                on_tick(self.axes())
            if time.time() > deadline:
                raise TimeoutError("initialize did not return in time")
        if err:
            raise err[0]
        final = self.axes()
        if on_tick:
            on_tick(final)
        return final

    def simulation_speed(self, speed: str | None = None):
        if speed is None:
            return self.get("protocol-run/simulation-speed")
        return self.put("protocol-run/simulation-speed", json={"SimulationSpeed": speed})

    def wait_for_state(self, states: Iterable[str], timeout: float = 600, poll: float = 1.0,
                       on_tick: Callable[[dict], None] | None = None) -> dict:
        states = set(states)
        deadline = time.time() + timeout
        while True:
            st = self.status()
            if on_tick:
                on_tick(st)
            if run_state_name(st.get("protocolRunState")) in states:
                return st
            if time.time() > deadline:
                raise TimeoutError(f"state still {run_state_name(st.get('protocolRunState'))}")
            time.sleep(poll)

    def run(self, pid: int, simulate: bool = True, auto_load: bool = True,
            on_tick: Callable[[dict], None] | None = None) -> dict:
        """Create a run, wait for the Loading stage, confirm loading, and return the status.

        With auto_load=True the deck is assumed to be loaded per the protocol's
        load instructions already. Returns once the run is Running (or finished)."""
        self.create_run(pid, simulate=simulate)
        st = self.wait_for_state({"Loading", "Running", "Idle", "ErrorHandling"}, timeout=300, on_tick=on_tick)
        if run_state_name(st["protocolRunState"]) == "Loading" and auto_load:
            self.load_complete(pid, simulate=simulate)
            st = self.wait_for_state({"Running", "Idle", "ErrorHandling"}, timeout=120, on_tick=on_tick)
        return st

    # ---------------------------------------------------------------- dialogs & errors
    def close_view(self, view_id: str, result: str = "Ok", text: str = ""):
        """Answer a maintenance/message-box dialog. result: Ok, Yes, No, Cancel, Abort."""
        return self.post("maintenance/close-view", json={"ViewId": view_id, "ViewResult": result, "ViewInput": text})

    def respond_to_error(self, error: dict, response: str):
        """Answer a pending runtime error (as returned by pending_errors()) with one of its ErrorResponses."""
        items = error.get("errorData") or [{"id": error.get("id")}]
        body = {"Id": error.get("id"), "GroupId": error.get("groupId"), "SelectedResponse": response,
                "ErrorData": [{"Id": d.get("id"), "SelectedResponse": response} for d in items]}
        return self.put("errors/runtime", json=body)

    def clear_errors(self): return self.put("errors/clear-errors")

    # ---------------------------------------------------------------- environment
    def set_enclosure_rgbw(self, r: int, g: int, b: int, w: int = 0):
        self.post("enclosure/set-custom-lighting")
        return self.post("enclosure", json={"Red": r, "Green": g, "Blue": b, "White": w})

    def auto_lighting(self): return self.post("enclosure/set-automatic-lighting")

    def hhc_start(self, celsius: int): return self.put("thermal-device/hhc/start-temperature-control", json=celsius)
    def hhc_stop(self):                return self.put("thermal-device/hhc/stop-temperature-control")

    # ---------------------------------------------------------------- live events
    def stream(self, on_event: Callable[[str, Any], None], stop: threading.Event | None = None) -> threading.Event:
        """Subscribe to both websockets in background threads. on_event(channel, payload)
        is called with channel 'events' or 'errors'. Reconnects automatically. Returns the stop event."""
        import websocket  # websocket-client

        stop = stop or threading.Event()

        def pump(channel: str, path: str):
            while not stop.is_set():
                try:
                    ws = websocket.create_connection(f"{self.ws_base}/{path}", timeout=5)
                    ws.settimeout(1)
                    while not stop.is_set():
                        try:
                            msg = ws.recv()
                        except websocket.WebSocketTimeoutException:
                            continue
                        on_event(channel, _deep_json(msg))
                    ws.close()
                except Exception as e:  # noqa: BLE001 - keep the stream alive
                    on_event("system", {"code": "ws-disconnected", "channel": channel, "error": str(e)})
                    stop.wait(3)

        for ch, path in (("events", "instinctevents"), ("errors", "instincterrors")):
            threading.Thread(target=pump, args=(ch, path), daemon=True).start()
        return stop

    # ---------------------------------------------------------------- helpers
    def _save(self, path: str, out: str) -> str:
        with open(out, "wb") as f:
            f.write(self.get(path, raw=True, timeout=60).content)
        return out


def _deep_json(value: Any) -> Any:
    """Websocket payloads nest JSON inside JSON strings (e.g. message-box data); unwrap all levels."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "{[\"":
            try:
                return _deep_json(json.loads(s))
            except ValueError:
                return value
        return value
    if isinstance(value, dict):
        return {k: _deep_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_json(v) for v in value]
    return value


# ====================================================================== CLI
def _login_interactive(p: Prep) -> None:
    user = os.environ.get("PREP_USER") or input("Prep username: ")
    pw = os.environ.get("PREP_PASSWORD") or getpass.getpass("Prep password: ")
    p.login(user, pw)


def _authed(p: Prep, fn: Callable[[], Any]) -> Any:
    """Many Prep endpoints don't enforce auth; only log in if the instrument says 401."""
    try:
        return fn()
    except PrepError as e:
        if e.status != 401:
            raise
        _login_interactive(p)
        return fn()


def _fmt_status(st: dict) -> str:
    state = run_state_name(st.get("protocolRunState"))
    if not st.get("isRunInProgress") and state == "Idle":
        return "Idle - no run in progress"
    sim = " [SIMULATION]" if st.get("isSimulation") else ""
    return (f"{state}{sim}  protocol={st.get('protocolName')!r} (id {st.get('protocolId')})  "
            f"step={st.get('currentStep')}  eta={st.get('estimatedEndTime')}")


def _print_axes(ax: dict[str, bool]) -> None:
    for k, v in ax.items():
        print(f"  {k:18} {'HOME' if v else 'not home'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hamilton Microlab Prep remote control")
    ap.add_argument("--ip", default=DEFAULT_IP)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("info")
    sub.add_parser("protocols")
    sp = sub.add_parser("snap"); sp.add_argument("path", nargs="?", default="deck.png"); sp.add_argument("--raw", action="store_true")
    sub.add_parser("watch")
    sub.add_parser("errors")
    rp = sub.add_parser("run"); rp.add_argument("protocol_id", type=int)
    g = rp.add_mutually_exclusive_group(); g.add_argument("--simulate", action="store_true", default=True); g.add_argument("--real", action="store_true")
    for c in ("pause", "resume", "abort"):
        sub.add_parser(c)
    lp = sub.add_parser("light"); [lp.add_argument(c, type=int) for c in "rgbw"]
    sub.add_parser("light-auto")
    sub.add_parser("axes")
    hp = sub.add_parser("home"); hp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    hp.add_argument("--allow-door-open", action="store_true", help="home even if the enclosure door is open")
    a = ap.parse_args(argv)
    p = Prep(a.ip)

    if a.cmd == "status":
        print(f"global: {p.global_state()}")
        print(_fmt_status(p.status()))
        errs = p.pending_errors()
        print(f"pending errors: {len(errs)}")
        for e in errs:
            print(f"  ! {e.get('title')}: {e.get('message')}  responses={[r.get('response') for r in e.get('errorResponses') or []]}")
    elif a.cmd == "info":
        inst = p.instrument()
        print(f"{inst['instrumentName']}  serial {inst['serialNumber']}  controller {inst['endpoint']}  enclosure={inst['isEnclosurePresent']}")
        for v in p.versions():
            print(f"  {v['name']:36} {v['version']}")
        c = p.connection()
        print(f"controlled by {c['controllerMachineName']} ({c['controllerUserName']})" if c["isControlled"] else "not controlled")
    elif a.cmd == "protocols":
        for pr in p.protocols():
            print(f"{pr['protocolId']:>6}  {pr['name']}")
    elif a.cmd == "snap":
        print("saved", p.snapshot(a.path, rectify=not a.raw))
    elif a.cmd == "errors":
        print(json.dumps(p.pending_errors(), indent=2))
    elif a.cmd == "watch":
        print(f"watching {p.ws_base} (Ctrl+C to stop)")
        stop = p.stream(lambda ch, m: print(f"{time.strftime('%H:%M:%S')} [{ch}] {json.dumps(m)[:400]}", flush=True))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop.set()
    elif a.cmd == "run":
        simulate = not a.real
        if not simulate:
            name = next((x["name"] for x in p.protocols() if x["protocolId"] == a.protocol_id), "?")
            if input(f"REAL run of {name!r} - the arm WILL move. Type RUN to continue: ").strip() != "RUN":
                print("cancelled"); return
        last = [None]
        def tick(st):
            line = _fmt_status(st)
            if line != last[0]:
                print(time.strftime("%H:%M:%S"), line, flush=True); last[0] = line
        _authed(p, lambda: p.run(a.protocol_id, simulate=simulate, on_tick=tick))
        p.wait_for_state({"Idle"}, timeout=24 * 3600, poll=2, on_tick=tick)
        print("finished")
    elif a.cmd in ("pause", "resume", "abort"):
        print(_authed(p, getattr(p, a.cmd)))
    elif a.cmd == "light":
        _authed(p, lambda: p.set_enclosure_rgbw(a.r, a.g, a.b, a.w)); print("ok")
    elif a.cmd == "light-auto":
        _authed(p, p.auto_lighting); print("ok")
    elif a.cmd == "axes":
        _print_axes(p.axes())
        s = p.sensors()
        print(f"door {'closed' if s.get('isDoorClosed') else 'OPEN'}  parked={p.is_parked()}  tips={p.has_tips()}  power-initialized={p.power_ready()}")
    elif a.cmd == "home":
        _print_axes(p.axes())
        problems = p.home_preflight(a.allow_door_open)
        if problems:
            sys.exit("not homing: " + "; ".join(problems) + ("  (use --allow-door-open to override)" if any("door" in x for x in problems) else ""))
        door_open = not p.sensors().get("isDoorClosed")
        if door_open:
            print("WARNING: the door is OPEN - keep hands and objects out of the deck.")
        if not a.yes and input("Home ALL axes? The gantry and channels will move. Type HOME: ").strip() != "HOME":
            print("cancelled"); return
        last = [None]
        def tick(ax):
            line = "  ".join(f"{k}:{'ok' if v else '..'}" for k, v in ax.items())
            if line != last[0]:
                print(time.strftime("%H:%M:%S"), line, flush=True); last[0] = line
        final = _authed(p, lambda: p.home_all(on_tick=tick, allow_door_open=a.allow_door_open))
        missing = [k for k, v in final.items() if not v]
        print("all axes home" if not missing else f"initialize returned, but not at home sensor: {', '.join(missing)}")


if __name__ == "__main__":
    try:
        main()
    except PrepError as e:
        sys.exit(f"error: {e}")
