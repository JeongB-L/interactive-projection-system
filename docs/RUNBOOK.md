# Runbook

Everything is driven by one command: `python ips.py <command>`. Run it from the repo folder. Python 3.8+ is enough (standard library only).

## One-time setup

One script installs the toolchain, builds the hub, and runs the tests (see the README):
- Windows: `powershell -ExecutionPolicy Bypass -File setup.ps1`
- Linux / Raspberry Pi / macOS: `./setup.sh` (add `--pi` on the Pi for camera packages)

Optional, for the AI app only: put your key after `ANTHROPIC_API_KEY=` in `.env`. Without a key everything else still works.

On Linux use `python3` wherever this runbook says `python`.

## Turn it on

```bash
python ips.py start          # hub runs in the background
python ips.py open           # opens the projected display page in your browser
python ips.py sim            # a fake Pi with 1 camera, so you can see it work with no hardware
python ips.py status         # fps, latency, gestures
```
Watch the page: a red dot follows the fake fingertip, pinches click, and a swipe moves Snake. `python ips.py open status` shows the same numbers live.

## Turn it off

```bash
python ips.py stop           # stops the simulator and the hub; the recording is saved first
```

## Things you can do

| Command | What it does |
|---|---|
| `python ips.py launch snake` | switch app (`browser`, `ai`, `snake`, `launcher`, or any URL) |
| `python ips.py home` | back to the launcher |
| `python ips.py calibrate auto` | **map the camera onto the projected surface** (wall, table...): the projector shows a marker pattern for a few seconds and the camera measures it. No hand needed. Prints the accuracy in projector pixels. See "Surface calibration" below |
| `python ips.py calibrate` | older way: show 4 yellow markers, point at each and pinch. `calibrate reset` clears any calibration |
| `python ips.py brightness 0.4` | dim the projected image (test tracking under less light) |
| `python ips.py ai "explain pinch gestures"` | ask the AI through the hub |
| `python ips.py sessions` | recorded sessions |
| `python ips.py assets` | files stored (video, logs); `--kind video_mjpeg` to filter |
| `python tools/iter_assets.py` | example loop that reads every stored file via the database |
| `python ips.py bench --seconds 15` | latency and throughput table |
| `python ips.py test` | unit tests |
| `python ips.py logs` | last lines of the hub log |

## Everything on the Pi (no laptop)

```bash
./setup.sh --pi                       # once
python3 ips.py pi-start               # hub + camera agent (1 CSI camera) + fullscreen Chromium on the projector
python3 ips.py status                 # from the Pi, or from a laptop: IPS_URL=http://<pi-ip>:8080 python3 ips.py status
python3 ips.py stop
```
Options: `--source camera` for a USB webcam, `--cams 0 1` for two cameras, `--os-pointer` to drive real windows, `--no-kiosk` to skip the browser.
`python3 ips.py install-autostart` makes `pi-start` run at desktop login (`--remove` undoes it).

### Using it on the wall (tested 2026-11-20)
- **Point** with the index finger: the red dot follows. **Pinch** (thumb + index, then open) to click. A pinch is always a
  click at the spot you pointed at just before pinching; the cursor does not follow the hand while pinched
  (`click_rewind_ms` and `click_drag_px` in `config.json`; measured: the tracked fingertip jumps ~250 px as the fingers close).
- **Home**: pinch the ⌂ Home button at the bottom center of every app, or hold an open palm still for 1 s.
- **Calibrate** tile in the launcher = `calibrate auto` (3 s countdown so you can step aside, then the result is shown).
- **Snake**: pinch to start, then point where the snake should go (it turns toward the cursor). Swipes also steer.
- **Clicks in external websites (Browser tile)**: need the virtual touchscreen. After every reboot run `sudo ~/ts.sh`
  (loads the module, makes `/dev/virtual_touchscreen` writable for the user), then `pi-start`. When the device is writable,
  `pi-start` passes `--touch-device /dev/virtual_touchscreen` to the agent and every pinch becomes a real touch (works in
  any page); otherwise clicks only work in our own pages. `--no-touch` turns this off. The device can be opened by one
  program at a time, so Nina's `version1/test.py --device` and the agent cannot both use it.
- `config.json` has `"gesture_source": "edge"` (Johnny's engine decides pinches and swipes; it caught swipes that the hub
  rules missed). Set it back to `"hub"` to use the hub's own rules.

What to check first on the real Pi (none of this has run on hardware yet):
1. `python3 ips.py logs` and `data/logs/agent.log` after `pi-start`: the agent should print "connected to hub" and `status` should show about 20-30 fps.
2. Colors: picamera2's "RGB888" frames are in B,G,R order; the agent now converts them for MediaPipe. If detection is poor, record a frame (`record.frames: true`) and check that skin looks normal.
3. CPU and heat: tracking, the browser and the hub share 4 cores. Watch `vcgencmd measure_temp`; use a heatsink/fan.
4. `--os-pointer` on the default Wayland desktop uses `ydotool` (installed by setup); it needs the `ydotoold` service running. Alternatively switch to X11 in `raspi-config`.
5. If `import picamera2, mediapipe` fails with a numpy error: `.venv/bin/pip install "numpy<2"`.
6. Camera video fills disk fast (about 2 MB/s per camera); prefer an SSD over the SD card if you record video.

## Who can reach the hub (security)

By default the hub listens on **this computer only** (`"bind": "127.0.0.1"` in `config.json`). That is the right setting when the hub,
the camera agent and the projected page all run on the Pi, and nobody else on the network can see it.

If a *separate* Pi must connect to a hub on a laptop, set `"bind": "0.0.0.0"` **and** put a secret in `.env`:
```
IPS_TOKEN=pick-a-long-random-string
```
Then: the agent sends the token when it connects (`export IPS_TOKEN=...` on the Pi too); calls that change the hub or spend the AI key
(`/api/shutdown`, `/api/ai`, `/api/calibrate/*`, `/api/brightness`, opening an arbitrary URL, listing stored files) are refused from
other machines unless they carry the token in an `X-IPS-Token` header (`ips.py` does this automatically when `IPS_TOKEN` is set).
The projected page can still switch between configured apps, go home and report its size from another machine; the AI app in that split setup
is refused (run everything on the Pi, or open the AI page on the hub computer). Never put the hub on an untrusted network without the token.

## Choosing the gesture engine

`config.json` has `"gesture_source": "hub"` (our C++ rules, the default) or `"edge"` (Johnny's motion engine running on the Pi).
Edit it, then `python3 ips.py stop` and `start`. Both engines always run; `python3 ips.py status` shows what each one detected:
```
gestures acted on: {...} (engine in charge: hub)
engine comparison (detected, excluding cursor moves):
   our hub rules  : {'down': 2, 'up': 2, 'swipe': 1, 'home': 1}
   Johnny's engine: {'down': 2, 'up': 2, 'swipe': 2, 'zoom': 1}  (390 frames carried his data)
```
Notes: `edge` mode follows one camera (the first that sends motion data) and still takes the "home" gesture from our rules.
If the agent runs with `--no-motion`, the hub falls back to its own rules whatever the setting says.
Two-hand zoom needs `--max-hands 2` (`python3 ips.py pi-start --max-hands 2`), which costs more CPU on the Pi.
Agent options: `--max-hands {1,2}`, `--no-motion`, `--mirror`, `--exposure-us N --gain G` (shorter exposure for fast motion), `--two-hands` (fake zoom in the simulator).

## Running the tests

```bash
python3 ips.py test          # C++ hub, Python agent + motion engine, Johnny's own tests
python3 ips.py test --e2e    # plus the end-to-end test: real hub + real agent, both gesture modes, replay (about a minute)
```
On the Pi the real-model tests also run (they need `models/hand_landmarker.task` and `data/hands.jpg`; the sample photo command is below).
Without OpenCV/MediaPipe those tests are skipped, not failed. Johnny's files must stay untouched: a test checks their fingerprints.

## Testing with nobody in front of the camera

Three ways to exercise the system when no one can stand at the camera (all on the Pi, from a terminal on its desktop):
```bash
python3 ips.py pi-start --source synthetic          # fake hands drive the real hub, display and apps
python3 ips.py pi-start --source video --video data/hands.jpg   # the REAL tracker on a photo (or video) of hands
.venv/bin/python edge/agent.py --source replay --replay <events.ndjson from a past session>   # replay real recorded landmarks
```
`--video` accepts a photo (repeated) or a video file (looped). Sample photo: `curl -fL -o data/hands.jpg https://storage.googleapis.com/mediapipe-tasks/hand_landmarker/woman_hands.jpg`.
To see what the camera sees remotely: `.venv/bin/python tools/camera_check.py` saves `data/camera_check.jpg` (open it on the Pi desktop).
Whenever someone is at the camera, the hub records the hand data (`python3 ips.py assets`), and that recording can be replayed later without them.

## Real Pi (when hardware is ready)

On the Pi: `python3 -m pip install opencv-python mediapipe` (and `picamera2` for the CSI cameras), then
```bash
python3 edge/agent.py --hub <laptop-ip>:8080 --source picamera --cams 0 --os-pointer
```
Use `--source camera` for USB webcams. `--send-frames` also uploads video (the hub only stores it if `record.frames` is `true` in `config.json`). On the Pi, open `http://<laptop-ip>:8080/ui/display.html` fullscreen in Chromium (kiosk mode) on the projector output. The Pi code paths are written but have **not been run on real hardware yet**.

## Where things live

| What | Where |
|---|---|
| Settings (thresholds, apps, storage) | `config.json` |
| Secrets (AI key, token) | `.env` (never committed) |
| Database (index only) | `data/ips.db` |
| Video and recordings (the actual bytes) | `data/blobs/...` |
| Calibration | `data/calibration.json` |
| Hub log | `data/logs/hub.log` |

## Storage is local on purpose

Everything is stored on this machine (`data/`): no account, no cloud bill. The database stores a key and a URI for each file, so storage *could* move later (any bucket, NAS or mounted drive), but a cloud store is **not built** and is not needed for testing. Cloud object storage such as AWS S3 is a paid service, so it is deliberately left out. If the team ever wants it: write one class implementing `IBlobStore` (`hub/src/storage.hpp`), copy the files, run `python ips.py migrate-uri <new base>`, set `blob_uri`.

## Surface calibration

Calibration teaches the hub where the projected picture is in the camera image, so a fingertip on the wall becomes
a cursor at the matching spot of the picture. Redo it whenever the camera or the projector moves.

```bash
python3 ips.py pi-start          # hub + camera agent + display (if not already running)
python3 ips.py calibrate auto    # ~3 s: pattern on the wall, then e.g.
# camera 0: calibrated (good): 15/15 markers, error 1.6 px rms (3.2 px worst)
```
- **Needs**: the camera sees the whole projected picture (part of it is fine; more is better), nobody standing in the
  way for those few seconds. Works from a distance as long as the picture fills at least ~25% of the camera's width.
- **Quality**: `good` < ~9 px, `ok` < ~18 px, `poor` above that (at 1080p). A `poor` or failed fit usually means the
  surface is not flat, or the lens bends straight lines near the image edges.
- After calibration a fingertip beyond the picture's edge holds the cursor at that edge.
- The mapping is only exact for a fingertip **on** the surface; a hand held in the air between camera and wall
  lands slightly off (parallax). Mount the camera close to the projector for the smallest error.
- `python3 ips.py status` and the `/status` page show the last result. The mapping is saved in `data/calibration.json`.

Without a live camera (e.g. on a laptop, from a recording made on the Pi):
```bash
python3 ips.py calibrate pattern                       # show the pattern only
rpicam-vid -t 3000 -o calib.h264 --width 640 --height 480   # record it (stop the agent first: one camera user at a time)
python3 ips.py calibrate hide
python3 tools/surface_calib.py fit calib.h264 --debug calib_debug.jpg      # check; add --apply to install it
```
The debug picture shows the markers found (green) and the projected screen's grid as the mapping sees it.

## Troubleshooting

- **`hub did not come up`**: `python ips.py logs`. On Windows a wrong DLL on PATH shows as exit code 3221225785; `ips.py` already puts MSYS2 first.
- **Port in use**: change `port` in `config.json` and `set IPS_URL=http://127.0.0.1:<port>`.
- **Cursor stuck at wrong place after moving the projector or camera**: `python3 ips.py calibrate auto`.
- **`calibrate auto` says "pattern not found"**: the camera does not see the projected picture (check with `calibrate pattern` and a photo), or the room is so bright the markers wash out.
- **Browser app doesn't react to gestures**: external websites can't be driven from the display page. Run `sudo ~/ts.sh`, then `python3 ips.py stop` and `pi-start` (pinches become real touches), or run the agent with `--os-pointer`.
