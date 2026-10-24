# Interactive Projection System

A projector shows a UI on a flat surface; two cameras watch the user's hand over the image; hand gestures change what is shown.
Raspberry Pi 5 (8 GB) + 2 camera modules + small projector, with a C++ backend ("hub") on a laptop.
Class project by Jeongbin, Johnny, Nina, and Aneesh.

**Status: working prototype, tested in simulation.** Everything runs on one laptop with a fake Pi. The real Pi, cameras, MediaPipe path and projector have not been tested yet (see [docs/SPEC.md](docs/SPEC.md), section 8).

## Setup (one command, free, no accounts)

Needs Python 3.8+ and an internet connection for the first install. The script installs the C++ toolchain, builds the hub, and runs the tests.

| System | Command |
|---|---|
| Windows 10/11 | `powershell -ExecutionPolicy Bypass -File setup.ps1` (installs MSYS2 via winget if missing) |
| Linux, Raspberry Pi OS, macOS | `./setup.sh`  (on the Pi: `./setup.sh --pi` adds camera and MediaPipe packages) |

Fork or clone first: `git clone https://github.com/JeongB-L/interactive-projection-system && cd interactive-projection-system`. The AI app is optional: put an API key in `.env` (created by the script); everything else works without it. Details and manual steps: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Try it (2 minutes, no hardware)

```bash
python ips.py start      # background hub (builds first if needed)
python ips.py open       # projected display page
python ips.py sim        # fake 2-camera Pi: red dot follows the fingertip, pinch clicks, swipe steers
python ips.py status
python ips.py stop
```
Setup and all commands: **[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

## How it works

```
Cam0 + Cam1 -> Pi agent (MediaPipe, Python) --JSON/WebSocket--> HUB (C++)
                                                                  |  fuse both cameras -> gestures -> projector pixels
Projector <- HDMI <- Pi browser showing display page <-- actions --+
                                                                  +--> blob files (video, logs) + SQLite index
```
- **Landmarks, not video**, travel from the Pi to the hub (about 1 KB per frame), so the loop stays fast.
- **Two cameras are fused** by confidence, only across cameras that agree on the hand pose.
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
| `config.json`, `.env.example` | settings; secrets go in `.env` (git-ignored) |
| `docs/` | [SPEC](docs/SPEC.md) (requirements, tests, status) · [RUNBOOK](docs/RUNBOOK.md) · [DESIGN](docs/DESIGN.md) · [INTERFACES](docs/INTERFACES.md) · [PROTOCOL](docs/PROTOCOL.md) |

## Not done yet

Real-Pi testing and calibration accuracy, true stereo 3D touch, hub-side hand tracking for non-Pi-5 devices, scroll gesture, Python binding of the gesture engine. Cloud storage is intentionally not built: everything is stored locally, so the project costs nothing to run.
