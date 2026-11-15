#!/usr/bin/env python3
# tools/camera_check.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   A stand-alone check of the camera and the hand tracker, with no hub involved. It grabs frames for a
#   few seconds, runs the same hand tracker the agent uses, and reports:
#     - how bright the image is (a very dark image means no hand can be found),
#     - how many frames contained a hand, and how confident the tracker was,
#     - how long tracking takes per frame on this machine,
#   then saves a picture (with the hand's 21 points drawn on it when one was found) so you can look at
#   what the camera actually sees.  Run it on the Raspberry Pi:
#       .venv/bin/python tools/camera_check.py --source picamera --seconds 10
# ---------------------------------------------------------------------------
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The tracker class lives in the agent; make it importable.
sys.path.insert(0, os.path.join(ROOT, "edge"))
import cv2  # noqa: E402
from agent import HandTracker  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["picamera", "camera"], default="picamera")
    ap.add_argument("--cam", type=int, default=0, help="camera index")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--model", default=os.path.join(ROOT, "models", "hand_landmarker.task"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "camera_check.jpg"))
    a = ap.parse_args()

    # Open the camera (same settings as the agent).
    if a.source == "picamera":
        from picamera2 import Picamera2
        cam = Picamera2(a.cam)
        cam.configure(cam.create_video_configuration(main={"size": (640, 480), "format": "RGB888"}))
        cam.start()
        grab = lambda: cam.capture_array()  # picamera2 "RGB888" arrays are in B,G,R order
    else:
        cap = cv2.VideoCapture(a.cam)
        grab = lambda: cap.read()[1]
    tracker = HandTracker(a.model)

    frames = with_hand = 0
    brightness = track_ms = best_score = 0.0
    best_frame = last_frame = None
    t_end = time.time() + a.seconds
    t_start = time.time()
    print("Checking the camera for %.0f s. Hold ONE hand in front of it, fingers visible, and move it slowly..." % a.seconds, flush=True)
    while time.time() < t_end:
        bgr = grab()
        if bgr is None:
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        t0 = time.perf_counter()
        hands = tracker.process(rgb)
        track_ms += (time.perf_counter() - t0) * 1000
        brightness += float(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).mean())
        frames += 1
        last_frame = bgr
        if hands:
            with_hand += 1
            if hands[0]["score"] >= best_score:
                best_score = hands[0]["score"]
                # Keep a copy of the best frame with the 21 hand points drawn on it.
                best_frame = bgr.copy()
                h, w = best_frame.shape[:2]
                for x, y, _ in hands[0]["pts"]:
                    cv2.circle(best_frame, (int(x * w), int(y * h)), 4, (0, 255, 0), -1)
    tracker.close()
    if frames == 0:
        sys.exit("no frames received from the camera")

    # Save the picture to look at (the best hand frame, or the last frame if no hand was seen).
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    cv2.imwrite(a.out, best_frame if best_frame is not None else last_frame)

    mean_b = brightness / frames
    print("\nframes: %d  (%.1f per second)" % (frames, frames / (time.time() - t_start)))
    print("image brightness (0-255): %.0f  %s" % (mean_b, "(very dark: add light)" if mean_b < 40 else "(ok)"))
    print("frames with a hand: %d%%  (best confidence %.2f)" % (100 * with_hand // frames, best_score))
    print("tracking time per frame: %.1f ms" % (track_ms / frames))
    print("saved picture:", a.out)
    if with_hand == 0:
        print("\nNo hand was found. Open the saved picture: is your hand in it, in focus, and lit? "
              "If the picture is dark, upside down, or shows the wrong scene, fix the camera view first.")


if __name__ == "__main__":
    main()
