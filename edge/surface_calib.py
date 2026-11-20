# edge/surface_calib.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   Surface calibration: works out exactly where the projected picture sits in the camera image, so a fingertip
#   seen by the camera can be turned into a position ON the projected screen (wall, table, any flat surface).
#
#   How: the projector shows a grid of ArUco markers (black-and-white squares with a unique ID each). The camera
#   finds them, so for every marker corner we know BOTH where it is in the camera image and where it is on the
#   screen. From ~60 such point pairs, averaged over several frames, we fit one homography (a 3x3 perspective
#   mapping) with RANSAC, which ignores the few corners that were detected badly. The hub then applies that
#   homography to every fingertip.
#
#   Compared with pointing at 4 dots and pinching, this needs no hand, uses ~15x more points, rejects outliers,
#   and reports its own accuracy (the RMS error in projector pixels).
#
#   Room light: in a lit room the projector's "black" is just the lit wall, so the markers can be too faint to read.
#   The pattern is therefore shown twice: normal, then with every marker inverted (black <-> white, background
#   unchanged). Subtracting the two averaged camera images cancels everything that did not change (room light, wall
#   texture, anything else in view) and leaves only the markers, at double contrast. In a simulation built from a
#   real photo of the team's wall this read 14-15/15 markers where the plain image read none.
#
#   Pieces:
#     marker_layout()   where each marker goes on screen (fractions of the screen) + its black/white bits
#     difference_image() pattern minus inverted pattern: the room-light-proof picture the markers are read from
#     MarkerDetector    finds the markers in one camera image (OpenCV 4.7+ and 5.x, plus the older 4.x API)
#     fit_homography()  robust camera -> screen fit and its error
#     AutoCalibrator    the agent-side state machine: show pattern, collect frames, fit, report to the hub
# ---------------------------------------------------------------------------
import threading
import time

# The marker family: 4x4 bits (+ a 1-module black border = 6x6 modules). Small codes stay readable from far away.
DICTIONARY = "DICT_4X4_50"
GRID = 6  # modules per marker side, border included
# Default grid of markers on the screen. 5 x 3 = 15 markers = 60 corners, spread over the whole picture.
DEFAULT_COLS, DEFAULT_ROWS = 5, 3

# Accept a calibration only if its RMS error is below this share of the screen diagonal (about 26 px at 1080p).
MAX_RMS_FRACTION = 0.012
# Below these shares of the diagonal the fit is reported as "good" / "ok" (about 9 px and 18 px at 1080p).
GOOD_RMS_FRACTION, OK_RMS_FRACTION = 0.004, 0.008
MIN_MARKERS = 4  # at least 4 markers (16 corners), spread out, are needed for a trustworthy fit


# ---------------------------------------------------------------- pattern layout
# Positions of the markers on a screen_w x screen_h display, as FRACTIONS of the screen (so the result still fits if the
# display changes size). Markers are square in pixels and leave a white gap of at least ~0.6 marker widths between them
# (ArUco needs a light "quiet zone" around each marker).
def marker_layout(screen_w, screen_h, cols=DEFAULT_COLS, rows=DEFAULT_ROWS, margin=0.03, fill=0.6):
    screen_w, screen_h = float(screen_w), float(screen_h)
    cell_w = screen_w * (1 - 2 * margin) / cols
    cell_h = screen_h * (1 - 2 * margin) / rows
    side = fill * min(cell_w, cell_h)
    out = []
    for r in range(rows):
        for c in range(cols):
            cx = screen_w * margin + (c + 0.5) * cell_w
            cy = screen_h * margin + (r + 0.5) * cell_h
            out.append({"id": r * cols + c,
                        "x0": (cx - side / 2) / screen_w, "y0": (cy - side / 2) / screen_h,
                        "x1": (cx + side / 2) / screen_w, "y1": (cy + side / 2) / screen_h})
    return out


# The four screen corners (fractions) of one layout entry, in ArUco's corner order: top-left, top-right,
# bottom-right, bottom-left (clockwise, as the marker is drawn upright on the screen).
def screen_corners(m):
    return [(m["x0"], m["y0"]), (m["x1"], m["y0"]), (m["x1"], m["y1"]), (m["x0"], m["y1"])]


def _aruco():
    import cv2
    if not hasattr(cv2, "aruco"):
        raise RuntimeError("this OpenCV build has no ArUco module; install opencv-python-headless 4.7 or newer")
    return cv2, cv2.aruco


def _dictionary():
    cv2, aruco = _aruco()
    return aruco.getPredefinedDictionary(getattr(aruco, DICTIONARY))


# The marker's inner 4x4 bits as a string of '0'/'1', row by row ('1' = white). The display page draws the marker
# from this, so the pattern on screen always matches the dictionary the detector uses (no copy of the table in JS).
def marker_bits(marker_id):
    cv2, aruco = _aruco()
    d = _dictionary()
    if hasattr(aruco, "generateImageMarker"):
        img = aruco.generateImageMarker(d, int(marker_id), GRID)
    else:  # OpenCV before 4.7
        img = aruco.drawMarker(d, int(marker_id), GRID)
    inner = img[1:GRID - 1, 1:GRID - 1]
    return "".join("1" if v > 127 else "0" for v in inner.flatten())


# The message that tells the display page to show (or hide) the pattern: layout plus each marker's bits.
# invert=True draws every marker cell in the opposite colour (the background stays white).
def pattern_message(layout, show=True, request_id=0, invert=False):
    if not show:
        return {"t": "calib_pattern", "id": request_id, "show": False}
    markers = [dict(m, bits=marker_bits(m["id"])) for m in layout]
    return {"t": "calib_pattern", "id": request_id, "show": True, "invert": bool(invert), "grid": GRID, "markers": markers}


# Draw the pattern into an image (white background, black markers), exactly as the display page does. Used by the
# tests and the offline tool to make synthetic "projected" pictures. invert=True: the inverted-marker version.
def render_pattern(layout, screen_w, screen_h, invert=False):
    import numpy as np
    img = np.full((int(screen_h), int(screen_w)), 255, np.uint8)
    for m in layout:
        bits = marker_bits(m["id"])
        x0, y0 = m["x0"] * screen_w, m["y0"] * screen_h
        mw, mh = (m["x1"] - m["x0"]) * screen_w / GRID, (m["y1"] - m["y0"]) * screen_h / GRID
        for gy in range(GRID):
            for gx in range(GRID):
                inner = 0 < gx < GRID - 1 and 0 < gy < GRID - 1
                white = (inner and bits[(gy - 1) * (GRID - 2) + (gx - 1)] == "1") != bool(invert)
                xa, xb = int(round(x0 + gx * mw)), int(round(x0 + (gx + 1) * mw))
                ya, yb = int(round(y0 + gy * mh)), int(round(y0 + (gy + 1) * mh))
                img[ya:yb, xa:xb] = 255 if white else 0
    return img


# Grey, floating-point copy of a camera frame (for averaging frames of one phase).
def to_gray(bgr):
    import cv2
    import numpy as np
    g = bgr if bgr.ndim == 2 else cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return g.astype(np.float32)


# Normal pattern minus inverted pattern, from the AVERAGED camera images of each phase. Everything that is the same in
# both (room light, wall texture, a lamp, the projector's own black level) cancels; the markers remain at double
# contrast. The camera's auto-exposure may change a little between the phases (the inverted pattern is brighter), so
# the inverted image is first scaled by the median brightness ratio of the two. Result: an 8-bit picture with the
# unchanged background at mid-grey, the markers dark/light around it, ready for the normal marker detector.
def difference_image(normal, inverse):
    import numpy as np
    usable = (normal > 15) & (inverse > 15) & (normal < 250) & (inverse < 250)  # skip clipped pixels
    gain = float(np.median(normal[usable] / inverse[usable])) if usable.sum() > 100 else 1.0
    d = normal - gain * inverse
    spread = max(1.0, float(np.percentile(np.abs(d), 99)))
    return np.clip(128 + d * (110.0 / spread), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- detection
class MarkerDetector:
    """Finds the calibration markers in one camera image. Returns {marker id: [[x, y] * 4]} with the corners in
    NORMALIZED camera coordinates (0..1, the same space as MediaPipe's hand landmarks)."""

    def __init__(self):
        cv2, aruco = _aruco()
        self.cv2 = cv2
        d = _dictionary()
        params = aruco.DetectorParameters() if hasattr(aruco, "DetectorParameters") else aruco.DetectorParameters_create()
        # Sub-pixel corners: each corner is located to a fraction of a camera pixel (this is most of the accuracy).
        params.cornerRefinementMethod = aruco.CORNER_REFINE_SUBPIX
        if hasattr(aruco, "ArucoDetector"):  # OpenCV 4.7+ and 5.x
            det = aruco.ArucoDetector(d, params)
            self._detect = det.detectMarkers
        else:  # older OpenCV 4.x
            self._detect = lambda gray: aruco.detectMarkers(gray, d, parameters=params)

    # `mirrored`: the agent flips this camera's image (--mirror) before hand tracking. Markers cannot be decoded in a
    # mirrored image, so pass the UNflipped image and mirrored=True; the corners are then flipped to match the hands.
    def detect(self, bgr, mirrored=False):
        cv2 = self.cv2
        gray = bgr if bgr.ndim == 2 else cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        corners, ids, _rejected = self._detect(gray)
        out = {}
        if ids is None:
            return out
        for c, i in zip(corners, ids.flatten()):
            pts = [[float(x) / w, float(y) / h] for x, y in c.reshape(4, 2)]
            if mirrored:
                pts = [[1.0 - x, y] for x, y in pts]
            out[int(i)] = pts
        return out


# ---------------------------------------------------------------- fitting
def fit_homography(cam_pts, scr_pts, screen_w, screen_h):
    """Robust fit of camera (normalized) -> screen (fractions). Returns a dict:
    ok, h (9 numbers, row-major, h[8] = 1), rms_px, max_px, inliers, points, quality, reason."""
    import cv2
    import numpy as np
    cam = np.asarray(cam_pts, np.float64).reshape(-1, 2)
    scr_px = np.asarray(scr_pts, np.float64).reshape(-1, 2) * [screen_w, screen_h]
    diag = float(np.hypot(screen_w, screen_h))
    if len(cam) < 4 * MIN_MARKERS:
        return {"ok": False, "points": int(len(cam)), "reason": "only %d marker corners found (need %d)" % (len(cam), 4 * MIN_MARKERS)}
    # Points must be spread out: if they cover a sliver of the camera image the fit extrapolates badly.
    span = cam.max(axis=0) - cam.min(axis=0)
    if span.min() < 0.05:
        return {"ok": False, "points": int(len(cam)), "reason": "the markers found are bunched together in the camera image"}
    # RANSAC in projector pixels: corners more than ~1% of the screen off the consensus are treated as bad detections.
    H, mask = cv2.findHomography(cam, scr_px, cv2.RANSAC, ransacReprojThreshold=0.01 * max(screen_w, screen_h))
    if H is None:
        return {"ok": False, "points": int(len(cam)), "reason": "no consistent perspective mapping found"}
    inl = mask.ravel().astype(bool)
    if inl.sum() < 4 * MIN_MARKERS - 4:
        return {"ok": False, "points": int(len(cam)), "inliers": int(inl.sum()), "reason": "too many inconsistent corners"}
    # Final least-squares fit on the inliers only.
    H2, _ = cv2.findHomography(cam[inl], scr_px[inl], 0)
    if H2 is not None:
        H = H2
    proj = cv2.perspectiveTransform(cam.reshape(-1, 1, 2), H).reshape(-1, 2)
    err = np.linalg.norm(proj - scr_px, axis=1)[inl]
    rms = float(np.sqrt(np.mean(err ** 2)))
    # From "camera -> projector pixels" to "camera -> fractions of the screen" (what the hub stores).
    Hf = np.diag([1.0 / screen_w, 1.0 / screen_h, 1.0]) @ H
    if abs(Hf[2, 2]) < 1e-12 or not np.all(np.isfinite(Hf)):
        return {"ok": False, "points": int(len(cam)), "reason": "degenerate mapping"}
    Hf = Hf / Hf[2, 2]
    quality = "good" if rms <= GOOD_RMS_FRACTION * diag else "ok" if rms <= OK_RMS_FRACTION * diag else "poor"
    res = {"ok": rms <= MAX_RMS_FRACTION * diag, "h": [float(v) for v in Hf.flatten()], "rms_px": round(rms, 2),
           "max_px": round(float(err.max()), 2), "inliers": int(inl.sum()), "points": int(len(cam)), "quality": quality}
    if not res["ok"]:
        res["reason"] = ("error %.0f px is too large: the surface may not be flat, or the lens distorts strongly "
                         "near the edges" % rms)
    return res


# Apply a 9-number homography to a point (same formula as the hub's Homography::apply).
def apply_h(h, x, y):
    w = h[6] * x + h[7] * y + h[8]
    if abs(w) < 1e-12:
        w = 1e-12
    return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w


class Accumulator:
    """Collects marker detections over several frames and averages them (per-corner median), which removes most
    of the frame-to-frame jitter of the corner positions. Detections from the difference image (see
    difference_image) can be added with prefer(); they come from an average of many frames already, are not
    disturbed by room light, and are used instead of the per-frame median for the markers they contain."""

    def __init__(self):
        self.seen = {}  # marker id -> list of [[x, y] * 4]
        self.frames = 0  # frames that contributed (enough markers in view)
        self.preferred = {}  # marker id -> [[x, y] * 4] from the difference image

    # Add one frame's detections; frames with too few markers (someone walking past, pattern not up yet) are skipped.
    def add(self, detections, known_ids):
        good = {i: c for i, c in detections.items() if i in known_ids}
        if len(good) < MIN_MARKERS:
            return False
        for i, c in good.items():
            self.seen.setdefault(i, []).append(c)
        self.frames += 1
        return True

    # Detections from the difference image (one averaged picture): used in preference to the per-frame median.
    def prefer(self, detections, known_ids):
        self.preferred = {i: c for i, c in detections.items() if i in known_ids}

    # Fit using the averaged corners. `layout` gives each marker's screen position.
    def fit(self, layout, screen_w, screen_h):
        import numpy as np
        by_id = {m["id"]: m for m in layout}
        cam, scr = [], []
        # A marker must have been seen in at least a third of the frames: one-off sightings are likely misreads.
        need = max(1, self.frames // 3)
        corners = {i: np.median(np.asarray(s, np.float64), axis=0).tolist()
                   for i, s in self.seen.items() if i in by_id and len(s) >= need}
        corners.update({i: c for i, c in self.preferred.items() if i in by_id})
        for i in sorted(corners):
            cam.extend(corners[i])
            scr.extend(screen_corners(by_id[i]))
        res = fit_homography(cam, scr, screen_w, screen_h) if cam else {
            "ok": False, "points": 0, "reason": "no calibration markers seen: is the camera pointed at the projected picture?"}
        res.update(markers=len(corners), of=len(layout), frames=self.frames,
                   method="difference" if self.preferred else "plain")
        return res


# ---------------------------------------------------------------- agent-side state machine
class AutoCalibrator:
    """Runs inside the agent, which owns the camera(s). Flow:
        hub -> agent  {"t": "calib_capture", "id", "screen": [w, h]}   (start)
        agent -> hub  {"t": "calib_pattern", "show": true, "invert": false, "markers": [...]}  (shown on the display)
        ... the agent's camera loop calls feed() with every frame: phase 1 collects want_frames frames ...
        agent -> hub  {"t": "calib_pattern", "show": true, "invert": true, ...}   (phase 2: inverted markers)
        ... another want_frames frames; then each camera's two averaged images are subtracted ...
        agent -> hub  {"t": "calib_result", "id", "results": [{"cam", "ok", "h", "rms_px", ...}]}
    If phase 1 alone already saw every marker in every frame (a dark room), phase 2 is skipped.
    start() is called from the connection's reader thread and feed() from the camera loop, hence the lock."""

    def __init__(self, send, cameras, settle_s=1.0, want_frames=8, timeout_s=12.0, clock=time.monotonic):
        self.send = send  # function(msg dict) that sends one message to the hub
        self.cameras = list(cameras)  # camera indices used in messages (0, 1, ...)
        self.settle_s = settle_s  # wait this long after each pattern change (display + camera exposure catch up)
        self.want_frames = want_frames  # frames collected per phase
        self.timeout_s = timeout_s
        self.clock = clock
        self.lock = threading.Lock()
        self.req = None
        self.detector = None

    @property
    def active(self):
        return self.req is not None

    # Begin a calibration requested by the hub.
    def start(self, msg):
        screen = msg.get("screen") or [1920, 1080]
        sw, sh = max(100, int(screen[0])), max(100, int(screen[1]))
        layout = marker_layout(sw, sh, int(msg.get("cols", DEFAULT_COLS)), int(msg.get("rows", DEFAULT_ROWS)))
        rid = int(msg.get("id", 0))
        now = self.clock()
        with self.lock:
            if self.detector is None:
                self.detector = MarkerDetector()
            self.req = {"id": rid, "w": sw, "h": sh, "layout": layout, "ids": {m["id"] for m in layout},
                        "t0": now, "phase": "normal", "phase_t0": now,
                        "cams": {c: {"acc": Accumulator(), "sum": {}, "n": {"normal": 0, "inverse": 0}, "mirrored": False}
                                 for c in self.cameras}}
        self.send(pattern_message(layout, True, rid))
        print("calibration %d: pattern shown, looking for %d markers" % (rid, len(layout)), flush=True)

    # The hub (or the user) gave up: stop quietly.
    def cancel(self):
        with self.lock:
            self.req = None

    # Something went wrong on our side: stop and tell the hub why (so it takes the pattern down at once).
    def fail(self, reason, request_id=None):
        with self.lock:
            r, self.req = self.req, None
        rid = request_id if request_id is not None else (r["id"] if r else 0)
        self.send({"t": "calib_result", "id": rid,
                   "results": [{"cam": c, "ok": False, "reason": str(reason)} for c in self.cameras]})

    # Look at one camera frame. Cheap when no calibration is running.
    def feed(self, cam, bgr, mirrored=False):
        if self.req is None:
            return
        out = []  # messages to send once the lock is released
        with self.lock:
            r = self.req
            if r is None:
                return
            now = self.clock()
            phase = r["phase"]
            st = r["cams"].get(cam)
            if st is not None and now - r["phase_t0"] >= self.settle_s and st["n"][phase] < self.want_frames:
                self._collect(r, st, phase, bgr, mirrored)
            full = all(c["n"][phase] >= self.want_frames for c in r["cams"].values())
            timed_out = now - r["t0"] > self.timeout_s
            finish = timed_out
            if phase == "normal" and not finish:
                # A dark room: every marker was read in every frame already, so the inverted phase adds nothing.
                clear = all(c["acc"].frames >= self.want_frames and len(c["acc"].seen) == len(r["layout"])
                            for c in r["cams"].values())
                if full and clear:
                    finish = True
                elif full or now - r["phase_t0"] > self.timeout_s / 2:  # (a silent camera must not block phase 2)
                    r["phase"], r["phase_t0"] = "inverse", now
                    out.append(pattern_message(r["layout"], True, r["id"], invert=True))
            elif phase == "inverse" and full:
                finish = True
            if finish:
                results = self._finish(r, timed_out)
                self.req = None
                out.append({"t": "calib_result", "id": r["id"], "screen": [r["w"], r["h"]], "results": results})
        for m in out:
            self.send(m)
            if m["t"] == "calib_result":
                for res in m["results"]:
                    print("calibration %d cam %d: %s" % (m["id"], res["cam"], describe(res)), flush=True)

    # Use one frame: add it to this phase's running sum (for the difference image) and, in the normal phase, read
    # the markers directly too (enough on its own in a dark room).
    def _collect(self, r, st, phase, bgr, mirrored):
        g = to_gray(bgr)
        prev = st["sum"].get(phase)
        if prev is not None and prev.shape != g.shape:
            return  # the camera changed size mid-calibration: ignore the odd frame
        st["sum"][phase] = g if prev is None else prev + g
        st["n"][phase] += 1
        st["mirrored"] = mirrored
        if phase == "normal":
            st["acc"].add(self.detector.detect(bgr, mirrored), r["ids"])

    # Fit every camera: the per-frame detections, improved by the difference image where both phases were seen.
    def _finish(self, r, timed_out):
        results = []
        for c, st in sorted(r["cams"].items()):
            n, s = st["n"], st["sum"]
            if n["normal"] and n["inverse"] and s["normal"].shape == s["inverse"].shape:
                diff = difference_image(s["normal"] / n["normal"], s["inverse"] / n["inverse"])
                st["acc"].prefer(self.detector.detect(diff, st["mirrored"]), r["ids"])
            res = st["acc"].fit(r["layout"], r["w"], r["h"])
            if not res.get("ok") and not res.get("markers"):
                res["reason"] = ("pattern not found in the camera image%s: is the camera pointed at the projected "
                                 "picture, and is nothing (a window, a person) covering it?"
                                 % (" within %.0f s" % self.timeout_s if timed_out else ""))
            results.append(dict(res, cam=c))
        return results


# One human-readable line for a result.
def describe(res):
    if res.get("ok"):
        return "OK (%s): %d/%d markers, error %.1f px rms, %.1f px worst" % (
            res.get("quality"), res.get("markers", 0), res.get("of", 0), res.get("rms_px", 0), res.get("max_px", 0))
    return "FAILED: " + str(res.get("reason", "unknown"))
