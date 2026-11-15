# edge/motion_bridge.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   Connects Johnny's hand tracker (kept untouched in edge/pi_hand_tracking_v2/) to our agent.
#   It loads two things from his folder WITHOUT modifying them:
#     - tracker.py : the MotionEngine (hand IDs, smoothing, prediction, pinch / swipe / zoom events)
#     - main.py    : the LatestFrameCamera (a capture thread that always hands over the newest frame)
#   and adds one small class, MotionBridge, that runs the MotionEngine on the hand landmarks our
#   agent already produces, so the hub receives BOTH our raw landmarks and Johnny's motion events.
# ---------------------------------------------------------------------------
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
V2_DIR = os.path.join(HERE, "pi_hand_tracking_v2")


# Load one of Johnny's files as a module, by path, under the given module name.
# The module is registered in sys.modules BEFORE it runs (required by dataclasses inside it).
def _load(name, filename):
    path = os.path.join(V2_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Johnny's tracker.py (pure Python, no dependencies). Loaded once, under the name "tracker", because his
# main.py does "from tracker import ..." and finds it in sys.modules.
def tracker_module():
    if "tracker" not in sys.modules or not hasattr(sys.modules["tracker"], "MotionEngine"):
        _load("tracker", "tracker.py")
    return sys.modules["tracker"]


# Johnny's LatestFrameCamera class (needs picamera2 or OpenCV only when .start() is called).
def camera_class():
    tracker_module()
    if "pi_v2_main" not in sys.modules:
        _load("pi_v2_main", "main.py")
    return sys.modules["pi_v2_main"].LatestFrameCamera


# Runs Johnny's MotionEngine on our hand dictionaries, one engine per camera (each keeps its own hand IDs).
class MotionBridge:
    def __init__(self, max_hands=2):
        self.t = tracker_module()
        self.max_hands = max_hands
        self.engines = {}

    # Process one frame from one camera. `hands` is our list of {"right", "score", "pts": [[x,y,z] * 21]},
    # `t_s` is the frame time in seconds (any steady clock; only differences matter).
    # Returns a small JSON-friendly dict: tracked hands, how many were detected, and the events.
    def process(self, cam, hands, t_s):
        engine = self.engines.get(cam)
        if engine is None:
            engine = self.engines[cam] = self.t.MotionEngine(self.max_hands)
        detections = []
        for h in hands[:self.max_hands]:
            pts = h.get("pts", [])
            if len(pts) != 21:
                continue  # malformed hand: ignore it rather than crash the stream
            # HandDetection.from_landmarks wants objects with .x and .y
            points = [types.SimpleNamespace(x=p[0], y=p[1]) for p in pts]
            detections.append(self.t.HandDetection.from_landmarks(points, "Right" if h.get("right", True) else "Left"))
        out = engine.update(detections, t_s)
        return {"hands": out["hands"], "detected_hands": out["detected_hands"], "events": out["events"]}

    # Forget all hand IDs (used when a new session starts).
    def reset(self):
        self.engines.clear()
