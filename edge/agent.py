#!/usr/bin/env python3
# edge/agent.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   The program that runs on the Raspberry Pi (the "edge"). It produces hand landmarks and streams
#   them to the hub over a WebSocket. Three sources of hand data are available:
#     synthetic  fake hands doing a scripted routine (no hardware needed; used for testing)
#     replay     re-send a recording the hub made earlier
#     camera / picamera  real cameras + MediaPipe hand tracking (written but not yet tested on hardware)
#   It can also turn the hub's actions into real OS mouse events (--os-pointer) so any window
#   on the Pi, such as a real web browser, can be controlled by gestures.
#   When the hub asks for it (`python3 ips.py calibrate auto`), it also runs the automatic surface calibration
#   (edge/surface_calib.py): the projector shows a marker pattern and the camera measures where the picture is.
# ---------------------------------------------------------------------------
"""Pi-side agent: produces hand landmarks and sends them to the hub.

Sources:
  synthetic  fake hands doing a scripted routine (no hardware, no installs)   <- tested
  replay     re-send the hands from a recorded events.ndjson                   <- tested
  camera     OpenCV capture (USB webcam / Pi cam via V4L2) + MediaPipe          <- NOT tested here
  picamera   Pi CSI cameras via picamera2 + MediaPipe                           <- NOT tested here

Examples:
  python edge/agent.py --source synthetic --cams 2 --seconds 30
  python edge/agent.py --source picamera --os-pointer      (one camera, index 0)
"""
import argparse
import json
import math
import os
import random
import sys
import threading
import time

# Make the sibling file wsclient.py importable no matter where the script is run from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wsclient import WS, WSClosed  # noqa: E402


# ---------------------------------------------------------------- synthetic hands
# Build a fake hand: 21 landmarks laid out like MediaPipe's, so the hub's real logic
# can be tested without any camera.
def make_hand(ax, ay, pose, pinch, rng, noise):
    """21 MediaPipe-style landmarks. pose 'point': index tip at (ax,ay). 'open': palm centre at (ax,ay)."""
    # Place the wrist so the index fingertip (point) or the palm center (open) lands at the requested spot.
    if pose == "point":
        wx, wy = ax + 0.04, ay + 0.32
    else:
        wx, wy = ax, ay + 0.15
    # Landmark 0 is the wrist, 1-4 the thumb, then four fingers of four landmarks each.
    p = [None] * 21
    p[0] = (wx, wy)
    p[1], p[2], p[3], p[4] = (wx - .04, wy - .04), (wx - .06, wy - .08), (wx - .08, wy - .12), (wx - .09, wy - .16)
    # Horizontal offsets of the index, middle, ring and pinky knuckles from the wrist.
    dx = [-0.04, 0.0, 0.035, 0.065]
    # Each finger is either stretched out (tip far from the wrist) or curled in (tip near the knuckle).
    for f in range(4):
        base = 5 + 4 * f
        mx, my = wx + dx[f], wy - 0.15
        ext = (f == 0) or pose == "open"
        p[base] = (mx, my)
        if ext:
            p[base + 1], p[base + 2], p[base + 3] = (mx, my - .07), (mx, my - .12), (mx, my - .17)
        else:
            p[base + 1], p[base + 2], p[base + 3] = (mx, my - .04), (mx, my), (mx, my + .03)
    # Pinch: slide the thumb tip onto the index tip (pinch = 1 means they touch).
    if pinch > 0:  # thumb tip slides onto the index tip
        tx, ty = p[8]
        p[4] = (p[4][0] + (tx + .004 - p[4][0]) * pinch, p[4][1] + (ty + .004 - p[4][1]) * pinch)
    # Add a little random noise so the data looks like a real tracker's output.
    return [[x + rng.gauss(0, noise), y + rng.gauss(0, noise), 0.0] for x, y in p]


# The scripted routine repeats every 12 seconds.
PERIOD = 12.0


# The scripted fake user: point and click twice, swipe right, then hold an open palm to go home.
def script(t):
    """What the fake user does at time t (seconds, repeating every 12 s)."""
    t = t % PERIOD
    # 0-6 s: point at the screen and move in a smooth loop, with two short pinches (clicks).
    if t < 6:  # point and move; pinch twice
        ax = 0.5 + 0.25 * math.sin(2 * math.pi * t / 6)
        ay = 0.5 + 0.2 * math.sin(2 * math.pi * t / 4)
        pinch = 0.0
        for start in (2.0, 4.0):
            if start <= t < start + 0.45:
                pinch = min(1.0, (t - start) / 0.1, (start + 0.45 - t) / 0.1)
        return "point", ax, ay, max(0.0, pinch)
    # 6-7 s: open palm, then a fast move to the right (a swipe).
    if t < 7:  # open palm, swipe right
        if t < 6.2:
            return "open", 0.15, 0.5, 0.0
        if t < 6.6:
            return "open", 0.15 + 0.7 * (t - 6.2) / 0.4, 0.5, 0.0
        return "open", 0.85, 0.5, 0.0
    # 7-8.5 s: hand out of view (this resets the recognizer).
    if t < 8.5:
        return None
    # 8.5-10.2 s: open palm held still, which should trigger 'home'. After that: no hand until 12 s.
    if t < 10.2:  # open palm held still -> home
        return "open", 0.5, 0.5, 0.0
    return None


# Two-hand zoom routine (only with --two-hands): from 10.2 s to 12 s two pointing hands appear close
# together, then move apart. Returns how far apart they are (0 = close, 1 = far), or None outside that window.
def zoom_phase(t):
    t = t % PERIOD
    if t < 10.2:
        return None
    if t < 10.4:
        return 0.0
    if t < 10.9:
        return (t - 10.4) / 0.5
    return 1.0


# Produce one camera's JSON message for time t.
def synthetic_frame(cam, seq, t, rng, two_hands=False):
    s = script(t)
    hands = []
    zoom = zoom_phase(t) if two_hands else None
    if zoom is not None:
        # Two hands (a left and a right one) spreading apart: should make the tracker report a zoom in.
        for x, right, score in ((0.40 - 0.18 * zoom, False, 0.95), (0.60 + 0.18 * zoom, True, 0.90)):
            hands.append({"right": right, "score": score, "pts": make_hand(x, 0.5, "point", 0.0, rng, 0.002)})
    elif s:
        pose, ax, ay, pinch = s
        # Camera 1 is made slightly noisier and offset so the two cameras do not agree exactly.
        noise = 0.002 if cam == 0 else 0.004
        pts = make_hand(ax + (0.003 if cam else 0), ay, pose, pinch, rng, noise)
        hands.append({"right": True, "score": round(0.95 if cam == 0 else 0.85, 2), "pts": pts})
    return {"t": "hands", "cam": cam, "seq": seq, "ts": int(time.time() * 1e6),
            "track_ms": round(rng.uniform(8, 12), 2), "hands": hands}


# ---------------------------------------------------------------- hub connection
# The connection to the hub: sends hello, runs a background reader thread, offers send helpers.
class Link:
    def __init__(self, args):
        self.args = args
        host, port = args.hub.split(":")
        self.ws = WS(host, int(port), "/edge")
        # First message: who we are, which cameras we have, and whether we want real OS mouse events.
        hello = {"t": "hello", "device": args.device, "source": args.source,
                 "cameras": list(range(len(args.cam_list))), "os_pointer": args.os_pointer,
                 "token": os.environ.get("IPS_TOKEN", "")}
        self.ws.send_text(json.dumps(hello))
        self.pointer = OsPointer() if args.os_pointer else None
        self.alive = True
        # Automatic surface calibration reports through whichever connection is current (the agent reconnects).
        calib = getattr(args, "calib", None)
        if calib is not None:
            calib.send = self.send
        # Read the hub's replies on a separate thread so sending is never blocked.
        threading.Thread(target=self._reader, daemon=True).start()

    # Reads the hub's messages: print its greeting, apply actions as real mouse events if requested, report errors.
    def _reader(self):
        try:
            # Keep reading messages until the connection closes.
            while True:
                m = json.loads(self.ws.recv_text())
                if m.get("t") == "hello_ack":
                    print("hub session", m.get("session"), "record_frames", m.get("record_frames"), flush=True)
                elif m.get("t") == "action" and self.pointer:
                    self.pointer.apply(m)
                elif m.get("t") == "calib_capture":
                    self.start_calibration(m)
                elif m.get("t") == "calib_cancel" and getattr(self.args, "calib", None) is not None:
                    self.args.calib.cancel()
                    print("calibration cancelled by the hub", flush=True)
                elif m.get("t") == "error":
                    print("hub error:", m.get("error"), flush=True)
        except (WSClosed, OSError):
            self.alive = False

    # The hub asked for an automatic surface calibration. Sources without camera images (synthetic, replay) and
    # machines without the ArUco module answer at once with a clear failure instead of leaving the pattern up.
    def start_calibration(self, m):
        calib = getattr(self.args, "calib", None)
        if calib is None:
            reason = "the agent's source '%s' has no camera images to calibrate from" % self.args.source
            print("calibration:", reason, flush=True)
            self.send({"t": "calib_result", "id": m.get("id", 0),
                       "results": [{"cam": c, "ok": False, "reason": reason} for c in range(len(self.args.cam_list))]})
            return
        try:
            calib.start(m)
        except Exception as exc:  # e.g. an OpenCV build without ArUco
            print("calibration could not start: %s" % exc, flush=True)
            calib.fail("calibration could not start: %s" % exc, m.get("id", 0))

    # Send one JSON message (compact form, to keep it small).
    def send(self, msg):
        self.ws.send_text(json.dumps(msg, separators=(",", ":")))

    # Send a camera JPEG as a binary message: one byte for the camera index, then the image bytes.
    def send_jpeg(self, cam, jpeg):
        self.ws.send_binary(bytes([cam]) + jpeg)


# Turns hub actions into real operating-system mouse events.
class OsPointer:
    """Turns hub actions into real OS mouse events, so any window (e.g. a real browser) can be driven.
    Two backends: pyautogui (Windows, macOS, Linux on X11) and ydotool (Linux on Wayland, which is
    the Raspberry Pi OS default; needs `sudo apt install ydotool` and the ydotoold service running)."""

    # Linux key codes for the arrow keys (used by ydotool).
    KEYCODES = {"up": 103, "left": 105, "right": 106, "down": 108}

    def __init__(self):
        # On a Wayland desktop pyautogui cannot move the mouse, so use ydotool there.
        self.wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        if self.wayland:
            import shutil
            if not shutil.which("ydotool"):
                raise SystemExit("--os-pointer on Wayland needs ydotool: sudo apt install ydotool "
                                 "(or switch the Pi to X11 in raspi-config > Advanced > Wayland)")
            return
        # Imported only when --os-pointer is used, so the simulator needs no extra packages.
        import pyautogui  # pip install pyautogui
        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = False
        self.g = pyautogui

    # Run one ydotool command without waiting for it (keeps the reader thread fast).
    def _ydo(self, *args):
        import subprocess
        subprocess.Popen(["ydotool", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Translate one hub action into a real mouse move / press / release / arrow-key press.
    def apply(self, a):
        k = a.get("kind")
        if self.wayland:
            # ydotool: absolute move, left button down (0x40) / up (0x80), key press then release.
            if k in ("move", "down", "up"):
                self._ydo("mousemove", "--absolute", "-x", str(a["x"]), "-y", str(a["y"]))
            if k == "down":
                self._ydo("click", "0x40")
            elif k == "up":
                self._ydo("click", "0x80")
            elif k == "swipe" and a.get("detail") in self.KEYCODES:
                code = self.KEYCODES[a["detail"]]
                self._ydo("key", "%d:1" % code, "%d:0" % code)
            return
        if k in ("move", "down", "up"):
            self.g.moveTo(a["x"], a["y"], _pause=False)
        if k == "down":
            self.g.mouseDown()
        elif k == "up":
            self.g.mouseUp()
        elif k == "swipe":
            self.g.press({"left": "left", "right": "right", "up": "up", "down": "down"}[a["detail"]])


# If a camera delivers no frame for this many seconds, the agent says so (instead of waiting silently).
STALL_WARN_SECONDS = 5.0


# ---------------------------------------------------------------- hand messages
# Build the JSON message for one camera frame (the "hands" message the hub receives).
def hands_message(cam, seq, ts, track_ms, hands, motion=None):
    msg = {"t": "hands", "cam": cam, "seq": seq, "ts": ts, "track_ms": round(track_ms, 2), "hands": hands}
    if motion is not None:
        msg["motion"] = motion
    return msg


# Add Johnny's motion-engine result to a message (computed from the message's own hand landmarks), or
# remove any old one if motion is switched off. `t_s` is the frame time in seconds.
def attach_motion(args, msg, t_s):
    bridge = getattr(args, "bridge", None)
    if bridge is None:
        msg.pop("motion", None)
    else:
        msg["motion"] = bridge.process(msg["cam"], msg["hands"], t_s)
    return msg


# ---------------------------------------------------------------- sources
# Fake-hand source: sends the scripted hands at the chosen frame rate for each camera.
def run_synthetic(link, args):
    # A fixed random seed makes every run identical.
    rng = random.Random(7)
    seq = [0] * len(args.cam_list)
    t0 = time.time()
    n = 0
    # Each tick: send one frame per camera, then sleep to keep a steady frame rate.
    while link.alive:
        now = time.time() - t0
        if args.seconds and now > args.seconds:
            break
        for i, cam in enumerate(args.cam_list):
            msg = synthetic_frame(i, seq[i], now, rng, getattr(args, "two_hands", False))
            link.send(attach_motion(args, msg, now))
            seq[i] += 1
        n += 1
        delay = t0 + n / args.fps - time.time()
        if delay > 0:
            time.sleep(delay)


# Replay source: re-send the hand messages from a recording made by the hub.
def run_replay(link, args):
    rows = []
    with open(args.replay, encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            if m.get("t") == "hands":
                rows.append(m)
    if not rows:
        print("no hand frames in", args.replay)
        return
    # Keep the original spacing between frames, but stamp each message with the current time.
    t0, r0 = time.time(), rows[0]["ts"]
    for m in rows:
        wait = t0 + (m["ts"] - r0) / 1e6 - time.time()
        if wait > 0:
            time.sleep(wait)
        if not link.alive:
            return
        # Recompute the motion result from the recorded landmarks using the ORIGINAL frame time, so a replay
        # is deterministic and always uses the current motion engine (any old recorded result is replaced).
        original_t = m["ts"] / 1e6
        m = dict(m, ts=int(time.time() * 1e6))
        link.send(attach_motion(args, m, original_t))
    print("replayed", len(rows), "frames", flush=True)


# Hand tracker wrapper. Uses MediaPipe's current Tasks API (HandLandmarker) when the model file exists
# (setup.sh --pi downloads it to models/hand_landmarker.task); otherwise falls back to the older
# mp.solutions.hands API, which some MediaPipe versions still include.
class HandTracker:
    def __init__(self, model_path, max_hands=1):
        import mediapipe as mp
        self.mp = mp
        if model_path and os.path.exists(model_path):
            from mediapipe.tasks import python as mpt
            from mediapipe.tasks.python import vision
            opts = vision.HandLandmarkerOptions(
                base_options=mpt.BaseOptions(model_asset_path=model_path),
                running_mode=vision.RunningMode.VIDEO, num_hands=max_hands,
                min_hand_detection_confidence=0.5, min_hand_presence_confidence=0.5, min_tracking_confidence=0.5)
            self.task = vision.HandLandmarker.create_from_options(opts)
            self.legacy = None
            self.last_ms = -1
        else:
            if not hasattr(mp, "solutions"):
                raise SystemExit("hand model not found at %s; run ./setup.sh --pi to download it" % model_path)
            self.task = None
            self.legacy = mp.solutions.hands.Hands(max_num_hands=max_hands, model_complexity=0,
                                                   min_detection_confidence=0.5, min_tracking_confidence=0.5)

    # Track hands in one RGB image; returns our JSON hand list (21 [x, y, z] points per hand).
    def process(self, rgb):
        hands = []
        if self.task:
            # VIDEO mode needs strictly increasing timestamps in milliseconds.
            ms = max(int(time.monotonic() * 1000), self.last_ms + 1)
            self.last_ms = ms
            img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
            res = self.task.detect_for_video(img, ms)
            for lm, hd in zip(res.hand_landmarks, res.handedness):
                hands.append({"right": hd[0].category_name == "Right", "score": float(hd[0].score),
                              "pts": [[p.x, p.y, p.z] for p in lm]})
            return hands
        res = self.legacy.process(rgb)
        if res.multi_hand_landmarks:
            for lm, hd in zip(res.multi_hand_landmarks, res.multi_handedness):
                hands.append({"right": hd.classification[0].label == "Right",
                              "score": float(hd.classification[0].score),
                              "pts": [[p.x, p.y, p.z] for p in lm.landmark]})
        return hands

    # Release the model (called when the agent stops).
    def close(self):
        if self.task:
            self.task.close()


# The camera index for the i-th camera in the list (used in messages).
def cam_index(args, i):
    return args.cam_list[i] if i < len(args.cam_list) else i


# Prints one progress line every 5 seconds: frames per second, share of frames with a hand, tracking time.
class Progress:
    def __init__(self):
        self.reset()

    def reset(self):
        self.frames = self.hands = 0
        self.ms = 0.0
        self.t = time.time()

    # Count one processed frame, and print + restart if 5 seconds have passed.
    def add(self, has_hand, track_ms):
        self.frames += 1
        self.hands += 1 if has_hand else 0
        self.ms += track_ms
        span = time.time() - self.t
        if span >= 5:
            print("agent: %.1f frames/s, hand seen in %d%% of frames, tracking %.1f ms/frame"
                  % (self.frames / span, 100 * self.hands // self.frames, self.ms / self.frames), flush=True)
            self.reset()


# Give one camera frame to the automatic surface calibration (does nothing unless a calibration is running).
# A calibration problem must never stop hand tracking, so errors are reported and the calibration abandoned.
def feed_calibration(args, cam, bgr, mirrored):
    calib = getattr(args, "calib", None)
    if calib is None or not calib.active:
        return
    try:
        calib.feed(cam, bgr, mirrored)
    except Exception as exc:
        print("calibration error: %s" % exc, flush=True)
        calib.fail("calibration error on the Pi: %s" % exc)


# Video / photo source: runs the REAL hand tracker on a video file (or one photo, repeated) instead of a
# live camera. Lets the whole pipeline be tested when nobody can stand in front of the camera.
def run_video(link, args):
    import cv2
    if not args.video or not os.path.exists(args.video):
        raise SystemExit("--source video needs --video <video or photo file> that exists")
    is_image = args.video.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
    cap = None if is_image else cv2.VideoCapture(args.video)
    still = cv2.imread(args.video) if is_image else None
    if is_image and still is None:
        raise SystemExit("cannot read image " + args.video)
    tracker = HandTracker(args.model, args.max_hands)
    progress = Progress()
    seq = 0
    read_since_rewind = 0  # frames read since the video last started from the beginning
    t_end = time.time() + args.seconds if args.seconds else None
    next_t = time.time()
    try:
        while link.alive and (t_end is None or time.time() < t_end):
            # Next frame: the photo again, or the next video frame (restarting the video at its end).
            if is_image:
                bgr = still
            else:
                ok, bgr = cap.read()
                if not ok:
                    # End of the video: rewind. If nothing could be read since the last rewind, the file is unusable.
                    if read_since_rewind == 0:
                        raise SystemExit("cannot read video " + args.video)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    read_since_rewind = 0
                    continue
                read_since_rewind += 1
            feed_calibration(args, 0, bgr, False)
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            ts = int(time.time() * 1e6)
            t0 = time.perf_counter()
            hands = tracker.process(rgb)
            track_ms = (time.perf_counter() - t0) * 1000
            link.send(attach_motion(args, hands_message(0, seq, ts, track_ms, hands), time.monotonic()))
            seq += 1
            progress.add(bool(hands), track_ms)
            # Keep the chosen frame rate (never sleep a negative time if tracking is slower than that).
            next_t += 1.0 / args.fps
            time.sleep(max(0.0, next_t - time.time()))
    finally:
        tracker.close()
        if cap:
            cap.release()


# Real-camera source: grab frames, run MediaPipe hand tracking, send the landmarks (and optionally JPEGs).
def run_camera(link, args):
    import cv2
    from motion_bridge import camera_class
    # Johnny's capture class: one thread per camera that always keeps only the NEWEST frame, so tracking
    # never works on a stale image. "picamera2" = Pi CSI cameras, "opencv" = USB / V4L2 cameras.
    Camera = camera_class()
    backend = "picamera2" if args.source == "picamera" else "opencv"
    cams = []
    trackers = []
    try:
        for idx in args.cam_list:
            cams.append(Camera(backend, idx, args.width, args.height, int(args.fps), args.exposure_us, args.gain).start())
        # One tracker per camera, because each keeps its own tracking state between frames.
        trackers = [HandTracker(args.model, args.max_hands) for _ in cams]
        seq = [0] * len(cams)
        last_seq = [0] * len(cams)
        t_end = time.time() + args.seconds if args.seconds else None
        progress = Progress()
        last_frame_at = [time.monotonic()] * len(cams)
        warned_at = [0.0] * len(cams)
        while link.alive and (t_end is None or time.time() < t_end):
            for i, cam in enumerate(cams):
                # Wait briefly for this camera's newest frame (None = nothing new yet).
                newest = cam.newest(last_seq[i], timeout=0.2 if len(cams) > 1 else 2.0)
                if newest is None:
                    # No frame: warn (at most every STALL_WARN_SECONDS) once the camera has been silent that long.
                    quiet = time.monotonic() - last_frame_at[i]
                    if quiet >= STALL_WARN_SECONDS and time.monotonic() - warned_at[i] >= STALL_WARN_SECONDS:
                        print("agent: camera %d has delivered no frames for %.0f s (cable? another program using it?)" % (cam_index(args, i), quiet), flush=True)
                        warned_at[i] = time.monotonic()
                    continue
                last_frame_at[i] = time.monotonic()
                bgr, frame_t, cam_seq = newest
                last_seq[i] = cam_seq
                # Surface calibration looks at the UNflipped image (markers cannot be read mirrored) and is told about
                # the flip, so its mapping matches the flipped coordinates the hand tracker sees.
                feed_calibration(args, i, bgr, args.mirror)
                if args.mirror:
                    bgr = cv2.flip(bgr, 1)
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                # Wall-clock capture time = now minus how old the frame already is.
                ts = int((time.time() - (time.monotonic() - frame_t)) * 1e6)
                t0 = time.perf_counter()
                # Run hand tracking and time it: this 'track_ms' is what the benchmark compares.
                hands = trackers[i].process(rgb)
                track_ms = (time.perf_counter() - t0) * 1000
                link.send(attach_motion(args, hands_message(i, seq[i], ts, track_ms, hands), frame_t))
                seq[i] += 1
                progress.add(bool(hands), track_ms)
                # Optionally also send the camera image so the hub can store video.
                if args.send_frames:
                    ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok:
                        link.send_jpeg(i, jpg.tobytes())
    except RuntimeError as exc:
        # Johnny's camera class reports camera failures as RuntimeError: show a clear message.
        raise SystemExit("camera error: %s" % exc)
    finally:
        # Stop the cameras and close the trackers cleanly (avoids a noisy error message at exit).
        for c in cams:
            c.stop()
        for t in trackers:
            t.close()


# ---------------------------------------------------------------- main
# Make printing never crash: error messages from the operating system can contain characters (for example a
# localized "connection refused") that a plain ASCII / cp1252 console or log file cannot encode.
def safe_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


# Read the command line, pick the source, and keep reconnecting if the hub goes away.
def main():
    safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", default="127.0.0.1:8080")
    ap.add_argument("--source", choices=["synthetic", "replay", "camera", "picamera", "video"], default="synthetic")
    ap.add_argument("--video", help="for --source video: a video file or a photo of hands (real tracker, no live camera needed)")
    ap.add_argument("--cams", nargs="+", type=int, default=[0], help="camera indices; default one camera (0). e.g. 0 1 for two (synthetic: only the count matters)")
    ap.add_argument("--model", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "hand_landmarker.task"),
                    help="MediaPipe hand model file (downloaded by setup.sh --pi)")
    ap.add_argument("--fps", type=float, default=30)
    # 16:9 by default: the Pi Camera Module 3 sensor is 16:9, so a 4:3 size (e.g. 640x480) crops off its sides.
    ap.add_argument("--width", type=int, default=848, help="camera capture width (camera sources)")
    ap.add_argument("--height", type=int, default=480, help="camera capture height (camera sources)")
    ap.add_argument("--seconds", type=float, default=0, help="0 = run until stopped")
    ap.add_argument("--replay", help="events.ndjson recorded by the hub")
    ap.add_argument("--os-pointer", action="store_true", help="inject real OS mouse events (needs pyautogui)")
    ap.add_argument("--send-frames", action="store_true", help="also send JPEG frames (the hub stores them if record.frames is true)")
    ap.add_argument("--device", default="pi5")
    ap.add_argument("--max-hands", type=int, choices=[1, 2], default=1,
                    help="hands to track per camera (1 = cursor only; 2 enables two-hand zoom but costs more CPU)")
    ap.add_argument("--no-motion", action="store_true", help="do not run Johnny's motion engine (send raw landmarks only)")
    ap.add_argument("--mirror", action="store_true", help="flip the camera image horizontally (calibration normally makes this unnecessary)")
    ap.add_argument("--exposure-us", type=int, default=None, help="Pi camera manual exposure in microseconds (e.g. 5000 for fast motion)")
    ap.add_argument("--gain", type=float, default=None, help="Pi camera analogue gain when --exposure-us is used")
    ap.add_argument("--two-hands", action="store_true", help="synthetic source only: add a two-hand zoom to the scripted routine")
    args = ap.parse_args()
    args.cam_list = args.cams
    # Johnny's motion engine: one per camera, run on every source so the hub can use or compare its events.
    args.bridge = None
    if not args.no_motion:
        from motion_bridge import MotionBridge
        args.bridge = MotionBridge(max(args.max_hands, 2 if args.two_hands else 1))
    # Automatic surface calibration, for the sources that have real images (it needs OpenCV, which they need anyway).
    args.calib = None
    if args.source in ("camera", "picamera", "video"):
        from surface_calib import AutoCalibrator
        args.calib = AutoCalibrator(lambda msg: None, range(len(args.cam_list)))
    # Map the chosen --source to the function that implements it.
    runner = {"synthetic": run_synthetic, "replay": run_replay, "camera": run_camera, "picamera": run_camera, "video": run_video}[args.source]
    # Connect and run; on connection failure wait 2 seconds and try again.
    while True:
        try:
            link = Link(args)
            print("connected to hub", args.hub, "source", args.source, "cameras", args.cam_list, flush=True)
            runner(link, args)
            link.ws.close()
            if args.seconds or args.source == "replay":
                return
        except (WSClosed, OSError) as e:
            print("hub unreachable (%s), retrying in 2 s" % e, flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
