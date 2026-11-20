# Interactive Projection System

A projector shows a UI on a flat surface; a camera watches the user's hand over the image; hand gestures change what is shown.
Raspberry Pi 5 (8 GB) + 1 camera module + small projector. The C++ backend ("hub") can run on a laptop or on the Pi itself.
Class project by Jeongbin, Johnny, Nina, and Aneesh.

**Status: working prototype, tested in simulation.** Everything runs on one laptop with a fake Pi. The real Pi, cameras, MediaPipe path and projector have not been tested yet (see [docs/SPEC.md](docs/SPEC.md), section 8).

## Setup (one command, free, no accounts)

Needs Python 3.8+ and an internet connection for the first install. The script installs the C++ toolchain, builds the hub, and runs the tests.

| System | Command |
|---|---|
| Windows 10/11 | `powershell -ExecutionPolicy Bypass -File setup.ps1` (installs MSYS2 via winget if missing) |
| Linux, Raspberry Pi OS, macOS | `./setup.sh`  (on the Pi: `./setup.sh --pi` adds camera and MediaPipe packages) |

Fork or clone first: `git clone https://github.com/JeongB-L/interactive-projection-system && cd interactive-projection-system`. The AI app is optional: put an API key in `.env` (created by the script); everything else works without it. Details and manual steps: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Run everything on the Raspberry Pi

The hub, the camera agent and the projected page can all run on the Pi 5 (no laptop needed):
```bash
./setup.sh --pi                   # once: toolchain, build, tests, camera + MediaPipe packages, hand model
python3 ips.py pi-start           # hub + camera agent + fullscreen display on the projector
python3 ips.py stop               # stops all of it
python3 ips.py install-autostart  # optional: start automatically when the Pi's desktop logs in
```
Verified on the team's Pi 5 with the synthetic routine and with real MediaPipe on recorded video (about 11-14 fps, no dropped frames). Gesture accuracy is still being improved. See docs/RUNBOOK.md ("Real Pi") for what to check first.

## Try it (2 minutes, no hardware)

```bash
python ips.py start      # background hub (builds first if needed)
python ips.py open       # projected display page
python ips.py sim        # fake Pi with 1 camera: red dot follows the fingertip, pinch clicks, swipe steers
python ips.py status
python ips.py stop
```
Setup and all commands: **[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

## How it works

```
Camera -> Pi agent (MediaPipe, Python) --JSON/WebSocket--> HUB (C++, on the Pi or a laptop)
                                                                  |  (fuse cameras) -> gestures -> projector pixels
Projector <- HDMI <- Pi browser showing display page <-- actions --+
                                                                  +--> blob files (video, logs) + SQLite index
```
- **Landmarks, not video**, travel from the Pi to the hub (about 1 KB per frame), so the loop stays fast.
- **One camera by default.** More cameras are supported and fused by confidence (only across cameras that agree on the hand pose), e.g. `--cams 0 1`.
- **Gestures:** point = cursor, pinch = click/drag, open-palm swipe = left/right/up/down, open palm held still = home.
- **Apps** are web pages (launcher, Snake, AI chat, browser). The AI key lives in `.env` on the hub only.
- **Storage:** video and recordings are files in a blob store (a local folder; the interface allows other stores later); SQLite stores only each file's key and URI, so ML jobs can loop over the table.

## Layout

| Path | What |
|---|---|
| `hub/src/` | C++20 hub (every file starts with a "what this file does" header and is commented chunk by chunk): gesture engine, calibration math, fusion, storage, HTTP/WebSocket server |
| `hub/tests/` | unit tests (`python ips.py test`) |
| `edge/agent.py` | Pi-side agent: synthetic hands, replay, or real cameras + MediaPipe |
| `ui/` | display page and apps |
| `ips.py` | command-line control (start, stop, status, sim, bench, ...) |
| `tools/iter_assets.py` | example of reading every stored file through the database |
| `tools/video_probe.py`, `tools/probe_csv.py` | offline pinch/engine diagnosis of a recorded gesture video |
| `version1/` | Nina's standalone path: camera → MediaPipe gesture recognizer → Linux virtual touchscreen ([steps](version1/steps.md)) |
| `config.json`, `.env.example` | settings; secrets go in `.env` (git-ignored) |
| `docs/` | [SPEC](docs/SPEC.md) (requirements, tests, status) · [RUNBOOK](docs/RUNBOOK.md) · [DESIGN](docs/DESIGN.md) · [INTERFACES](docs/INTERFACES.md) · [PROTOCOL](docs/PROTOCOL.md) |

## Not done yet

Real-Pi testing and calibration accuracy, true stereo 3D touch, hub-side hand tracking for non-Pi-5 devices, scroll gesture, Python binding of the gesture engine. Cloud storage is intentionally not built: everything is stored locally, so the project costs nothing to run.

## Hand tracking: two engines, one switch

Two gesture engines run side by side on every frame, and `gesture_source` in `config.json` chooses which one acts:

| | `"hub"` (default) | `"edge"` |
|---|---|---|
| Who | our C++ rules in the hub | Johnny's motion engine, run on the Pi |
| Hands | cursor + clicks from one hand | stable hand IDs, up to two hands, brief prediction |
| Gestures | point, pinch, open-palm swipe, home | point, pinch, swipe, **two-hand zoom** (home still from the hub) |

The other engine is never wasted: the status page and `python ips.py status` compare what each one detected on the same
live data, so the choice can be made from evidence. Johnny's original app is kept exactly as he wrote it in
[edge/pi_hand_tracking_v2/](edge/pi_hand_tracking_v2/) (its own instructions are in its README); the agent uses his
motion engine and his latest-frame camera class from there without modifying them.

## Virtual touchscreen path (version1)

[version1/](version1/) is a second, standalone way to drive the Pi's desktop. `test.py` reads the camera with Picamera2,
runs MediaPipe's gesture recognizer, and prints [virtual_touchscreen](https://github.com/vi/virtual_touchscreen)
commands. The palm position moves the touch point and a thumbs-up presses. Piped into `/dev/virtual_touchscreen`, it acts like a
touchscreen on the desktop. It does not use the hub. Only one program can use the camera at a time, so run
`python3 ips.py stop` before it and `python3 ips.py pi-start` after. Step-by-step setup: [version1/steps.md](version1/steps.md).

## Backups on the team Pi

On 2026-11-20, before Nina's `version1/` was pulled, the Pi's untracked files were copied to
`~/backup_untracked/` on the Pi. Keep this folder. Do not delete it.

| Backup | What it holds |
|---|---|
| `version1/`, `version1_pre_pull/` | the Pi's earlier local `version1/` (older README describing a `Pointing_Up` design, `test.py` with `--min-score` 0.5, `test_click_only.py`) |
| `pi_hand_tracking_v2/` | an extra unzipped copy of Johnny's folder from the repo root (the repo's copy is `edge/pi_hand_tracking_v2/`) |
| `test.jpg` | a test image from the repo root |

## Security in one paragraph

The hub listens on **this computer only** by default. If a separate Pi has to reach a hub on a laptop, set `"bind": "0.0.0.0"` in `config.json`
and an `IPS_TOKEN` in `.env`; see [docs/RUNBOOK.md](docs/RUNBOOK.md) ("Who can reach the hub"). Keep your AI key only in `.env` (git-ignored).

## Tests

```bash
python ips.py test           # C++ hub tests + Python agent/motion tests + Johnny's own tests
python ips.py test --e2e     # also starts a real hub and agent and checks both gesture modes and replay (about a minute)
```

