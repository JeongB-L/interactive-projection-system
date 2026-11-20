# Protocol (the one shared contract)

Version 1 (as implemented). JSON text messages over WebSocket; JPEG frames as binary messages.
JSON was chosen over Protobuf for the prototype: no code generation, easy to read in any tool, and a hand frame is only about 1.2 KB. If bandwidth ever matters, only the encoder/decoder changes.

Hub: `http://<laptop>:8080`. Everything below is a WebSocket at `ws://<laptop>:8080<path>`.

| Path | Peer | Direction |
|---|---|---|
| `/edge` | Pi agent | agent → hub: hello, hands, frames, calib_pattern, calib_result. hub → agent: hello_ack, action (only if `os_pointer`), pong, calib_capture, calib_cancel |
| `/display` | projected page (browser on the Pi) | hub → page: content, action, calib, calib_pattern, brightness, hb |

## Pi agent → hub (`/edge`)

First message must be `hello`:
```json
{"t":"hello","device":"pi5","source":"picamera","cameras":[0,1],"os_pointer":false,"token":""}
```
`token` must equal `IPS_TOKEN` from the hub's `.env` if that is set. The hub replies
`{"t":"hello_ack","session":3,"record_frames":false}`; a session row and a recording start here.

One message per camera per frame (about 30/s per camera):
```json
{"t":"hands","cam":0,"seq":1042,"ts":1792867322311000,"track_ms":9.8,
 "hands":[{"right":true,"score":0.95,"pts":[[0.51,0.62,0.0], ...21 entries...]}]}
```
- `cam` camera index; `seq` per-camera counter (gaps are counted as drops).
- `ts` capture time, **wall-clock microseconds** (so end-to-end latency can be measured; needs NTP-synced clocks between Pi and hub).
- `track_ms` time the Pi spent on tracking (feeds the Pi-vs-other benchmark).
- `pts` MediaPipe order, normalized image coordinates. An empty `hands` list means "no hand seen".
- If a hand frame has several hands, the hub's own recognizer uses the one with the highest `score`.
- Optional `"motion"` field: the result of Johnny's motion engine for this frame, computed by the agent from the same
  landmarks (the agent can switch it off with `--no-motion`):
  ```json
  "motion":{"detected_hands":1,
            "hands":[{"id":1,"handedness":"Right","detected":true,"predicted":false,"tip":[0.51,0.62],"raw_tip":[0.5,0.6],
                      "center":[0.5,0.7],"velocity":[0.0,0.0],"pinching":false,"last_seen_ms_ago":0.0}],
            "events":[{"type":"PINCH_START","hand_id":1}]}
  ```
  Event types: `HAND_FOUND`, `PINCH_START`, `PINCH_END` (with `hand_id`), `SWIPE_LEFT|RIGHT|UP|DOWN`, `ZOOM_IN`, `ZOOM_OUT`.
  Coordinates are normalized like the landmarks. Hubs and recordings that do not know the field ignore it.

Optional JPEG frame (binary WebSocket message): `[1 byte camera index][JPEG bytes]`. The hub stores these only when `record.frames` is true in `config.json`.

`{"t":"ping","ts":N}` → `{"t":"pong","ts":N}`.

### Automatic surface calibration (`ips.py calibrate auto`)

The agent owns the camera, so it measures; the hub starts it, relays the pattern and installs the result
(code: `edge/surface_calib.py`, `Hub::calibrate_auto` / `on_calib_result`).
```json
hub → agent  {"t":"calib_capture","id":3,"screen":[1920,1080]}
agent → hub  {"t":"calib_pattern","id":3,"show":true,"grid":6,
              "markers":[{"id":0,"x0":0.07,"y0":0.08,"x1":0.17,"y1":0.27,"bits":"1011000101100110"}, ...]}
agent → hub  {"t":"calib_result","id":3,"screen":[1920,1080],
              "results":[{"cam":0,"ok":true,"h":[9 numbers],"rms_px":1.6,"max_px":3.2,"quality":"good",
                          "markers":15,"of":15,"frames":8,"inliers":60}]}
             (a failed camera: {"cam":1,"ok":false,"reason":"pattern not found in the camera image within 12 s"})
hub → agent  {"t":"calib_cancel","id":3}
```
- Marker positions are fractions of the screen; corners in ArUco order (top-left, top-right, bottom-right, bottom-left).
  `bits` are the marker's inner 4x4 cells row by row (`1` = white), from OpenCV's `DICT_4X4_50`.
- `h` maps a **normalized camera point** (same space as the hand landmarks, after `--mirror`) to a **fraction of the
  screen**, row-major. The hub checks it (9 finite numbers, not singular) before installing it, and ignores results
  whose `id` is not the request it is waiting for.
- Sources without images (`synthetic`, `replay`) answer `calib_capture` at once with `ok:false`.

## Hub → display page (`/display`)

```json
{"t":"content","url":"/apps/snake.html","app":"snake"}          switch the app shown in the page
{"t":"action","kind":"move|down|up|swipe|zoom|home|hide","x":960,"y":540,"detail":"left"}
{"t":"calib","step":0,"total":4,"x":192,"y":108}                 show a calibration marker
{"t":"calib","done":true,"cameras_calibrated":2}
{"t":"calib","done":true,"auto":true,"cameras_calibrated":0,"error":"..."}  automatic calibration failed
{"t":"calib_pattern","show":true,"grid":6,"markers":[...]}           cover the screen with the marker pattern
{"t":"calib_pattern","show":false}
{"t":"brightness","value":0.6}
{"t":"hb"}                                                        heartbeat, every second
```
`x`,`y` are projector pixels in the display page's own coordinate system (the page reports its size with `POST /api/display_size`). `detail` is the swipe direction (`left|right|up|down`) or the zoom direction (`in|out`); the page passes them to the app as the `ips-swipe` / `ips-zoom` events.

The same `action` messages go to the agent when it said `os_pointer:true`, so a real browser or any window on the Pi can be driven with OS mouse events.

## HTTP API (hub)

| Call | Meaning |
|---|---|
| `GET /api/status` | live numbers: fps, latency, drops, gestures acted on, `gesture_source`, `compare` (what each engine detected), `motion_frames`, cameras, storage base |
| `GET /api/apps` | configured apps |
| `POST /api/launch {"app":"snake"}` | switch app (id, `launcher`, or a URL) |
| `POST /api/home` | back to launcher |
| `POST /api/calibrate/auto` | automatic surface calibration; `{"ok":true,"id":n}`, or 409 with `error` when no agent is connected. The outcome appears in `/api/status` → `auto_calib.last` |
| `POST /api/calibrate/cancel` | stop a running automatic calibration (takes the pattern down) |
| `POST /api/calibrate/set {"cam":0,"h":[9 numbers]}` | install a mapping measured offline (`tools/surface_calib.py fit --apply`) |
| `POST /api/calibrate/pattern {"show":true,"grid":6,"markers":[...]}` | show / hide the pattern without measuring |
| `POST /api/calibrate/start` · `/reset` | the older pinch-at-4-markers calibration · forget the calibration |
| `POST /api/brightness {"value":0.5}` | dim the projected image |
| `POST /api/display_size {"w":1920,"h":1080}` | sent by the display page |
| `POST /api/ai {"prompt":"..."}` | ask the AI (key read from the hub's `.env`) |
| `GET /api/sessions` · `GET /api/assets?kind=video_mjpeg` | what the database knows |
| `POST /api/shutdown` | flush recordings and exit |

Static: `/ui/display.html`, `/status`, `/apps/*.html`.

## Who may call what

| Caller | Allowed |
|---|---|
| This computer (loopback) | everything |
| Another machine, no token | `GET /api/status`, `GET /api/apps`, static pages, `POST /api/display_size`, `POST /api/home`, `POST /api/launch` of a configured app, the `/display` socket, the `/edge` socket (checked by the hello token when `IPS_TOKEN` is set) |
| Another machine **with** `X-IPS-Token: <IPS_TOKEN>` | everything |
| Another machine, wrong or no token, sensitive call | HTTP 403 `{"ok":false,"error":"forbidden: ..."}` |

Sensitive = `POST /api/shutdown`, `/api/ai`, every `/api/calibrate/...` call, `/api/brightness`, `/api/launch` with a URL that is not a configured app, `GET /api/sessions`, `GET /api/assets`.
URLs under `/ui/` and `/apps/` can only name files inside the `ui/` folder; anything else is a 404.
A malformed message on `/edge` is counted in `bad_messages` and skipped; it never closes the connection.
Large messages may reach a client split into several WebSocket frames (normal; browsers and the agent's client reassemble them).

## Rules
- The hub always uses the newest data: a camera not heard from for `stale_ms` (150 ms) is ignored, so a stalled camera cannot move the cursor.
- Click/swipe/home actions are never merged or dropped; `move` actions are suppressed inside the dead zone.
- Add fields freely; unknown fields are ignored.
- Secrets never travel over this protocol except the optional `token`.
