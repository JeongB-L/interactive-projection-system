# edge/tests/helpers.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   Shared building blocks for the agent / motion-engine tests: makes the edge/ folder importable, builds
#   realistic hand data, and provides stand-ins for the hub connection, the camera and the hand tracker so
#   the real agent code can be exercised with no hardware.
# ---------------------------------------------------------------------------
import os
import random
import sys
import time
import types

EDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(EDGE)
sys.path.insert(0, EDGE)

import agent  # noqa: E402  (the Pi agent under test)
import motion_bridge  # noqa: E402  (loads Johnny's code from edge/pi_hand_tracking_v2)

RNG = random.Random(0)


# One hand with realistic finger geometry and NO noise: pose "point" (index tip at ax, ay) or "open" (palm center).
def hand(ax, ay, pose="point", pinch=0.0, right=True, score=0.95):
    return {"right": right, "score": score, "pts": agent.make_hand(ax, ay, pose, pinch, RNG, 0.0)}


# Stand-in for the connection to the hub: just remembers what the agent sends.
class FakeLink:
    def __init__(self):
        self.alive = True
        self.sent = []
        self.jpegs = []

    def send(self, msg):
        self.sent.append(msg)

    def send_jpeg(self, cam, jpeg):
        self.jpegs.append((cam, jpeg))


# The command-line arguments the agent's source functions read, with safe defaults; override with keywords.
def make_args(**kw):
    d = dict(source="synthetic", cam_list=[0], fps=60.0, width=848, height=480, seconds=0.4, replay=None, video=None, model="unused",
             max_hands=1, mirror=False, exposure_us=None, gain=None, send_frames=False, two_hands=False, bridge=None)
    d.update(kw)
    return types.SimpleNamespace(**d)


# Feed a list of (time, [hands]) frames through a MotionBridge and return every event as (time, type).
def run_frames(frames, bridge=None, cam=0):
    bridge = bridge or motion_bridge.MotionBridge(2)
    events = []
    for t, hands in frames:
        out = bridge.process(cam, hands, t)
        events += [(t, e["type"]) for e in out["events"]]
    return events


def types_of(events):
    return [e[1] for e in events]


# Optional libraries: tests that need them are skipped (not failed) where they are not installed.
try:
    import cv2  # noqa: F401
    import numpy as np  # noqa: F401
    HAVE_CV = True
except ImportError:
    HAVE_CV = False

try:
    import mediapipe  # noqa: F401
    HAVE_MP = True
except ImportError:
    HAVE_MP = False
