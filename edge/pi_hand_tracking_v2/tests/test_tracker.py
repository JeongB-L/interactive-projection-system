import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracker import HandDetection, MotionEngine, OneEuro2D


def hand(x, y=.5, *, pinch=False, label="Unknown"):
    tip = (x, y)
    thumb = (x + (.012 if pinch else .11), y)
    return HandDetection((x, y + .08), tip, thumb, .13, label)


class TrackingTests(unittest.TestCase):
    def test_empty(self):
        output = MotionEngine().update([], 1)
        self.assertEqual(0, output["detected_hands"])

    def test_two_hands_and_reorder(self):
        engine = MotionEngine()
        first = engine.update([hand(.25, label="Left"), hand(.75, label="Right")], 1)
        ids = [h["id"] for h in first["hands"]]
        second = engine.update([hand(.73, label="Right"), hand(.27, label="Left")], 1.05)
        self.assertEqual(ids, [h["id"] for h in second["hands"]])
        self.assertAlmostEqual(.27, second["hands"][0]["center"][0])
        self.assertEqual(2, second["detected_hands"])

    def test_two_hands_cross_without_switching_ids(self):
        e = MotionEngine()
        e.update([hand(.30, label="Left"), hand(.70, label="Right")], 1.0)
        e.update([hand(.45, label="Left"), hand(.55, label="Right")], 1.10)
        # The observed hand order reverses as the hands cross.
        out = e.update([hand(.35, label="Right"), hand(.65, label="Left")], 1.20)
        self.assertEqual([(1, "Left"), (2, "Right")],
                         [(h["id"], h["handedness"]) for h in out["hands"]])

    def test_fast_motion_preserves_id(self):
        e = MotionEngine()
        a = e.update([hand(.2)], 1.0)["hands"][0]["id"]
        b = e.update([hand(.43)], 1.10)["hands"][0]["id"]
        c = e.update([hand(.67)], 1.20)["hands"][0]["id"]
        self.assertEqual([a, a], [b, c])

    def test_predicted_is_not_reported_as_detection(self):
        e = MotionEngine()
        e.update([hand(.22)], 1.0)
        e.update([hand(.30)], 1.07)
        prediction = e.update([], 1.12)
        self.assertEqual(0, prediction["detected_hands"])
        self.assertTrue(prediction["hands"][0]["predicted"])
        self.assertFalse(prediction["hands"][0]["detected"])
        self.assertEqual([], prediction["events"])
        old = e.update([], 1.4)
        self.assertEqual([], old["hands"])

    def test_recovery_reuses_id(self):
        e = MotionEngine()
        orig = e.update([hand(.25)], 1.0)["hands"][0]["id"]
        e.update([], 1.10)
        current = e.update([hand(.3)], 1.18)["hands"][0]["id"]
        self.assertEqual(orig, current)

    def test_swipe_right_quick_without_repeats(self):
        e = MotionEngine()
        e.update([hand(.15)], 1.00)
        out = e.update([hand(.35)], 1.10)
        self.assertIn("SWIPE_RIGHT", [event["type"] for event in out["events"]])
        out = e.update([hand(.57)], 1.20)
        self.assertNotIn("SWIPE_RIGHT", [event["type"] for event in out["events"]])

    def test_swipe_up(self):
        e = MotionEngine()
        e.update([hand(.5, .7)], 1)
        result = e.update([hand(.5, .45)], 1.11)
        self.assertIn("SWIPE_UP", [event["type"] for event in result["events"]])

    def test_no_swipe_created_by_prediction_or_gap(self):
        e = MotionEngine()
        e.update([hand(.1)], 1)
        e.update([], 1.20)
        output = e.update([hand(.3)], 1.30)
        self.assertNotIn("SWIPE_RIGHT", [v["type"] for v in output["events"]])

    def test_pinch_hysteresis_and_release(self):
        e = MotionEngine()
        a = e.update([hand(.3, pinch=True)], 1.0)
        self.assertIn("PINCH_START", [v["type"] for v in a["events"]])
        b = e.update([hand(.31, pinch=True)], 1.05)
        self.assertNotIn("PINCH_START", [v["type"] for v in b["events"]])
        c = e.update([hand(.32, pinch=False)], 1.10)
        self.assertIn("PINCH_END", [v["type"] for v in c["events"]])

    def test_pinch_released_on_occlusion(self):
        e = MotionEngine()
        e.update([hand(.4, pinch=True)], 1.0)
        a = e.update([], 1.12)
        self.assertIn("PINCH_END", [v["type"] for v in a["events"]])

    def test_zoom(self):
        e = MotionEngine()
        e.update([hand(.3), hand(.6)], 1.0)
        a = e.update([hand(.23), hand(.68)], 1.1)
        self.assertIn("ZOOM_IN", [v["type"] for v in a["events"]])

    def test_smoothing_is_responsive(self):
        f = OneEuro2D(min_cutoff=1.6, beta=1.8)
        f.update((.2, .5), 1.0)
        slow = f.update((.202, .5), 1.033)
        self.assertTrue(.2 < slow[0] < .202)
        fast = f.update((.45, .5), 1.066)
        self.assertGreater(fast[0], slow[0])


if __name__ == "__main__":
    unittest.main()
