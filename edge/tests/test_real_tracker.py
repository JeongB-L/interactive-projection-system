# edge/tests/test_real_tracker.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   The REAL MediaPipe hand model on a REAL photo of hands, end to end through our agent and Johnny's engine.
#   These tests are skipped automatically unless MediaPipe, the model file and a test photo are all available:
#     model : models/hand_landmarker.task          (or set IPS_TEST_MODEL)   - setup.sh --pi downloads it
#     photo : data/hands.jpg                       (or set IPS_TEST_PHOTO)   - see docs/RUNBOOK.md for the sample
#   On the Raspberry Pi they run as part of `python3 ips.py test`.
# ---------------------------------------------------------------------------
import os
import time
import types
import unittest

import helpers
from helpers import ROOT, agent, motion_bridge

MODEL = os.environ.get("IPS_TEST_MODEL", os.path.join(ROOT, "models", "hand_landmarker.task"))
PHOTO = os.environ.get("IPS_TEST_PHOTO", os.path.join(ROOT, "data", "hands.jpg"))
READY = helpers.HAVE_MP and helpers.HAVE_CV and os.path.isfile(MODEL) and os.path.isfile(PHOTO)


@unittest.skipUnless(READY, "needs mediapipe, opencv, models/hand_landmarker.task and data/hands.jpg")
class RealModelOnRealPhoto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import cv2
        cls.cv2 = cv2
        cls.rgb = cv2.cvtColor(cv2.imread(PHOTO), cv2.COLOR_BGR2RGB)

    def new_tracker(self, max_hands=1):
        t = agent.HandTracker(MODEL, max_hands)
        self.addCleanup(t.close)
        return t

    def test_finds_a_hand_with_21_points(self):
        hands = self.new_tracker().process(self.rgb)
        self.assertEqual(1, len(hands))
        h = hands[0]
        self.assertEqual(21, len(h["pts"]))
        self.assertTrue(all(len(p) == 3 for p in h["pts"]))
        self.assertTrue(0.5 <= h["score"] <= 1.0)
        self.assertIsInstance(h["right"], bool)

    def test_points_are_normalized_image_coordinates(self):
        pts = self.new_tracker().process(self.rgb)[0]["pts"]
        self.assertTrue(all(-0.2 <= p[0] <= 1.2 and -0.2 <= p[1] <= 1.2 for p in pts))

    def test_two_hand_mode_returns_at_most_two_hands(self):
        hands = self.new_tracker(2).process(self.rgb)
        self.assertTrue(1 <= len(hands) <= 2)

    def test_blank_image_has_no_hands(self):
        import numpy as np
        self.assertEqual([], self.new_tracker().process(np.zeros((240, 320, 3), np.uint8)))

    def test_tracking_one_frame_is_fast_enough_on_this_machine(self):
        tracker = self.new_tracker()
        tracker.process(self.rgb)  # warm up
        t0 = time.perf_counter()
        for _ in range(5):
            tracker.process(self.rgb)
        per_frame_ms = (time.perf_counter() - t0) * 200
        self.assertLess(per_frame_ms, 500, "tracking took %.0f ms per frame" % per_frame_ms)

    def test_video_mode_accepts_many_frames_in_a_row(self):
        tracker = self.new_tracker()
        found = sum(bool(tracker.process(self.rgb)) for _ in range(15))
        self.assertGreaterEqual(found, 13)

    def test_johnnys_engine_tracks_the_real_hand(self):
        bridge = motion_bridge.MotionBridge(2)
        tracker = self.new_tracker()
        out = None
        for i in range(5):
            out = bridge.process(0, tracker.process(self.rgb), 1.0 + 0.033 * i)
        self.assertEqual(1, out["detected_hands"])
        self.assertTrue(all(0.0 <= v <= 1.0 for v in out["hands"][0]["tip"]))

    def test_open_palm_photo_is_not_reported_as_a_pinch(self):
        bridge = motion_bridge.MotionBridge(2)
        tracker = self.new_tracker()
        events = []
        for i in range(10):
            events += bridge.process(0, tracker.process(self.rgb), 1.0 + 0.033 * i)["events"]
        self.assertNotIn("PINCH_START", [e["type"] for e in events])

    def test_our_reading_of_mediapipe_matches_johnnys(self):
        # Johnny's main.py converts MediaPipe results with observations(); our agent has its own conversion.
        # On the same image both must give the same fingertip, otherwise the two engines would disagree.
        import mediapipe as mp
        motion_bridge.camera_class()  # makes sure his main.py is loaded
        his_main = __import__("sys").modules["pi_v2_main"]
        args = types.SimpleNamespace(detection_confidence=.5, presence_confidence=.5, tracking_confidence=.5)
        detector, _ = his_main.create_detector(mp, __import__("pathlib").Path(MODEL), 1, args)
        self.addCleanup(detector.close)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=__import__("numpy").ascontiguousarray(self.rgb))
        his = his_main.observations(detector.detect_for_video(image, 1))
        ours = self.new_tracker().process(self.rgb)
        self.assertEqual(len(his), len(ours))
        self.assertAlmostEqual(his[0].tip[0], ours[0]["pts"][8][0], places=3)
        self.assertAlmostEqual(his[0].tip[1], ours[0]["pts"][8][1], places=3)
        self.assertAlmostEqual(his[0].thumb[0], ours[0]["pts"][4][0], places=3)


if __name__ == "__main__":
    unittest.main()
