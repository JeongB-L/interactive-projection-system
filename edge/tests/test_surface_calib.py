# edge/tests/test_surface_calib.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   The surface calibration (edge/surface_calib.py) on synthetic camera pictures: the marker pattern is drawn as the
#   display page draws it, then warped into a 640x480 "camera image" the way a projector picture on a wall looks to a
#   camera at an angle (plus blur and sensor noise). Because the warp is known, every result can be checked against
#   the TRUE camera -> screen mapping, not just against itself:
#     - the layout (markers on screen, square, separated, unique bits),
#     - detection + fit accuracy, mirrored cameras, partly covered patterns, and clear failures,
#     - a BRIGHT room (projector barely brighter than the lit, textured wall; auto-exposure drifting): the plain
#       picture shows no readable marker, the pattern-minus-inverted-pattern difference image recovers them,
#     - the agent-side AutoCalibrator from "pattern shown" to "result sent", including timeouts and two cameras,
#     - the real agent loops (run_video, run_camera with --mirror) and its replies to the hub's requests.
#   Skipped where OpenCV + numpy are not installed.
# ---------------------------------------------------------------------------
import os
import tempfile
import time
import types
import unittest

import helpers
from helpers import FakeLink, agent, hand, make_args, motion_bridge

if helpers.HAVE_CV:
    import cv2
    import numpy as np
    import surface_calib as sc

W, H = 1920, 1080  # projector size
CW, CH = 640, 480  # camera size
# Where the projected picture's corners land in the camera image (a wall seen at an angle).
WALL_QUAD = [[130, 95], [540, 70], [575, 400], [95, 380]]


def screen_to_camera():
    return cv2.getPerspectiveTransform(np.float32([[0, 0], [W, 0], [W, H], [0, H]]), np.float32(WALL_QUAD))


# A camera picture of the projected pattern on a grey wall. `cover`: (x0, y0, x1, y1) camera pixels hidden by a dark object.
def camera_image(cover=None, seed=0, blank=False):
    rng = np.random.default_rng(seed)
    layout = sc.marker_layout(W, H)
    screen = np.full((H, W), 255, np.uint8) if blank else sc.render_pattern(layout, W, H)
    M = screen_to_camera()
    proj = cv2.warpPerspective(screen, M, (CW, CH), flags=cv2.INTER_AREA)
    lit = cv2.warpPerspective(np.full((H, W), 255, np.uint8), M, (CW, CH)) > 0
    img = np.full((CH, CW), 70, np.uint8)
    img[lit] = (proj[lit] * 0.8 + 30).astype(np.uint8)  # projector black is never fully black
    if cover:
        x0, y0, x1, y1 = cover
        img[y0:y1, x0:x1] = 40
    img = cv2.GaussianBlur(img, (3, 3), 0)
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


# A camera picture in a BRIGHT room: the wall is lit (level ~150, with a fixed plaster texture) and the projector adds
# only `delta` levels where its picture is white. `invert`: the inverted-marker phase. `gain`: auto-exposure change.
def bright_room_image(delta=14, invert=False, gain=1.0, seed=0):
    rng = np.random.default_rng(seed)
    texture = cv2.GaussianBlur(np.random.default_rng(42).normal(0, 12, (CH, CW)).astype(np.float32), (5, 5), 0)
    screen = sc.render_pattern(sc.marker_layout(W, H), W, H, invert=invert)
    proj = cv2.warpPerspective(screen, screen_to_camera(), (CW, CH), flags=cv2.INTER_AREA).astype(np.float32) / 255
    img = (150 + texture + proj * delta) * gain + rng.normal(0, 2.5, (CH, CW))
    return cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


# The TRUE screen position (fractions) of a camera pixel, from the known warp.
def truth(cx, cy):
    g = np.linalg.inv(screen_to_camera()) @ [cx, cy, 1.0]
    return g[0] / g[2] / W, g[1] / g[2] / H


# Worst error, in projector pixels, of a fitted homography over points spread across the projected picture.
def worst_error_px(h, mirrored=False):
    worst = 0.0
    for cx in range(150, 540, 40):
        for cy in range(110, 370, 40):
            tx, ty = truth(cx, cy)
            nx = cx / CW
            fx, fy = sc.apply_h(h, 1 - nx if mirrored else nx, cy / CH)
            worst = max(worst, float(np.hypot((fx - tx) * W, (fy - ty) * H)))
    return worst


def fit_frames(frames, mirrored=False):
    layout = sc.marker_layout(W, H)
    det, acc = sc.MarkerDetector(), sc.Accumulator()
    for f in frames:
        acc.add(det.detect(f, mirrored), {m["id"] for m in layout})
    return acc.fit(layout, W, H)


@unittest.skipUnless(helpers.HAVE_CV, "needs OpenCV + numpy")
class Layout(unittest.TestCase):
    def setUp(self):
        self.layout = sc.marker_layout(W, H)

    def test_fifteen_markers_with_unique_ids_inside_the_screen(self):
        self.assertEqual(15, len(self.layout))
        self.assertEqual(list(range(15)), [m["id"] for m in self.layout])
        for m in self.layout:
            self.assertTrue(0 < m["x0"] < m["x1"] < 1 and 0 < m["y0"] < m["y1"] < 1)

    def test_markers_are_square_in_pixels(self):
        for m in self.layout:
            self.assertAlmostEqual((m["x1"] - m["x0"]) * W, (m["y1"] - m["y0"]) * H, places=6)

    def test_markers_keep_a_white_gap_between_them(self):
        side = (self.layout[0]["x1"] - self.layout[0]["x0"]) * W
        gap = (self.layout[1]["x0"] - self.layout[0]["x1"]) * W
        self.assertGreater(gap, 0.5 * side)

    def test_markers_reach_towards_all_four_screen_edges(self):
        self.assertLess(min(m["x0"] for m in self.layout), 0.1)
        self.assertGreater(max(m["x1"] for m in self.layout), 0.9)
        self.assertLess(min(m["y0"] for m in self.layout), 0.12)
        self.assertGreater(max(m["y1"] for m in self.layout), 0.88)

    def test_bits_are_16_binary_digits_and_differ_per_marker(self):
        bits = [sc.marker_bits(i) for i in range(15)]
        self.assertTrue(all(len(b) == 16 and set(b) <= {"0", "1"} for b in bits))
        self.assertEqual(15, len(set(bits)))

    def test_pattern_message_carries_bits_and_hide_carries_none(self):
        msg = sc.pattern_message(self.layout, True, 7)
        self.assertEqual(("calib_pattern", 7, True, 6), (msg["t"], msg["id"], msg["show"], msg["grid"]))
        self.assertEqual(sc.marker_bits(3), msg["markers"][3]["bits"])
        self.assertNotIn("markers", sc.pattern_message(self.layout, False, 7))


@unittest.skipUnless(helpers.HAVE_CV, "needs OpenCV + numpy")
class DetectAndFit(unittest.TestCase):
    def test_all_markers_are_found_in_a_tilted_view(self):
        self.assertEqual(set(range(15)), set(sc.MarkerDetector().detect(camera_image())))

    def test_fit_matches_the_true_mapping_to_a_few_projector_pixels(self):
        res = fit_frames([camera_image(seed=s) for s in range(6)])
        self.assertTrue(res["ok"], res)
        self.assertEqual("good", res["quality"])
        self.assertEqual(15, res["markers"])
        self.assertLess(res["rms_px"], 4)
        self.assertLess(worst_error_px(res["h"]), 5)

    def test_result_is_normalized_like_the_hub_expects(self):
        res = fit_frames([camera_image()])
        self.assertEqual(9, len(res["h"]))
        self.assertAlmostEqual(1.0, res["h"][8])

    def test_a_mirrored_camera_is_handled(self):
        # The agent flips the image for hand tracking; the markers are detected in the unflipped image and flipped back.
        res = fit_frames([camera_image()], mirrored=True)
        self.assertTrue(res["ok"], res)
        self.assertLess(worst_error_px(res["h"], mirrored=True), 5)

    def test_partly_covered_pattern_still_calibrates(self):
        res = fit_frames([camera_image(cover=(90, 60, 330, 250))])  # top-left third hidden (a person, a lamp...)
        self.assertTrue(res["ok"], res)
        self.assertLess(res["markers"], 15)
        self.assertLess(worst_error_px(res["h"]), 8)

    def test_no_pattern_fails_with_a_reason_instead_of_guessing(self):
        res = fit_frames([camera_image(blank=True)])
        self.assertFalse(res["ok"])
        self.assertIn("no calibration markers", res["reason"])

    def test_markers_bunched_in_one_spot_are_refused(self):
        layout = sc.marker_layout(W, H)
        cam = [[0.5 + 0.001 * i, 0.5 + 0.0007 * (i % 5)] for i in range(20)]
        scr = [c for m in layout[:5] for c in sc.screen_corners(m)]
        self.assertFalse(sc.fit_homography(cam, scr, W, H)["ok"])

    def test_one_wildly_wrong_corner_is_rejected_by_ransac(self):
        layout = sc.marker_layout(W, H)
        det = sc.MarkerDetector().detect(camera_image())
        cam, scr = [], []
        for m in layout:
            cam += det[m["id"]]
            scr += sc.screen_corners(m)
        cam[0] = [0.9, 0.9]  # a misread corner far from where it belongs
        res = sc.fit_homography(cam, scr, W, H)
        self.assertTrue(res["ok"])
        self.assertEqual(len(cam) - 1, res["inliers"])
        self.assertLess(worst_error_px(res["h"]), 5)


@unittest.skipUnless(helpers.HAVE_CV, "needs OpenCV + numpy")
class BrightRoom(unittest.TestCase):
    def averaged(self, invert, gain=1.0):
        return np.mean([sc.to_gray(bright_room_image(invert=invert, gain=gain, seed=s)) for s in range(8)], axis=0)

    def test_the_plain_picture_is_unreadable_in_a_bright_room(self):
        self.assertLessEqual(len(sc.MarkerDetector().detect(bright_room_image())), 2)

    def test_the_difference_image_recovers_the_markers_and_an_accurate_fit(self):
        diff = sc.difference_image(self.averaged(False), self.averaged(True))
        acc = sc.Accumulator()
        acc.prefer(sc.MarkerDetector().detect(diff), set(range(15)))
        res = acc.fit(sc.marker_layout(W, H), W, H)
        self.assertTrue(res["ok"], res)
        self.assertGreaterEqual(res["markers"], 13)
        self.assertEqual("difference", res["method"])
        self.assertLess(worst_error_px(res["h"]), 6)

    def test_an_exposure_change_between_the_two_phases_is_compensated(self):
        for gain in (0.85, 1.15):
            diff = sc.difference_image(self.averaged(False), self.averaged(True, gain))
            self.assertGreaterEqual(len(sc.MarkerDetector().detect(diff)), 12, "gain %.2f" % gain)


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@unittest.skipUnless(helpers.HAVE_CV, "needs OpenCV + numpy")
class AutoCalibratorFlow(unittest.TestCase):
    def setUp(self):
        self.sent, self.clock = [], FakeClock()
        self.cal = sc.AutoCalibrator(self.sent.append, [0], settle_s=1.0, want_frames=4, timeout_s=10, clock=self.clock)
        self.img = camera_image()

    def test_start_asks_the_hub_to_show_the_pattern(self):
        self.cal.start({"id": 3, "screen": [W, H]})
        self.assertTrue(self.cal.active)
        self.assertEqual(("calib_pattern", 3, True, 15), (self.sent[0]["t"], self.sent[0]["id"], self.sent[0]["show"],
                                                          len(self.sent[0]["markers"])))

    def test_frames_before_the_pattern_has_settled_are_ignored(self):
        self.cal.start({"id": 1, "screen": [W, H]})
        for _ in range(10):
            self.cal.feed(0, self.img)
        self.assertEqual(1, len(self.sent))  # only the pattern message: nothing collected yet

    def test_enough_frames_produce_a_result_for_the_hub(self):
        self.cal.start({"id": 5, "screen": [W, H]})
        self.clock.t += 1.5
        for _ in range(4):
            self.cal.feed(0, self.img)
        msg = self.sent[-1]
        self.assertEqual(("calib_result", 5), (msg["t"], msg["id"]))
        r = msg["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual(0, r["cam"])
        self.assertLess(worst_error_px(r["h"]), 5)
        self.assertFalse(self.cal.active)

    def test_feed_does_nothing_when_no_calibration_is_running(self):
        self.cal.feed(0, self.img)
        self.assertEqual([], self.sent)

    def test_timeout_without_the_pattern_reports_a_failure(self):
        self.cal.start({"id": 2, "screen": [W, H]})
        blank = camera_image(blank=True)
        self.clock.t += 2
        self.cal.feed(0, blank)
        self.clock.t += 10
        self.cal.feed(0, blank)
        r = self.sent[-1]["results"][0]
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["reason"])

    def test_no_pattern_at_all_tries_both_phases_then_explains(self):
        self.cal.start({"id": 3, "screen": [W, H]})
        blank = camera_image(blank=True)
        while self.cal.active and self.clock.t < 200:
            self.clock.t += 0.1
            self.cal.feed(0, blank)
        self.assertTrue(any(m.get("invert") for m in self.sent), "the inverted phase was tried before giving up")
        r = self.sent[-1]["results"][0]
        self.assertFalse(r["ok"])
        self.assertIn("covering it", r["reason"])
        self.assertLess(self.clock.t, 104, "it gives up as soon as both phases are done, not at the timeout")
        self.assertFalse(self.cal.active)

    def test_cancel_stops_without_a_result(self):
        self.cal.start({"id": 4, "screen": [W, H]})
        self.cal.cancel()
        self.clock.t += 2
        self.cal.feed(0, self.img)
        self.assertEqual(["calib_pattern"], [m["t"] for m in self.sent])

    def test_a_dark_room_needs_no_inverted_phase(self):
        self.cal.start({"id": 5, "screen": [W, H]})
        self.clock.t += 1.5
        for _ in range(4):
            self.cal.feed(0, self.img)
        self.assertFalse(any(m.get("invert") for m in self.sent))
        self.assertEqual("plain", self.sent[-1]["results"][0]["method"])

    def test_a_bright_room_is_calibrated_through_the_inverted_phase(self):
        # The fake camera shows whichever pattern the agent last asked the display for, in a bright room.
        def frame():
            shown = [m for m in self.sent if m["t"] == "calib_pattern"][-1]
            return bright_room_image(invert=shown.get("invert", False), gain=1.1 if shown.get("invert") else 1.0,
                                     seed=int(self.clock.t * 1000) % 997)
        self.cal.start({"id": 6, "screen": [W, H]})
        for _ in range(40):
            self.clock.t += 0.1
            self.cal.feed(0, frame())
            if not self.cal.active:
                break
        inverted = [m for m in self.sent if m["t"] == "calib_pattern" and m.get("invert")]
        self.assertEqual(1, len(inverted), "the inverted pattern is shown once")
        r = self.sent[-1]["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual("difference", r["method"])
        self.assertLess(worst_error_px(r["h"]), 6)

    def test_a_bright_room_with_a_mirrored_camera(self):
        def frame():
            shown = [m for m in self.sent if m["t"] == "calib_pattern"][-1]
            return bright_room_image(invert=shown.get("invert", False), seed=int(self.clock.t * 1000) % 997)
        self.cal.start({"id": 7, "screen": [W, H]})
        for _ in range(40):
            self.clock.t += 0.1
            self.cal.feed(0, frame(), mirrored=True)
            if not self.cal.active:
                break
        r = self.sent[-1]["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertLess(worst_error_px(r["h"], mirrored=True), 6)

    def test_two_cameras_each_get_their_own_result(self):
        cal = sc.AutoCalibrator(self.sent.append, [0, 1], settle_s=0, want_frames=3, timeout_s=5, clock=self.clock)
        cal.start({"id": 9, "screen": [W, H]})
        blank = camera_image(blank=True)
        for _ in range(3):
            cal.feed(0, self.img)
            cal.feed(1, blank)
        self.clock.t += 6
        cal.feed(1, blank)
        res = {r["cam"]: r for r in self.sent[-1]["results"]}
        self.assertTrue(res[0]["ok"])
        self.assertFalse(res[1]["ok"])

    def test_the_layout_follows_the_screen_size_the_hub_reports(self):
        self.cal.start({"id": 1, "screen": [1280, 800]})
        m = self.sent[0]["markers"][0]
        self.assertAlmostEqual((m["x1"] - m["x0"]) * 1280, (m["y1"] - m["y0"]) * 800, places=6)


# Stand-ins for MediaPipe and Johnny's camera class, so the agent's real loops run without hardware.
class StillTracker:
    def __init__(self, model, max_hands=1):
        pass

    def process(self, rgb):
        return [hand(0.5, 0.5)]

    def close(self):
        pass


class PatternCamera:
    """Always delivers the camera picture of the projected pattern (as the Pi camera would while calibrating)."""
    frame = None

    def __init__(self, backend, index, width, height, fps, exposure_us=None, gain=None):
        self.n = 0

    def start(self):
        return self

    def newest(self, last_seq, timeout=2.0):
        time.sleep(0.01)
        self.n += 1
        return PatternCamera.frame, time.monotonic(), self.n

    def stop(self):
        pass


@unittest.skipUnless(helpers.HAVE_CV, "needs OpenCV + numpy")
class AgentRunsTheCalibration(unittest.TestCase):
    def setUp(self):
        saved = (agent.HandTracker, motion_bridge.camera_class)
        agent.HandTracker = StillTracker
        motion_bridge.camera_class = lambda: PatternCamera
        self.addCleanup(lambda: (setattr(agent, "HandTracker", saved[0]), setattr(motion_bridge, "camera_class", saved[1])))

    def calibrator(self, link):
        return sc.AutoCalibrator(link.send, [0], settle_s=0.0, want_frames=4, timeout_s=5)

    def results(self, link):
        return [m for m in link.sent if m.get("t") == "calib_result"]

    def test_video_source_calibrates_from_a_photo_of_the_pattern(self):
        path = os.path.join(tempfile.mkdtemp(), "pattern.png")
        cv2.imwrite(path, camera_image())
        link = FakeLink()
        args = make_args(source="video", video=path, seconds=0.6, fps=30)
        args.calib = self.calibrator(link)
        args.calib.start({"id": 11, "screen": [W, H]})
        agent.run_video(link, args)
        res = self.results(link)
        self.assertEqual(1, len(res))
        self.assertEqual(11, res[0]["id"])
        self.assertTrue(res[0]["results"][0]["ok"], res)
        self.assertLess(worst_error_px(res[0]["results"][0]["h"]), 5)
        self.assertTrue(any(m.get("t") == "hands" for m in link.sent), "hand tracking keeps running meanwhile")

    def test_mirrored_camera_gets_a_mapping_for_the_flipped_image(self):
        PatternCamera.frame = camera_image()
        link = FakeLink()
        args = make_args(source="picamera", seconds=0.6, fps=30, mirror=True)
        args.calib = self.calibrator(link)
        args.calib.start({"id": 3, "screen": [W, H]})
        agent.run_camera(link, args)
        r = self.results(link)[0]["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertLess(worst_error_px(r["h"], mirrored=True), 5)

    def test_a_crash_inside_calibration_is_reported_and_tracking_continues(self):
        link = FakeLink()
        args = make_args(source="picamera", seconds=0.4, fps=30)
        args.calib = self.calibrator(link)
        args.calib.start({"id": 8, "screen": [W, H]})
        PatternCamera.frame = camera_image()

        def broken(bgr, mirrored=False):
            raise ValueError("detector exploded")
        args.calib.detector.detect = broken
        agent.run_camera(link, args)
        res = self.results(link)
        self.assertEqual(8, res[0]["id"])
        self.assertFalse(res[0]["results"][0]["ok"])
        self.assertIn("error", res[0]["results"][0]["reason"])
        self.assertFalse(args.calib.active)
        self.assertGreater(sum(m.get("t") == "hands" for m in link.sent), 5)

    def link_for(self, args):
        lk = agent.Link.__new__(agent.Link)  # a Link without a socket: only its message handling is used
        lk.args, lk.sent = args, []
        lk.send = lk.sent.append
        return lk

    def test_a_source_without_images_answers_at_once(self):
        lk = self.link_for(types.SimpleNamespace(calib=None, source="synthetic", cam_list=[0, 1]))
        lk.start_calibration({"t": "calib_capture", "id": 4, "screen": [W, H]})
        self.assertEqual(4, lk.sent[0]["id"])
        self.assertEqual([False, False], [r["ok"] for r in lk.sent[0]["results"]])
        self.assertIn("synthetic", lk.sent[0]["results"][0]["reason"])

    def test_a_camera_source_starts_and_shows_the_pattern(self):
        args = types.SimpleNamespace(source="picamera", cam_list=[0], calib=None)
        lk = self.link_for(args)
        args.calib = sc.AutoCalibrator(lk.send, [0])
        lk.start_calibration({"t": "calib_capture", "id": 6, "screen": [W, H]})
        self.assertEqual(("calib_pattern", 6, True), (lk.sent[0]["t"], lk.sent[0]["id"], lk.sent[0]["show"]))
        self.assertTrue(args.calib.active)


if __name__ == "__main__":
    unittest.main()
