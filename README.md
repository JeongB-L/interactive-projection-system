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
Untested on real hardware so far; see docs/RUNBOOK.md ("Real Pi") for what to check first.

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
| `config.json`, `.env.example` | settings; secrets go in `.env` (git-ignored) |
| `docs/` | [SPEC](docs/SPEC.md) (requirements, tests, status) · [RUNBOOK](docs/RUNBOOK.md) · [DESIGN](docs/DESIGN.md) · [INTERFACES](docs/INTERFACES.md) · [PROTOCOL](docs/PROTOCOL.md) |

## Not done yet

Real-Pi testing and calibration accuracy, true stereo 3D touch, hub-side hand tracking for non-Pi-5 devices, scroll gesture, Python binding of the gesture engine. Cloud storage is intentionally not built: everything is stored locally, so the project costs nothing to run.

## ADD THESE FOR PI

# Raspberry Pi Hand Tracking V2

A lightweight, real-time hand-tracking application optimized for Raspberry Pi. Supports two-hand detection, fast-motion tracking, gesture recognition, and performance benchmarking using MediaPipe and OpenCV.

## 1. Navigate to the Project

After extracting `pi_hand_tracking_v2.zip`, open a terminal and navigate to the project directory:

```bash
cd ~/Downloads/pi_hand_tracking_v2
```

Adjust the path if the folder was extracted elsewhere.

## 2. Install Dependencies

Update the Raspberry Pi and install the required system packages:

```bash
sudo apt update
sudo apt install -y python3-venv python3-opencv python3-picamera2
```

Create and activate a Python virtual environment:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
```

Install the project dependencies:

```bash
python -m pip install -r requirements.txt
```

## 3. Download the MediaPipe Model

```bash
python download_model.py
```

This downloads the hand-tracking model required for inference.

## 4. Run Hand Tracking

### Option A: Raspberry Pi Camera Module (CSI)

```bash
python main.py --backend picamera2 --show
```

### Option B: USB Webcam

```bash
python main.py --camera 0 --show
```

The application opens a camera preview displaying detected hands, tracking IDs, and motion information.

Press `Q` to exit.

## 5. Fast-Motion Tracking

For improved tracking during rapid hand movements, use a shorter camera exposure:

```bash
python main.py --backend picamera2 \
  --fps 30 \
  --exposure-us 5000 \
  --gain 4 \
  --show
```

**Recommended conditions:**

- Use bright, consistent lighting.
- Keep both hands within the camera frame.
- Avoid backgrounds with excessive visual clutter.
- Adjust exposure and gain based on lighting.
- Minimize camera motion during testing.

A shorter exposure reduces motion blur but requires sufficient lighting.

## 6. Performance Benchmarking

Run a 30-second benchmark using the Raspberry Pi Camera Module:

```bash
python main.py --backend picamera2 \
  --show \
  --seconds 30 \
  --summary benchmark.json
```

For a USB webcam:

```bash
python main.py --camera 0 \
  --show \
  --seconds 30 \
  --summary benchmark.json
```

The results are saved to `benchmark.json`.

The benchmark can help evaluate:

- Processing frame rate (FPS)
- Inference latency
- Hand-detection statistics
- Dropped frames

### Suggested Test Cases

| Test | Procedure | Expected Behavior |
|---|---|---|
| Single hand | Hold one hand still | Stable detection |
| Two hands | Hold both hands in frame | Both hands detected |
| Fast horizontal movement | Move hand rapidly left and right | Minimal tracking loss |
| Fast vertical movement | Move hand rapidly up and down | Minimal tracking loss |
| Hand crossing | Cross and separate hands | Stable tracking IDs |
| Temporary disappearance | Move a hand outside the frame and return | Tracking recovers |
| Pinch | Repeatedly pinch thumb and index finger | Correct pinch events |
| Swipe | Perform left and right swipes | Correct movement direction |
| Two-hand zoom | Move hands apart and together | Correct zoom events |

## 7. Run Offline Tests

Run the built-in tests without requiring a camera:

```bash
python main.py --self-test
```

These tests validate tracking and motion-processing logic. They do not replace live camera accuracy testing.

## 8. Run Without a Graphical Interface

For headless deployment or SSH environments, omit `--show`:

```bash
python main.py --backend picamera2
```

This avoids unnecessary graphical rendering and reduces resource usage.

## 9. Reactivate the Environment

Whenever you open a new terminal, navigate to the project folder and reactivate the virtual environment:

```bash
cd ~/Downloads/pi_hand_tracking_v2
source .venv/bin/activate
```

Then launch the application:

```bash
python main.py --backend picamera2 --show
```

## 10. Troubleshooting

### Camera Not Detected

Check whether the Raspberry Pi recognizes the camera.

For a CSI camera:

```bash
rpicam-hello --list-cameras
```

For a USB webcam:

```bash
ls /dev/video*
```

### Python Module Not Found

Make sure the virtual environment is active:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Low FPS

Try the following:

- Close unnecessary applications.
- Disable the graphical preview.
- Reduce the camera resolution if supported by the application.
- Ensure the Raspberry Pi is adequately cooled.
- Avoid running unnecessary processes alongside inference.

### Poor Fast-Motion Detection

- Increase ambient lighting.
- Reduce camera exposure time.
- Confirm that the actual capture FPS is sufficiently high.
- Check whether tracking fails because of motion blur or inference latency.

## 11. Performance Goals

These are initial evaluation targets, not guaranteed results.

| Metric | Target |
|---|---|
| Maximum simultaneous hands | 2 |
| Processing FPS | 20–30 |
| Stable two-hand detection | ≥90% in suitable conditions |
| Fast-swipe recognition | ≥90% |
| Motion-to-update latency | Under 100 ms |
| Resource usage | Suitable for Raspberry Pi deployment |

Actual performance depends on the Raspberry Pi model, camera, lighting, and inference configuration.

## Project Objective

Develop a reliable, low-latency hand-tracking system for the Interactive Projection System that prioritizes:

1. Accurate simultaneous detection of two hands.
2. Responsive tracking during fast movement.
3. Stable tracking identities and reduced jitter.
4. Low CPU and memory overhead.
5. Compatibility with Raspberry Pi hardware.
6. Minimal graphical overhead for production deployment.
