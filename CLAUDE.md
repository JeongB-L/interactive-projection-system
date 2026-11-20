# Interactive Projection System

A team prototype: a camera watches a person's hand, and hand gestures drive a cursor and clicks on a page projected through the Raspberry Pi's HDMI output.

## Who owns what
- **Jeongbin**: backend / hub.
- **Johnny**: ML hand tracking (`edge/pi_hand_tracking_v2/`).
- **Nina**: virtual touchscreen script (`version1/`).
- **Aneesh**: Pi hardware.

Johnny and Nina are still improving gesture detection. Low gesture accuracy (e.g. few or no pinches caught on a test clip) is a known detection issue they own, not a plumbing bug to fix here.

## Environment (the Pi)
- Raspberry Pi 5 (8 GB), Pi OS Trixie, Python 3.13, Wayland desktop, user `username`. Reached over Raspberry Pi Connect. No mouse.
- One camera: Camera Module 3 (`imx708`). Projector on HDMI.
- The Pi is in someone else's apartment, so nobody stands in front of the camera. Test gestures with **recorded video**, not live hands.
- Repo: `~/interactive-projection-system` (hyphens), GitHub `JeongB-L/interactive-projection-system` (public), branch `main`.
- Python venv: `.venv` (`source .venv/bin/activate`). `ips.py` uses it automatically for the agent and tests.

## Layout
- `hub/`: C++20 hub `ipsd` (HTTP + WebSocket, SQLite catalog, local blob store), built to `build/ipsd`. Binds to 127.0.0.1 by default. Sensitive calls from other machines need `IPS_TOKEN`.
- `edge/agent.py`: camera agent (sources: synthetic, replay, camera/picamera, video). Runs MediaPipe and sends landmarks to the hub. `edge/motion_bridge.py` runs Johnny's engine alongside the hub rules.
- `edge/pi_hand_tracking_v2/`: Johnny's code, vendored unchanged. `edge/tests/test_vendored.py` guards its file fingerprints.
- `edge/tests/`, `tests/e2e.py`: tests.
- `ui/`: the projected page and apps (launcher, Snake, AI chat).
- `tools/video_probe.py`, `tools/probe_csv.py`: offline diagnosis of a gesture video.
- `docs/`: SPEC, RUNBOOK, PROTOCOL.
- `version1/`: Nina's standalone path. `test.py` reads the camera with Picamera2, runs MediaPipe's gesture recognizer (`gesture_recognizer.task`), and prints virtual_touchscreen commands on stdout. Default click gesture is `Thumb_Up`. `test_click_only.py` is an older variant that sends only touch down/up. Camera-only: neither script has a video-file input. How to run it: `version1/steps.md`.

## Commands
```bash
python3 ips.py build          # compile the hub
python3 ips.py start | stop | status | logs
python3 ips.py pi-start       # on the Pi: hub + camera agent + fullscreen display
python3 ips.py test           # all unit suites (C++ hub, Python agent/motion, Johnny's tracker)
python3 ips.py test --e2e     # adds the end-to-end suite (starts a real hub on port 8099)
```
- `start` only builds the hub if `build/ipsd` is missing. After pulling changes under `hub/`, run `python3 ips.py build` yourself.
- `status` shows the comparison between the two gesture engines (hub rules vs Johnny's).
- Do not run `test --e2e` while the hub is up for a demo. Run `python3 ips.py stop` first.

## Two ways to use the camera (one at a time)
The camera can be opened by only one process at a time.
1. **Main pipeline**: `python3 ips.py pi-start` (agent → hub → projected UI).
2. **Nina's virtual touchscreen**: `version1/test.py` piped into `/dev/virtual_touchscreen`.

Run `python3 ips.py stop` before using (2), and `pi-start` again afterwards.

## Virtual touchscreen (verified on this Pi)
- A teammate's kernel module, built in `~/virtual_touchscreen` (upstream: vi/virtual_touchscreen).
- When it's set up: `lsmod` shows `virtual_touchscreen`, `/dev/virtual_touchscreen` is char device `509,0` matching `/proc/devices`, and the kernel lists an input device "Virtual touchscreen" (was `event5`). The module does not survive a reboot.
- Setup script: `~/Desktop/virtual_touchscreen_setup.sh` does `cd /home/username/virtual_touchscreen`, `insmod virtual_touchscreen.ko`, `mknod /dev/virtual_touchscreen c 509 0`, then `nc $1 9494 > /dev/virtual_touchscreen`. The `nc` line needs a sender address and is only for network mode. For local use, run only the insmod and mknod steps.
- Verified run, from a terminal **on the Pi's desktop** (the preview window needs the display): `cd version1 && python test.py | sudo tee /dev/virtual_touchscreen`. It drew in Krita.

## Rules
- Never print or commit `.env`. It holds the AI key and is git-ignored.
- Never edit `edge/pi_hand_tracking_v2/`.
- Never run `git reset --hard`, `git clean`, `git pull --force`, or delete untracked files without backing them up first and asking Jeongbin.
- Run `python3 ips.py test` before every commit.
- Do not push unless Jeongbin says so. If there is no GitHub login, say so; do not look for tokens or ask for one to be pasted.
- Ask before every `sudo` command.
- Leave the untracked repo-root files alone: `pi_hand_tracking_v2/` (an extra unzipped copy of Johnny's folder) and `test.jpg`.
