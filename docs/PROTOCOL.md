# Protocol (the one shared contract)

Version 1 (as implemented). JSON text messages over WebSocket; JPEG frames as binary messages.
JSON was chosen over Protobuf for the prototype: no code generation, easy to read in any tool, and a hand frame is only about 1.2 KB. If bandwidth ever matters, only the encoder/decoder changes.

Hub: `http://<laptop>:8080`. Everything below is a WebSocket at `ws://<laptop>:8080<path>`.

| Path | Peer | Direction |
|---|---|---|
| `/edge` | Pi agent | agent → hub: hello, hands, frames. hub → agent: hello_ack, action (only if `os_pointer`), pong |
| `/display` | projected page (browser on the Pi) | hub → page: content, action, calib, brightness, hb |

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
- If a hand frame has several hands, the hub uses the one with the highest `score`.

Optional JPEG frame (binary WebSocket message): `[1 byte camera index][JPEG bytes]`. The hub stores these only when `record.frames` is true in `config.json`.

`{"t":"ping","ts":N}` → `{"t":"pong","ts":N}`.

## Hub → display page (`/display`)

```json
{"t":"content","url":"/apps/snake.html","app":"snake"}          switch the app shown in the page
{"t":"action","kind":"move|down|up|swipe|home|hide","x":960,"y":540,"detail":"left"}
{"t":"calib","step":0,"total":4,"x":192,"y":108}                 show a calibration marker
{"t":"calib","done":true,"cameras_calibrated":2}
{"t":"brightness","value":0.6}
{"t":"hb"}                                                        heartbeat, every second
```
`x`,`y` are projector pixels in the display page's own coordinate system (the page reports its size with `POST /api/display_size`). `detail` is the swipe direction.

The same `action` messages go to the agent when it said `os_pointer:true`, so a real browser or any window on the Pi can be driven with OS mouse events.

## HTTP API (hub)

| Call | Meaning |
|---|---|
| `GET /api/status` | live numbers: fps, latency, drops, gestures, cameras, storage base |
| `GET /api/apps` | configured apps |
| `POST /api/launch {"app":"snake"}` | switch app (id, `launcher`, or a URL) |
| `POST /api/home` | back to launcher |
| `POST /api/calibrate/start` · `/reset` | calibration |
| `POST /api/brightness {"value":0.5}` | dim the projected image |
| `POST /api/display_size {"w":1920,"h":1080}` | sent by the display page |
| `POST /api/ai {"prompt":"..."}` | ask the AI (key read from the hub's `.env`) |
| `GET /api/sessions` · `GET /api/assets?kind=video_mjpeg` | what the database knows |
| `POST /api/shutdown` | flush recordings and exit |

Static: `/ui/display.html`, `/status`, `/apps/*.html`.

## Rules
- The hub always uses the newest data: a camera not heard from for `stale_ms` (150 ms) is ignored, so a stalled camera cannot move the cursor.
- Click/swipe/home actions are never merged or dropped; `move` actions are suppressed inside the dead zone.
- Add fields freely; unknown fields are ignored.
- Secrets never travel over this protocol except the optional `token`.
