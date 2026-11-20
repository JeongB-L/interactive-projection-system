#!/usr/bin/env python3
# tools/surface_calib.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   Surface calibration by hand / from a recording, for when the live `python3 ips.py calibrate auto` is not an
#   option (e.g. testing on a laptop with a video recorded on the Pi). Two commands:
#
#     pattern   show (or --hide) the calibration marker pattern on the projected display, through the hub
#               (--invert: the inverted-marker version, for a bright room; see --inverse below)
#     fit FILE  find the pattern in a photo or video of the projection, print the camera -> screen mapping and its
#               accuracy, and with --apply install it in the running hub (camera --cam, default 0).
#               In a bright room, also record the inverted pattern and pass it as --inverse FILE2: the two are
#               subtracted, which cancels the room light (same method as `ips.py calibrate auto`).
#
#   The pattern's layout depends only on the display size, so `fit` must use the size the display had when the
#   video was recorded (--screen WxH; default: what the running hub reports, else 1920x1080).
#
#   Example (on the Pi, nobody needs to stand in front of the camera):
#     python3 ips.py stop && python3 ips.py start          (hub only, so the camera is free)
#     python3 tools/surface_calib.py pattern
#     rpicam-vid -t 3000 -o calib.h264 --width 640 --height 480
#     python3 tools/surface_calib.py pattern --hide
#     python3 tools/surface_calib.py fit calib.h264 --apply --debug calib_debug.jpg
#   Bright room: also `pattern --invert`, record calib_inv.h264, then `fit calib.h264 --inverse calib_inv.h264 ...`
# ---------------------------------------------------------------------------
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "edge"))
import ips  # noqa: E402  (hub address, token handling)
import surface_calib as sc  # noqa: E402


# The display size: from --screen "WxH", else what the running hub reports, else 1920x1080.
def screen_size(text):
    if text:
        w, h = text.lower().split("x")
        return int(w), int(h)
    s = ips.api("/api/status", timeout=2)
    if s and "screen" in s:
        return int(s["screen"][0]), int(s["screen"][1])
    print("hub not reachable: assuming a 1920x1080 display (use --screen WxH if it was different)")
    return 1920, 1080


# Show or hide the pattern on the projected display.
def cmd_pattern(a):
    w, h = screen_size(a.screen)
    msg = sc.pattern_message(sc.marker_layout(w, h), not a.hide, invert=a.invert)
    r = ips.api("/api/calibrate/pattern", msg)
    if r is None:
        sys.exit("the hub is not running (python3 ips.py start)")
    if not r.get("ok"):
        sys.exit("the hub refused the pattern: %s" % r.get("error"))
    print("pattern hidden" if a.hide else "%spattern shown for a %dx%d display (hide it with --hide)"
          % ("inverted " if a.invert else "", w, h))


# Read up to `limit` frames from a photo or a video file.
def frames_of(path, limit):
    import cv2
    if path.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
        img = cv2.imread(path)
        if img is None:
            sys.exit("cannot read image " + path)
        return [img]
    cap = cv2.VideoCapture(path)
    out = []
    while len(out) < limit:
        ok, f = cap.read()
        if not ok:
            break
        out.append(f)
    cap.release()
    if not out:
        sys.exit("cannot read video " + path)
    return out


# Draw what was found: detected markers and where the fitted mapping puts the screen's outline and grid.
def write_debug(path, frame, detections, h_frac, mirrored):
    import cv2
    import numpy as np
    img = frame.copy()
    fh, fw = img.shape[:2]
    for i, corners in detections.items():
        pts = np.array([[(1 - x if mirrored else x) * fw, y * fh] for x, y in corners], np.int32)
        cv2.polylines(img, [pts], True, (0, 200, 0), 2)
        cv2.putText(img, str(i), tuple(int(v) for v in pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
    if h_frac:
        inv = np.linalg.inv(np.array(h_frac).reshape(3, 3))  # screen fraction -> camera (normalized)
        for k in range(11):  # a 10 x 10 grid over the screen, drawn into the camera picture
            for line in (((k / 10, 0), (k / 10, 1)), ((0, k / 10), (1, k / 10))):
                p = cv2.perspectiveTransform(np.array([line], np.float64), inv)[0]
                p = [((1 - x if mirrored else x) * fw, y * fh) for x, y in p]
                cv2.line(img, tuple(int(v) for v in p[0]), tuple(int(v) for v in p[1]),
                         (255, 128, 0) if k in (0, 10) else (200, 200, 0), 2 if k in (0, 10) else 1)
    cv2.imwrite(path, img)
    print("debug picture written to", path)


# Find the pattern in a recording and fit the mapping.
def cmd_fit(a):
    w, h = screen_size(a.screen)
    layout = sc.marker_layout(w, h)
    det, acc = sc.MarkerDetector(), sc.Accumulator()
    frames = frames_of(a.file, a.frames)
    best = {}
    for f in frames:
        d = det.detect(f, a.mirror)
        acc.add(d, {m["id"] for m in layout})
        if len(d) >= len(best):
            best, best_frame = d, f
    if a.inverse:
        # Bright room: subtract the averaged recording of the inverted pattern (cancels the room light).
        inv = frames_of(a.inverse, a.frames)
        normal = sum(sc.to_gray(f) for f in frames) / len(frames)
        inverse = sum(sc.to_gray(f) for f in inv) / len(inv)
        if normal.shape != inverse.shape:
            sys.exit("the two recordings have different sizes")
        diff = sc.difference_image(normal, inverse)
        found = det.detect(diff, a.mirror)
        acc.prefer(found, {m["id"] for m in layout})
        print("difference image: %d markers" % len(found))
        if len(found) >= len(best):
            import cv2
            best, best_frame = found, cv2.cvtColor(diff, cv2.COLOR_GRAY2BGR)
    res = acc.fit(layout, w, h)
    print("%d frames read, %d with the pattern readable directly; display %dx%d" % (len(frames), acc.frames, w, h))
    print(sc.describe(res))
    if a.debug:
        write_debug(a.debug, best_frame, best, res.get("h"), a.mirror)
    if not res.get("ok"):
        sys.exit(1)
    print("h =", [round(v, 6) for v in res["h"]])
    if a.apply:
        r = ips.api("/api/calibrate/set", {"cam": a.cam, "h": res["h"]})
        if r is None:
            sys.exit("the hub is not running, nothing installed")
        if not r.get("ok"):
            sys.exit("the hub refused the mapping: %s" % r.get("error"))
        print("installed for camera %d (saved by the hub; survives restarts)" % a.cam)


def main():
    ap = argparse.ArgumentParser(description=__doc__ if __doc__ else "surface calibration tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pattern", help="show / hide the marker pattern on the display")
    p.add_argument("--hide", action="store_true")
    p.add_argument("--invert", action="store_true", help="show the inverted-marker version (for --inverse)")
    p.add_argument("--screen", default="", help="display size WxH (default: what the hub reports)")
    p.set_defaults(fn=cmd_pattern)
    f = sub.add_parser("fit", help="calibrate from a photo or video of the projected pattern")
    f.add_argument("file")
    f.add_argument("--screen", default="", help="display size WxH when the video was recorded (default: what the hub reports)")
    f.add_argument("--cam", type=int, default=0, help="camera index to install the mapping for (with --apply)")
    f.add_argument("--mirror", action="store_true", help="the agent runs with --mirror (flipped image)")
    f.add_argument("--inverse", default="", help="photo/video of the INVERTED pattern (bright room: cancels room light)")
    f.add_argument("--frames", type=int, default=30, help="video frames to use")
    f.add_argument("--apply", action="store_true", help="install the result in the running hub")
    f.add_argument("--debug", default="", help="write a picture of what was found to this file")
    f.set_defaults(fn=cmd_fit)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
