#!/usr/bin/env python3
"""Pi-side agent: produces hand landmarks and sends them to the hub.

Sources:
  synthetic  fake hands doing a scripted routine (no hardware, no installs)   <- tested
  replay     re-send the hands from a recorded events.ndjson                   <- tested
  camera     OpenCV capture (USB webcam / Pi cam via V4L2) + MediaPipe          <- NOT tested here
  picamera   Pi CSI cameras via picamera2 + MediaPipe                           <- NOT tested here

Examples:
  python edge/agent.py --source synthetic --cams 2 --seconds 30
  python edge/agent.py --source camera --cams 0 1 --os-pointer
"""
import argparse
import json
import math
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wsclient import WS, WSClosed  # noqa: E402


# ---------------------------------------------------------------- synthetic hands
def make_hand(ax, ay, pose, pinch, rng, noise):
    """21 MediaPipe-style landmarks. pose 'point': index tip at (ax,ay). 'open': palm centre at (ax,ay)."""
    if pose == "point":
        wx, wy = ax + 0.04, ay + 0.32
    else:
        wx, wy = ax, ay + 0.15
    p = [None] * 21
    p[0] = (wx, wy)
    p[1], p[2], p[3], p[4] = (wx - .04, wy - .04), (wx - .06, wy - .08), (wx - .08, wy - .12), (wx - .09, wy - .16)
    dx = [-0.04, 0.0, 0.035, 0.065]
    for f in range(4):
        base = 5 + 4 * f
        mx, my = wx + dx[f], wy - 0.15
        ext = (f == 0) or pose == "open"
        p[base] = (mx, my)
        if ext:
            p[base + 1], p[base + 2], p[base + 3] = (mx, my - .07), (mx, my - .12), (mx, my - .17)
        else:
            p[base + 1], p[base + 2], p[base + 3] = (mx, my - .04), (mx, my), (mx, my + .03)
    if pinch > 0:  # thumb tip slides onto the index tip
        tx, ty = p[8]
        p[4] = (p[4][0] + (tx + .004 - p[4][0]) * pinch, p[4][1] + (ty + .004 - p[4][1]) * pinch)
    return [[x + rng.gauss(0, noise), y + rng.gauss(0, noise), 0.0] for x, y in p]


PERIOD = 12.0


def script(t):
    """What the fake user does at time t (seconds, repeating every 12 s)."""
    t = t % PERIOD
    if t < 6:  # point and move; pinch twice
        ax = 0.5 + 0.25 * math.sin(2 * math.pi * t / 6)
        ay = 0.5 + 0.2 * math.sin(2 * math.pi * t / 4)
        pinch = 0.0
        for start in (2.0, 4.0):
            if start <= t < start + 0.45:
                pinch = min(1.0, (t - start) / 0.1, (start + 0.45 - t) / 0.1)
        return "point", ax, ay, max(0.0, pinch)
    if t < 7:  # open palm, swipe right
        if t < 6.2:
            return "open", 0.15, 0.5, 0.0
        if t < 6.6:
            return "open", 0.15 + 0.7 * (t - 6.2) / 0.4, 0.5, 0.0
        return "open", 0.85, 0.5, 0.0
    if t < 8.5:
        return None
    if t < 10.2:  # open palm held still -> home
        return "open", 0.5, 0.5, 0.0
    return None


def synthetic_frame(cam, seq, t, rng):
    s = script(t)
    hands = []
    if s:
        pose, ax, ay, pinch = s
        noise = 0.002 if cam == 0 else 0.004
        pts = make_hand(ax + (0.003 if cam else 0), ay, pose, pinch, rng, noise)
        hands.append({"right": True, "score": round(0.95 if cam == 0 else 0.85, 2), "pts": pts})
    return {"t": "hands", "cam": cam, "seq": seq, "ts": int(time.time() * 1e6),
            "track_ms": round(rng.uniform(8, 12), 2), "hands": hands}


# ---------------------------------------------------------------- hub connection
class Link:
    def __init__(self, args):
        self.args = args
        host, port = args.hub.split(":")
        self.ws = WS(host, int(port), "/edge")
        hello = {"t": "hello", "device": args.device, "source": args.source,
                 "cameras": list(range(len(args.cam_list))), "os_pointer": args.os_pointer,
                 "token": os.environ.get("IPS_TOKEN", "")}
        self.ws.send_text(json.dumps(hello))
        self.pointer = OsPointer() if args.os_pointer else None
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                m = json.loads(self.ws.recv_text())
                if m.get("t") == "hello_ack":
                    print("hub session", m.get("session"), "record_frames", m.get("record_frames"), flush=True)
                elif m.get("t") == "action" and self.pointer:
                    self.pointer.apply(m)
                elif m.get("t") == "error":
                    print("hub error:", m.get("error"), flush=True)
        except (WSClosed, OSError):
            self.alive = False

    def send(self, msg):
        self.ws.send_text(json.dumps(msg, separators=(",", ":")))

    def send_jpeg(self, cam, jpeg):
        self.ws.send_binary(bytes([cam]) + jpeg)


class OsPointer:
    """Turns hub actions into real OS mouse events, so any window (e.g. a real browser) can be driven."""

    def __init__(self):
        import pyautogui  # pip install pyautogui
        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = False
        self.g = pyautogui

    def apply(self, a):
        k = a.get("kind")
        if k in ("move", "down", "up"):
            self.g.moveTo(a["x"], a["y"], _pause=False)
        if k == "down":
            self.g.mouseDown()
        elif k == "up":
            self.g.mouseUp()
        elif k == "swipe":
            self.g.press({"left": "left", "right": "right", "up": "up", "down": "down"}[a["detail"]])


# ---------------------------------------------------------------- sources
def run_synthetic(link, args):
    rng = random.Random(7)
    seq = [0] * len(args.cam_list)
    t0 = time.time()
    n = 0
    while link.alive:
        now = time.time() - t0
        if args.seconds and now > args.seconds:
            break
        for i, cam in enumerate(args.cam_list):
            link.send(synthetic_frame(i, seq[i], now, rng))
            seq[i] += 1
        n += 1
        delay = t0 + n / args.fps - time.time()
        if delay > 0:
            time.sleep(delay)


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
    t0, r0 = time.time(), rows[0]["ts"]
    for m in rows:
        wait = t0 + (m["ts"] - r0) / 1e6 - time.time()
        if wait > 0:
            time.sleep(wait)
        if not link.alive:
            return
        m = dict(m, ts=int(time.time() * 1e6))
        link.send(m)
    print("replayed", len(rows), "frames", flush=True)


def run_camera(link, args):
    import cv2
    import mediapipe as mp
    use_pi = args.source == "picamera"
    if use_pi:
        from picamera2 import Picamera2
        cams = []
        for c in args.cam_list:
            p = Picamera2(c)
            p.configure(p.create_video_configuration(main={"size": (640, 480), "format": "RGB888"}))
            p.start()
            cams.append(p)
    else:
        cams = [cv2.VideoCapture(c) for c in args.cam_list]
    trackers = [mp.solutions.hands.Hands(max_num_hands=1, model_complexity=0, min_detection_confidence=0.5,
                                         min_tracking_confidence=0.5) for _ in cams]
    seq = [0] * len(cams)
    t_end = time.time() + args.seconds if args.seconds else None
    while link.alive and (t_end is None or time.time() < t_end):
        for i, cam in enumerate(cams):
            if use_pi:
                rgb = cam.capture_array()
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            else:
                ok, bgr = cam.read()
                if not ok:
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            ts = int(time.time() * 1e6)
            t0 = time.perf_counter()
            res = trackers[i].process(rgb)
            track_ms = (time.perf_counter() - t0) * 1000
            hands = []
            if res.multi_hand_landmarks:
                for lm, hd in zip(res.multi_hand_landmarks, res.multi_handedness):
                    hands.append({"right": hd.classification[0].label == "Right",
                                  "score": float(hd.classification[0].score),
                                  "pts": [[p.x, p.y, p.z] for p in lm.landmark]})
            link.send({"t": "hands", "cam": i, "seq": seq[i], "ts": ts, "track_ms": round(track_ms, 2), "hands": hands})
            seq[i] += 1
            if args.send_frames:
                ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    link.send_jpeg(i, jpg.tobytes())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", default="127.0.0.1:8080")
    ap.add_argument("--source", choices=["synthetic", "replay", "camera", "picamera"], default="synthetic")
    ap.add_argument("--cams", nargs="+", type=int, default=[0, 1], help="camera indices, e.g. 0 1 (synthetic: only the count matters)")
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--seconds", type=float, default=0, help="0 = run until stopped")
    ap.add_argument("--replay", help="events.ndjson recorded by the hub")
    ap.add_argument("--os-pointer", action="store_true", help="inject real OS mouse events (needs pyautogui)")
    ap.add_argument("--send-frames", action="store_true", help="also send JPEG frames (the hub stores them if record.frames is true)")
    ap.add_argument("--device", default="pi5")
    args = ap.parse_args()
    args.cam_list = args.cams
    runner = {"synthetic": run_synthetic, "replay": run_replay, "camera": run_camera, "picamera": run_camera}[args.source]
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
