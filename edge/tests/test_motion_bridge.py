# edge/tests/test_motion_bridge.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   Johnny's MotionEngine as it is used by our agent (through edge/motion_bridge.py): the shape of its output,
#   hand IDs, pinch / swipe / zoom events, robustness to bad input, and that it behaves identically every run.
#   Johnny's own 13 tests (in edge/pi_hand_tracking_v2/tests) cover the engine in isolation; these cover the
#   integration with our hand format.
# ---------------------------------------------------------------------------
import json
import unittest

import helpers
from helpers import hand, motion_bridge, run_frames, types_of


class OutputShape(unittest.TestCase):
    def test_result_has_the_keys_the_hub_reads(self):
        out = motion_bridge.MotionBridge(2).process(0, [hand(0.5, 0.5)], 1.0)
        self.assertEqual({"hands", "detected_hands", "events"}, set(out))
        h = out["hands"][0]
        for key in ("id", "handedness", "detected", "predicted", "tip", "center", "pinching"):
            self.assertIn(key, h)
        self.assertEqual(2, len(h["tip"]))

    def test_result_is_json_serializable(self):
        out = motion_bridge.MotionBridge(2).process(0, [hand(0.4, 0.5), hand(0.7, 0.5, right=False)], 1.0)
        self.assertEqual(out, json.loads(json.dumps(out)))

    def test_tip_is_the_index_fingertip_in_normalized_coordinates(self):
        out = motion_bridge.MotionBridge(1).process(0, [hand(0.30, 0.60)], 1.0)
        tip = out["hands"][0]["tip"]
        self.assertAlmostEqual(0.30, tip[0], places=3)
        self.assertAlmostEqual(0.60, tip[1], places=3)

    def test_handedness_label_comes_from_our_right_flag(self):
        out = motion_bridge.MotionBridge(2).process(0, [hand(0.3, 0.5, right=True), hand(0.7, 0.5, right=False)], 1.0)
        self.assertEqual({"Right", "Left"}, {h["handedness"] for h in out["hands"]})


class Identity(unittest.TestCase):
    def test_one_hand_keeps_its_id_while_moving_fast(self):
        b = motion_bridge.MotionBridge(1)
        ids = {b.process(0, [hand(0.2 + 0.2 * i, 0.5)], 1.0 + 0.1 * i)["hands"][0]["id"] for i in range(3)}
        self.assertEqual(1, len(ids))

    def test_two_hands_get_two_ids(self):
        out = motion_bridge.MotionBridge(2).process(0, [hand(0.3, 0.5), hand(0.7, 0.5, right=False)], 1.0)
        self.assertEqual(2, out["detected_hands"])
        self.assertEqual(2, len({h["id"] for h in out["hands"]}))

    def test_ids_survive_the_hands_being_reported_in_swapped_order(self):
        b = motion_bridge.MotionBridge(2)
        a, c = hand(0.25, 0.5, right=False), hand(0.75, 0.5)
        first = b.process(0, [a, c], 1.0)
        second = b.process(0, [hand(0.73, 0.5), hand(0.27, 0.5, right=False)], 1.05)  # reported in the other order
        by_side_first = {h["handedness"]: h["id"] for h in first["hands"]}
        by_side_second = {h["handedness"]: h["id"] for h in second["hands"]}
        self.assertEqual(by_side_first, by_side_second)

    def test_each_camera_has_its_own_ids(self):
        b = motion_bridge.MotionBridge(1)
        b.process(0, [hand(0.5, 0.5)], 1.0)
        out = b.process(1, [hand(0.5, 0.5)], 1.0)
        self.assertEqual(1, out["hands"][0]["id"])  # camera 1 starts its own numbering

    def test_a_hand_that_disappears_briefly_comes_back_with_the_same_id(self):
        b = motion_bridge.MotionBridge(1)
        first = b.process(0, [hand(0.4, 0.5)], 1.0)["hands"][0]["id"]
        b.process(0, [], 1.10)
        again = b.process(0, [hand(0.42, 0.5)], 1.18)["hands"][0]["id"]
        self.assertEqual(first, again)

    def test_hand_that_stays_away_is_forgotten_and_gets_a_new_id(self):
        b = motion_bridge.MotionBridge(1)
        first = b.process(0, [hand(0.4, 0.5)], 1.0)["hands"][0]["id"]
        b.process(0, [], 3.0)
        again = b.process(0, [hand(0.4, 0.5)], 3.1)["hands"][0]["id"]
        self.assertNotEqual(first, again)

    def test_reset_clears_everything(self):
        b = motion_bridge.MotionBridge(1)
        b.process(0, [hand(0.4, 0.5)], 1.0)
        b.reset()
        self.assertEqual(1, b.process(0, [hand(0.4, 0.5)], 2.0)["hands"][0]["id"])


class Pinch(unittest.TestCase):
    def test_pinch_start_then_end(self):
        ev = run_frames([(1.00, [hand(0.5, 0.5, pinch=0.0)]),
                         (1.05, [hand(0.5, 0.5, pinch=1.0)]),
                         (1.10, [hand(0.5, 0.5, pinch=1.0)]),
                         (1.15, [hand(0.5, 0.5, pinch=0.0)])])
        self.assertEqual(["HAND_FOUND", "PINCH_START", "PINCH_END"], types_of(ev))

    def test_no_pinch_event_while_the_fingers_stay_apart(self):
        ev = run_frames([(1 + 0.05 * i, [hand(0.5, 0.5)]) for i in range(10)])
        self.assertNotIn("PINCH_START", types_of(ev))

    def test_holding_a_pinch_gives_one_start_not_many(self):
        ev = run_frames([(1 + 0.033 * i, [hand(0.5, 0.5, pinch=1.0)]) for i in range(20)])
        self.assertEqual(1, types_of(ev).count("PINCH_START"))

    def test_pinch_is_released_when_the_hand_disappears(self):
        b = motion_bridge.MotionBridge(1)
        b.process(0, [hand(0.5, 0.5, pinch=1.0)], 1.0)
        out = b.process(0, [], 1.12)
        self.assertIn("PINCH_END", [e["type"] for e in out["events"]])

    def test_half_closed_fingers_do_not_count_as_a_pinch(self):
        ev = run_frames([(1.0, [hand(0.5, 0.5, pinch=0.3)]), (1.05, [hand(0.5, 0.5, pinch=0.3)])])
        self.assertNotIn("PINCH_START", types_of(ev))


class Swipe(unittest.TestCase):
    def test_fast_move_right_is_a_right_swipe(self):
        ev = run_frames([(1.00, [hand(0.15, 0.5)]), (1.10, [hand(0.35, 0.5)])])
        self.assertIn("SWIPE_RIGHT", types_of(ev))

    def test_all_four_directions(self):
        cases = {"SWIPE_RIGHT": ((0.15, 0.5), (0.35, 0.5)), "SWIPE_LEFT": ((0.6, 0.5), (0.4, 0.5)),
                 "SWIPE_UP": ((0.5, 0.7), (0.5, 0.45)), "SWIPE_DOWN": ((0.5, 0.3), (0.5, 0.55))}
        for name, (a, b) in cases.items():
            with self.subTest(direction=name):
                ev = run_frames([(1.00, [hand(*a)]), (1.11, [hand(*b)])])
                self.assertIn(name, types_of(ev))

    def test_slow_drift_is_not_a_swipe(self):
        ev = run_frames([(1 + 0.033 * i, [hand(0.2 + 0.004 * i, 0.5)]) for i in range(40)])
        self.assertFalse([e for e in types_of(ev) if e.startswith("SWIPE")])

    def test_short_fast_twitch_is_not_a_swipe(self):
        ev = run_frames([(1.00, [hand(0.50, 0.5)]), (1.10, [hand(0.55, 0.5)])])
        self.assertFalse([e for e in types_of(ev) if e.startswith("SWIPE")])

    def test_no_swipe_is_invented_across_a_gap_in_detection(self):
        ev = run_frames([(1.00, [hand(0.10, 0.5)]), (1.20, []), (1.30, [hand(0.35, 0.5)])])
        self.assertFalse([e for e in types_of(ev) if e.startswith("SWIPE")])

    def test_no_swipe_while_pinching(self):
        ev = run_frames([(1.00, [hand(0.15, 0.5, pinch=1.0)]), (1.10, [hand(0.35, 0.5, pinch=1.0)])])
        self.assertFalse([e for e in types_of(ev) if e.startswith("SWIPE")])


class Zoom(unittest.TestCase):
    def test_hands_moving_apart_zoom_in(self):
        ev = run_frames([(1.0, [hand(0.40, 0.5), hand(0.60, 0.5, right=False)]),
                         (1.1, [hand(0.25, 0.5), hand(0.75, 0.5, right=False)])])
        self.assertIn("ZOOM_IN", types_of(ev))

    def test_hands_moving_together_zoom_out(self):
        ev = run_frames([(1.0, [hand(0.20, 0.5), hand(0.80, 0.5, right=False)]),
                         (1.1, [hand(0.38, 0.5), hand(0.62, 0.5, right=False)])])
        self.assertIn("ZOOM_OUT", types_of(ev))

    def test_one_hand_never_zooms(self):
        ev = run_frames([(1 + 0.05 * i, [hand(0.3 + 0.01 * i, 0.5)]) for i in range(20)])
        self.assertNotIn("ZOOM_IN", types_of(ev))
        self.assertNotIn("ZOOM_OUT", types_of(ev))

    def test_hands_holding_still_do_not_zoom(self):
        ev = run_frames([(1 + 0.05 * i, [hand(0.35, 0.5), hand(0.65, 0.5, right=False)]) for i in range(20)])
        self.assertFalse([e for e in types_of(ev) if e.startswith("ZOOM")])


class Robustness(unittest.TestCase):
    def test_empty_frame_is_fine(self):
        out = motion_bridge.MotionBridge(2).process(0, [], 1.0)
        self.assertEqual(0, out["detected_hands"])
        self.assertEqual([], out["events"])

    def test_hand_with_wrong_number_of_points_is_ignored(self):
        bad = {"right": True, "score": 0.9, "pts": [[0.5, 0.5, 0.0]] * 20}
        out = motion_bridge.MotionBridge(2).process(0, [bad], 1.0)
        self.assertEqual(0, out["detected_hands"])

    def test_only_max_hands_are_tracked(self):
        out = motion_bridge.MotionBridge(1).process(0, [hand(0.3, 0.5), hand(0.7, 0.5)], 1.0)
        self.assertEqual(1, out["detected_hands"])

    def test_time_going_backwards_does_not_crash(self):
        b = motion_bridge.MotionBridge(1)
        b.process(0, [hand(0.5, 0.5)], 5.0)
        b.process(0, [hand(0.5, 0.5)], 4.0)

    def test_predicted_hand_is_marked_and_never_produces_events(self):
        b = motion_bridge.MotionBridge(1)
        b.process(0, [hand(0.20, 0.5)], 1.0)
        b.process(0, [hand(0.30, 0.5)], 1.07)
        out = b.process(0, [], 1.12)
        self.assertEqual(0, out["detected_hands"])
        self.assertTrue(out["hands"][0]["predicted"])
        self.assertFalse(out["hands"][0]["detected"])
        self.assertEqual([], out["events"])


class Determinism(unittest.TestCase):
    def test_same_input_gives_identical_output(self):
        frames = [(1 + 0.033 * i, [hand(0.2 + 0.01 * i, 0.5, pinch=1.0 if 10 < i < 15 else 0.0)]) for i in range(30)]

        def run():
            b = motion_bridge.MotionBridge(2)
            return [json.dumps(b.process(0, h, t), sort_keys=True) for t, h in frames]

        self.assertEqual(run(), run())


class VendoredCodeIsJohnnys(unittest.TestCase):
    def test_bridge_uses_the_tracker_in_johnnys_folder(self):
        mod = motion_bridge.tracker_module()
        self.assertTrue(mod.__file__.replace("\\", "/").endswith("pi_hand_tracking_v2/tracker.py"))

    def test_bridge_loads_his_camera_class(self):
        self.assertEqual("LatestFrameCamera", motion_bridge.camera_class().__name__)


if __name__ == "__main__":
    unittest.main()
