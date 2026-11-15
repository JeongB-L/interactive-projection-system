# Interactive Projection System | Raspberry Pi Hand Tracker V2

A small, headless, CPU-oriented replacement for the earlier MacBook Gesture Lab. It **does not overwrite** `main.py`, `gesture_lab_all_gestures.py`, or `gesture_engine/controller.py` in the previous `hand-gesture-ml` project.

## What changed

- **Two hands from one camera** (`--max-hands 2`). Two cameras are not necessary to see two hands, and processing two camera streams would increase the load substantially.
- **MediaPipe Hand Landmarker** (`hand_landmarker.task`) instead of a gesture classification model. Computes 21 landmarks/hand without running the canned gesture classifier. The old `gesture_recognizer.task` is supported as a fallback.
- **Latest-frame capture thread** prevents old frames from accumulating while inference is running. Old frames are intentionally skipped; counts are measured.
- **Motion recovery:** short, explicitly flagged predictions for missing detections (up to 120 ms) and identity retention for up to 450 ms. A predicted point is **never** claimed to be a new detection or used to create a swipe.
- **Adaptive One Euro smoothing:** steady points are stabilized, faster points get less filtering.
- **Global matching** of up to two detections and persistent numeric track IDs, regardless of MediaPipe's output order.
- **Raw-position swipe recognition** for fast movements, pinch hysteresis, and two-hand zoom. Gestures are processed independently from the optional preview.
- **Headless operation** as the default; compact JSONL events and per-frame state for the projection controller.

## 1. Install on Raspberry Pi OS 64-bit

Recommended for Raspberry Pi 5. A Pi 4 may work at lower resolutions; Pi 3 performance is likely more limited. Actual frame rates depend on CPU, thermal state, model, camera, and scene.

### USB webcam

```bash
sudo apt update
sudo apt install -y python3-venv python3-opencv
cd pi_hand_tracking_v2
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python download_model.py
```

### Raspberry Pi CSI camera

Install the Pi camera backend too:

```bash
sudo apt install -y python3-picamera2
```

The `--system-site-packages` flag lets the virtual environment find Raspberry Pi OS's Picamera2 installation. On some OS images, package-version conflicts between apt OpenCV/NumPy and pip MediaPipe can arise. If so, use a clean virtualenv, install `mediapipe` and `opencv-python` via pip, and ensure Picamera2 imports from your system environment. Avoid installing both headless and GUI pip OpenCV in the same environment.

The current official MediaPipe PyPI release includes Linux ARM64 wheels, but older OS/Python combinations may need a compatible version. Verify your system uses 64-bit `aarch64` and an officially supported Python version before installing.

### Test on your Mac before moving to the Pi

```bash
cd pi_hand_tracking_v2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install mediapipe opencv-python
python download_model.py
python main.py --camera 0 --show
```

The same two-hand engine and JSONL output run on both systems. The Pi-specific code is limited to the camera backend and package installation.

## 2. Run

From the project directory, with the venv active:

**USB webcam, minimal debug view**

```bash
python main.py --camera 0 --show
```

**Headless Raspberry Pi USB camera, JSONL feed + performance summary**

```bash
python main.py --camera 0 --jsonl outputs/track.jsonl --summary outputs/summary.json
```

**Raspberry Pi CSI camera, default 640 x 480**

```bash
python main.py --backend picamera2 --camera 0 --show
```

**Faster, lower-resolution inference on a CPU-limited Pi**

```bash
python main.py --backend picamera2 --width 480 --height 360 --fps 30 --show
```

**Shorter exposure to reduce hand blur with adequate lighting**

```bash
python main.py --backend picamera2 --fps 30 --exposure-us 5000 --gain 4 --show
```

Exposure/gain controls apply to Picamera2 only. Use enough light to avoid extremely dark/noisy frames. A faster sensor/camera frame rate **does not guarantee** the CPU will infer at that speed.

**Use the already downloaded Gesture Recognizer model from the earlier project**

Copy `gesture_recognizer.task` into `models/` and run:

```bash
python main.py --model models/gesture_recognizer.task --show
```

This compatibility path avoids downloading a model, but it still runs the gesture classifier internally and may cost more CPU.

**Benchmark a 30-second real run**

```bash
python main.py --seconds 30 --summary outputs/benchmark.json --show
```

**Stream all results to another process**

```bash
python main.py --stdout | python your_projection_controller.py
```

For a JSONL file, use `--jsonl outputs/track.jsonl`. Without `--stdout` or `--jsonl`, the app prints *events only* on stdout and diagnostic metrics on stderr. Press `q` in the preview or `Ctrl+C` in the terminal to exit.

## 3. Output interface

Example frame schema (illustrative values):

```json
{
  "timestamp_ms": 1091,
  "hands": [
    {
      "id": 1,
      "handedness": "Left",
      "detected": true,
      "predicted": false,
      "tip": [0.41, 0.33],
      "raw_tip": [0.43, 0.33],
      "center": [0.4, 0.48],
      "velocity": [0.8, 0.0],
      "pinching": false,
      "last_seen_ms_ago": 0.0
    }
  ],
  "detected_hands": 1,
  "events": [{"type": "SWIPE_RIGHT", "hand_id": 1, "distance": 0.21, "duration_ms": 105}],
  "inference_ms": 24.4,
  "age_ms": 27.1
}
```

Coordinates are normalized within the processed camera image. `tip` is the adaptively smoothed index fingertip; `raw_tip` is the observed position; `center` is the palm center. `detected=false` and `predicted=true` means there is **no** fresh model detection, and controllers should avoid triggering click/swipe actions from it.

Event types: `HAND_FOUND`, `PINCH_START`, `PINCH_END`, `SWIPE_LEFT`, `SWIPE_RIGHT`, `SWIPE_UP`, `SWIPE_DOWN`, `ZOOM_IN`, `ZOOM_OUT`. `PINCH_END` includes a `reason` when cancelled by a missing hand. Gesture thresholds are intentionally simple starting points. Adjust `--swipe-distance` or `tracker.py` to calibrate for your projected area/camera placement.

Because the default video is horizontally mirrored, reported left/right labels correspond to the *mirrored input* rather than unmodified camera orientation. Use `--no-mirror` for physical left/right orientation. For application logic, prefer persistent `id` to a handedness string.

## 4. Offline tests

```bash
python main.py --self-test
```

Tests verify track identity across reordered detections, fast displacement, right/up swipes, swipe cooldown, pinch hysteresis, release on occlusion, two-hand zoom, smoothing, and prediction safety. They **do not measure** live camera recognition accuracy.

## 5. How to actually evaluate accuracy

Perform comparable 30-second trials with one hand, two hands, slow motion, and rapid swipes. Compare the earlier app with V2 using identical camera/lighting/scene conditions. Record:

- **Processed FPS** (`processing_fps`) and **p95 frame age** (`p95_frame_age_ms`). Prefer low latency over high rendering FPS.
- **Two-hand detection fraction** (`two_hand_fraction`) while two hands were intentionally visible. Since the summary does not know human ground truth, use it only within a two-hand-only trial.
- **Missed swipes, false swipes, hand ID switches, and time to reacquire a lost hand** by manually annotating trials. Automated metrics are not yet ground-truth accuracy measurements.

The fastest gesture can still be missed if the camera produces motion-blurred exposures, hands leave the frame, or the ML inference rate samples too sparsely. Improve lighting and exposure first, then reduce resolution if the CPU is saturated. Avoid running two simultaneous landmark models for two physical cameras unless a single camera genuinely cannot cover the projected interaction area.

## Files

- `main.py`: capture pipeline, MediaPipe integration, optional debug window, JSONL output, and benchmarks.
- `tracker.py`: pure-Python two-hand tracking, motion estimation, prediction, swipe/pinch/zoom recognition.
- `download_model.py`: retrieves the official Hand Landmarker task bundle from Google.
- `tests/test_tracker.py`: hardware-free unit tests.
- `requirements.txt`: small runtime package list.
