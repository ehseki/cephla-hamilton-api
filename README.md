# Hamilton Microlab Prep — remote control

Control the Prep (PRPGD2427, software 3.4.0) from a laptop on the instrument network.

| What | Where |
|---|---|
| REST API | `http://192.168.100.101/NimbusLite/api/v1/...` |
| OpenAPI spec (279 endpoints) | `http://192.168.100.101/NimbusLite/swagger/v1/swagger.json` (download with `curl -o prep_openapi.json <that URL>`) |
| Run/dialog events (websocket) | `ws://192.168.100.101/NimbusLite/instinctevents` |
| Runtime errors (websocket) | `ws://192.168.100.101/NimbusLite/instincterrors` |

The laptop reaches it over Ethernet (laptop is `192.168.100.50`).

## Setup

```bash
python -m pip install requests websocket-client pillow
```

## Cockpit (browser UI)

```bash
python prep_console.py
```

Then open http://localhost:8765. You get a live deck camera, run state with progress and ETA, Pause / Resume / Abort, a protocol list with **Simulate** and **Run** (a real run needs you to type `RUN`), a "Deck loaded → start" button during Loading, pending errors with their response buttons, pop-ups for instrument message boxes, RGBW enclosure lighting, the live websocket event stream, and run history with PDF reports.

## CLI

```bash
python prep.py info | status | protocols | errors
python prep.py snap deck.png          # deck camera, 2592x1944
python prep.py watch                  # live events + errors
python prep.py run 1 --simulate       # dry run (default)
python prep.py run 1 --real           # moves the arm, asks for confirmation
python prep.py pause | resume | abort
python prep.py light 0 120 255 0      # enclosure R G B W
python prep.py light-auto
python prep.py axes                   # home sensor for every axis, plus door/tips/power
python prep.py home                   # home all axes (checks idle/errors/door, asks you to type HOME)
python prep.py home --allow-door-open
```

## Library

```python
from prep import Prep
p = Prep()
p.status(); p.protocols(); p.snapshot("deck.png")
p.run(1, simulate=True)               # create -> Loading -> load-complete
p.stream(lambda ch, msg: print(ch, msg))
```

## Homing

`GET /service-software-api/sensor-status` reports a home sensor for each axis: X, then Y, Z, squeeze and dispenser on each fitted head (front/rear independent channels, and the MPH if present). The cockpit's **Axes** panel shows them live.

The API has **no per-axis homing**. `POST /instruments/initialize` homes everything at once, so both the CLI and the cockpit do "home all". They refuse to start unless the instrument is Idle and there are no pending errors. With the door open you must opt in: `--allow-door-open` on the CLI, or the "Home anyway" checkbox in the cockpit. The instrument may still enforce its own door interlock.

## Gotchas found on the instrument

- The API root is `/NimbusLite/api/v1`, not `/api/v1`.
- **Auth is barely enforced.** Most reads *and some writes* (enclosure lighting, for example) work without a token. The tools log in only when the Prep returns 401. Anyone on this subnet can drive the instrument, so keep that network isolated.
- `protocolRunState` is an integer in `/protocol-run` (0 = Idle … 9 = ErrorHandling). `prep.RUN_STATES` maps it.
- Websocket payloads nest JSON inside JSON strings; `prep._deep_json` unwraps them.
- `/maintenance/close-view` only accepts `Ok`, `Yes`, `No`, `Cancel` or `Abort`.
- `/errors` only shows Instinct-level errors. Firmware errors during a run arrive on the `instincterrors` socket.
