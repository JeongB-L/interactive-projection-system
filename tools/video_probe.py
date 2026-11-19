# tools/video_probe.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   Diagnoses WHY a gesture video is (or is not) recognised. It runs the real MediaPipe hand tracker over
#   a video ONCE, as fast as the machine allows, and prints:
#     - in how many frames a hand was found,
#     - the thumb-to-index "pinch ratio" the two engines use, as numbers (min / typical / max),
#     - how many pinches each engine's thresholds would count, both on the raw landmark numbers and with
#       the picture's width/height taken into account (landmark x and y are fractions of DIFFERENT sizes
#       on a non-square picture, which distorts distances),
#     - what Johnny's MotionEngine reports when it is fed the frames at the video's own frame times
#       (so the result does not depend on how fast this computer is).
#   It needs no hub and sends nothing anywhere. Optionally writes every frame's numbers to a CSV.
# USAGE (on the Pi, from the repo folder):
#   .venv/bin/python tools/video_probe.py data/pinch720.mp4 --csv data/pinch_probe.csv
# ---------------------------------------------------------------------------
import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "edge"))  # so agent.py and motion_bridge.py can be imported

# Pinch thresholds of the two engines: (close below this ratio, open again above that one).
HUB_CLOSE, HUB_OPEN = 0.25, 0.40       # hub/src/gesture.hpp (thumb-index / wrist-to-palm-center)
JOHNNY_CLOSE, JOHNNY_OPEN = 0.37, 0.52  # edge/pi_hand_tracking_v2/tracker.py (thumb-index / palm size)


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# Pinch ratios of one hand from its 21 [x, y, z] points. `sx`, `sy` scale x and y (1, 1 = raw fractions;
# width, height = real picture pixels, which makes distances the same in every direction).
def ratios(pts, sx, sy):
    p = [(q[0] * sx, q[1] * sy) for q in pts]
    gap = dist(p[4], p[8])
    hub = gap / max(dist(p[0], p[9]), 1e-9)
    johnny = gap / max(0.025 * max(sx, sy), dist(p[0], p[9]), dist(p[5], p[17]))
    return hub, johnny


# Count pinches in a series of ratios the way the engines do: start when below `close`, end when above `open`.
def count_pinches(series, close, opn):
    n, on = 0, False
    for r in series:
        if r is None:
            continue  # no hand in this frame: keep the state as it is
        if not on and r < close:
            n, on = n + 1, True
        elif on and r > opn:
            on = False
    return n


# Value at fraction q (0..1) of the sorted list.
def pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


def summarize(name, series):
    v = [r for r in series if r is not None]
    print("   %-26s min %.2f  p10 %.2f  median %.2f  p90 %.2f  max %.2f"
          % (name, min(v, default=float("nan")), pct(v, .1), pct(v, .5), pct(v, .9), max(v, default=float("nan"))))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("video")
    ap.add_argument("--model", default=os.path.join(ROOT, "models", "hand_landmarker.task"))
    ap.add_argument("--max-hands", type=int, default=2)
    ap.add_argument("--csv", help="write one row per frame to this file")
    args = ap.parse_args()

    import cv2
    from agent import HandTracker
    from motion_bridge import MotionBridge

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        sys.exit("cannot open " + args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    tracker = HandTracker(args.model, args.max_hands)
    raw_bridge, fixed_bridge = MotionBridge(args.max_hands), MotionBridge(args.max_hands)
    print("video %dx%d at %.1f fps; running the real hand tracker once over it..." % (w, h, fps))

    hub_raw, hub_fix, jn_raw, jn_fix, rows = [], [], [], [], []
    events_raw, events_fix = {}, {}
    frames = with_hand = two_hands = 0
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        hands = tracker.process(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        t = frames / fps  # the video's own clock, so results do not depend on this computer's speed
        frames += 1
        with_hand += 1 if hands else 0
        two_hands += 1 if len(hands) > 1 else 0
        if hands:
            pts = hands[0]["pts"]
            a, b = ratios(pts, 1, 1)
            c, d = ratios(pts, w, h)
            hub_raw.append(a); jn_raw.append(b); hub_fix.append(c); jn_fix.append(d)
        else:
            a = b = c = d = None
            hub_raw.append(None); jn_raw.append(None); hub_fix.append(None); jn_fix.append(None)
        # Johnny's engine: once on the raw landmarks (what the agent sends today), once with x stretched by
        # width/height so that x and y have the same scale (distances are then correct in every direction).
        for bridge, tally, stretch in ((raw_bridge, events_raw, 1.0), (fixed_bridge, events_fix, w / h)):
            conv = [{"right": hd["right"], "score": hd["score"],
                     "pts": [[p[0] * stretch, p[1], p[2]] for p in hd["pts"]]} for hd in hands]
            for ev in bridge.process(0, conv, t)["events"]:
                tally[ev["type"]] = tally.get(ev["type"], 0) + 1
        rows.append((frames - 1, round(t, 3), len(hands), a, b, c, d))
    cap.release()
    tracker.close()

    print("\nframes: %d (%.1f s)   hand found in %d%% of frames   two hands in %d%%"
          % (frames, frames / fps, 100 * with_hand // max(frames, 1), 100 * two_hands // max(frames, 1)))
    print("\npinch ratio (smaller = thumb and index closer; the engines call it a pinch below the 'close' value):")
    summarize("hub ratio, raw", hub_raw)
    summarize("hub ratio, pixel-corrected", hub_fix)
    summarize("Johnny gap, raw", jn_raw)
    summarize("Johnny gap, pixel-corrected", jn_fix)
    print("\npinches counted by each engine's thresholds (hold the ratio below 'close', release above 'open'):")
    print("   hub rules  (%.2f/%.2f)  raw: %d   pixel-corrected: %d"
          % (HUB_CLOSE, HUB_OPEN, count_pinches(hub_raw, HUB_CLOSE, HUB_OPEN), count_pinches(hub_fix, HUB_CLOSE, HUB_OPEN)))
    print("   Johnny     (%.2f/%.2f)  raw: %d   pixel-corrected: %d"
          % (JOHNNY_CLOSE, JOHNNY_OPEN, count_pinches(jn_raw, JOHNNY_CLOSE, JOHNNY_OPEN), count_pinches(jn_fix, JOHNNY_CLOSE, JOHNNY_OPEN)))
    print("\nJohnny's MotionEngine events at the video's own frame times:")
    print("   raw landmarks      :", events_raw or "none")
    print("   pixel-corrected x  :", events_fix or "none")
    if args.csv:
        with open(args.csv, "w") as f:
            f.write("frame,t_s,hands,hub_raw,johnny_raw,hub_fixed,johnny_fixed\n")
            for r in rows:
                f.write(",".join("" if v is None else str(round(v, 4)) if isinstance(v, float) else str(v) for v in r) + "\n")
        print("\nper-frame numbers written to", args.csv)


if __name__ == "__main__":
    main()
