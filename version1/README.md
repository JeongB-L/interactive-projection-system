# Raspberry Pi projector gesture test

The Raspberry Pi Camera Module 3 captures one hand. MediaPipe tracks the center of the palm and recognizes the gesture. The script converts palm movement into position commands for the virtual touchscreen. A thumbs-up presses and holds the touch. Lower your thumb to release it.

## Set up the Pi

Put the latest Raspberry Pi script in `~/interactive-projection-system/version1/test.py` and place `gesture_recognizer.task` next to it.

Check that Raspberry Pi OS detects Camera Module 3 as `imx708` and can capture frames.

```bash
rpicam-hello --list-cameras
rpicam-hello --camera 0 --nopreview -t 3000
```

If the camera is listed under a different index, use that index in both the capture check and `python test.py --camera INDEX`. Close the capture check before running the Python script.

If the project already has a working `.venv`, skip the install commands. For a fresh setup, run:

```bash
sudo apt update
sudo apt install python3-picamera2 python3-opencv python3-venv
cd ~/interactive_projection_system
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install mediapipe
python -c 'import cv2, mediapipe; from picamera2 import Picamera2'
```

## Run the gesture test

On the Raspberry Pi desktop, open a terminal and run the virtual touchscreen setup script from the Desktop with `sudo` before sending events to the device. Check that setup created `/dev/virtual_touchscreen`.

```bash
cat /dev/virtual_touchscreen
cd ~/home/username/interactive-projection-system
source .venv/bin/activate
cd version1
```

**Print commands in the terminal**

```bash
python test.py
```

**Print commands and send them to the virtual touchscreen**

```bash
python test.py | tee /dev/virtual_touchscreen
```

The camera preview opens by default. Press **Q** or **Esc** in the preview, or **Ctrl+C** in the terminal, to stop. Use `--headless` for SSH and `--debug` to see the detected gesture, confidence, and touch state on standard error.

## Commands

Every command block begins with `s 0` and ends with `S 0`. This example shows a position update, the first touch-down, and a touch-up. Each press increments the tracking ID, starting at `T 1`.

```text
s 0
X 512
Y 512
x 512
y 512
S 0
s 0
T 1
0 10
: 100
e 0
d 0
S 0
s 0
T -1
0 10
: 100
e 0
u 0
S 0
```

## Configuration

| Option | Default | Purpose |
| --- | --- | --- |
| `--camera` | `0` | libcamera camera index |
| `--model` | `gesture_recognizer.task` beside the script | MediaPipe gesture model |
| `--capture-width`, `--capture-height` | `640 × 480` | Camera frame dimensions |
| `--screen-width`, `--screen-height` | `1920 × 1080` | Projector resolution |
| `--x-max`, `--y-max` | `1024` each | Inclusive virtual touchscreen axis maxima |
| `--click-gesture` | `Thumb_Up` | Gesture that holds the touch down |
| `--min-score` | `0.5` | Minimum gesture confidence |

The script maps the full camera frame to the projected screen. If touches land in the wrong place, align the camera view with the projection, set the projector resolution, and check the device's axis maxima with `evtest`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Camera is missing or frames fail to capture | Check for `imx708` with `rpicam-hello --list-cameras`, verify the camera index, and close other programs using the camera. |
| `picamera2` or `libcamera` fails to import | Install `python3-picamera2` through APT and activate a virtual environment created with `--system-site-packages`. |
| The hand moves but no touch registers | Run with `--debug` and check that the gesture is `Thumb_Up`, the score meets the threshold, and `touch_down=True` appears. |
| Commands print but the desktop does not respond | Check the setup script, device permissions, and incoming touch events with `evtest`. If events arrive, check which display the desktop maps to the virtual touchscreen. |