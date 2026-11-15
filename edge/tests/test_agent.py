# edge/tests/test_agent.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   The Pi agent (edge/agent.py) without any hardware: the message format the hub depends on, the fake-hand
#   routine (including both of Johnny's engine's events on it), replaying a recording, the progress line, and the
#   command-line interface.
# ---------------------------------------------------------------------------
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import helpers
from helpers import FakeLink, agent, hand, make_args, motion_bridge, run_frames, types_of

FPS = 30
PERIOD = agent.PERIOD


# Every frame of the scripted routine (and of the two-hand zoom, if asked) as (time, message) pairs.
def routine(seconds=PERIOD, two_hands=False, cam=0):
    import random
    rng = random.Random(7)
    return [(n / FPS, agent.synthetic_frame(cam, n, n / FPS, rng, two_hands)) for n in range(int(seconds * FPS))]


class MessageFormat(unittest.TestCase):
    def test_every_hand_has_21_points_of_three_numbers(self):
        for _, msg in routine(two_hands=True):
            for h in msg["hands"]:
                self.assertEqual(21, len(h["pts"]))
                for p in h["pts"]:
                    self.assertEqual(3, len(p))
                    self.assertTrue(all(isinstance(v, float) for v in p))

    def test_message_has_the_fields_the_hub_requires(self):
        _, msg = routine(1)[5]
        for key in ("t", "cam", "seq", "ts", "track_ms", "hands"):
            self.assertIn(key, msg)
        self.assertEqual("hands", msg["t"])
        self.assertIsInstance(msg["ts"], int)

    def test_message_survives_json(self):
        msg = agent.attach_motion(make_args(bridge=motion_bridge.MotionBridge(2)), routine(1)[10][1], 0.33)
        self.assertEqual(msg, json.loads(json.dumps(msg)))

    def test_hands_message_builder_rounds_track_time(self):
        m = agent.hands_message(1, 7, 123, 12.3456, [], None)
        self.assertEqual({"t": "hands", "cam": 1, "seq": 7, "ts": 123, "track_ms": 12.35, "hands": []}, m)
        self.assertNotIn("motion", m)

    def test_motion_key_only_present_when_motion_is_on(self):
        msg = {"t": "hands", "cam": 0, "seq": 1, "ts": 1, "track_ms": 1.0, "hands": [hand(0.5, 0.5)]}
        on = agent.attach_motion(make_args(bridge=motion_bridge.MotionBridge(1)), dict(msg), 1.0)
        self.assertIn("motion", on)
        stale = dict(msg, motion={"events": [{"type": "OLD"}]})
        off = agent.attach_motion(make_args(bridge=None), stale, 1.0)
        self.assertNotIn("motion", off)  # an old recorded result is removed, never forwarded


class SyntheticRoutine(unittest.TestCase):
    def setUp(self):
        self.args = make_args(bridge=motion_bridge.MotionBridge(2))

    def events(self, two_hands=False, seconds=PERIOD):
        bridge = motion_bridge.MotionBridge(2)
        ev = []
        for t, msg in routine(seconds, two_hands):
            out = bridge.process(0, msg["hands"], t)
            ev += [(round(t, 2), e["type"]) for e in out["events"]]
        return ev

    def test_two_clicks_per_routine(self):
        ev = types_of(self.events())
        self.assertEqual(2, ev.count("PINCH_START"))
        self.assertEqual(2, ev.count("PINCH_END"))

    def test_clicks_happen_when_the_script_says(self):
        starts = [t for t, e in self.events() if e == "PINCH_START"]
        self.assertTrue(2.0 <= starts[0] <= 2.3 and 4.0 <= starts[1] <= 4.3, starts)

    def test_a_right_swipe_is_reported_during_the_swipe_window(self):
        swipes = [t for t, e in self.events() if e == "SWIPE_RIGHT"]
        self.assertTrue(swipes and all(6.0 <= t <= 7.0 for t in swipes), swipes)

    def test_no_zoom_without_two_hands(self):
        self.assertNotIn("ZOOM_IN", types_of(self.events()))

    def test_two_hand_zoom_is_reported_when_enabled(self):
        zooms = [t for t, e in self.events(two_hands=True) if e == "ZOOM_IN"]
        self.assertTrue(zooms and all(10.2 <= t <= 12.0 for t in zooms), zooms)

    def test_hand_found_events_follow_each_appearance(self):
        found = [t for t, e in self.events() if e == "HAND_FOUND"]
        self.assertEqual([0.0, 6.0, 8.5], found)

    def test_nothing_happens_while_no_hand_is_in_view(self):
        quiet = [(t, e) for t, e in self.events() if 7.0 < t < 8.5]
        self.assertEqual([], quiet)

    def test_second_period_repeats_the_first(self):
        first = [e for t, e in self.events(seconds=2 * PERIOD) if t < PERIOD]
        second = [e for t, e in self.events(seconds=2 * PERIOD) if t >= PERIOD]
        self.assertEqual(types_of([(0, e) for e in first]).count("PINCH_START"),
                         types_of([(0, e) for e in second]).count("PINCH_START"))

    def test_two_hands_are_in_view_only_during_the_zoom_window(self):
        frames = routine(two_hands=True)
        self.assertTrue(all(len(m["hands"]) == 2 for t, m in frames if 11.0 <= t < 12.0))
        self.assertTrue(all(len(m["hands"]) == 1 for t, m in frames if 3.0 <= t < 4.0))
        self.assertTrue(all(len(m["hands"]) == 0 for t, m in frames if 7.0 <= t < 8.5))

    def test_without_the_flag_there_is_never_a_second_hand(self):
        self.assertTrue(all(len(m["hands"]) <= 1 for _, m in routine(two_hands=False)))


class RunSynthetic(unittest.TestCase):
    def test_runs_for_the_requested_time_and_numbers_frames(self):
        link, args = FakeLink(), make_args(seconds=0.5, fps=60.0)
        agent.run_synthetic(link, args)
        self.assertGreater(len(link.sent), 15)
        self.assertEqual(list(range(len(link.sent))), [m["seq"] for m in link.sent])

    def test_two_cameras_get_their_own_sequence_numbers(self):
        link, args = FakeLink(), make_args(seconds=0.3, fps=60.0, cam_list=[0, 1])
        agent.run_synthetic(link, args)
        for cam in (0, 1):
            seqs = [m["seq"] for m in link.sent if m["cam"] == cam]
            self.assertEqual(list(range(len(seqs))), seqs)
        self.assertEqual({0, 1}, {m["cam"] for m in link.sent})

    def test_motion_is_attached_when_the_bridge_is_on(self):
        link, args = FakeLink(), make_args(seconds=0.3, fps=60.0, bridge=motion_bridge.MotionBridge(1))
        agent.run_synthetic(link, args)
        self.assertTrue(all("motion" in m for m in link.sent))

    def test_no_motion_key_when_the_bridge_is_off(self):
        link = FakeLink()
        agent.run_synthetic(link, make_args(seconds=0.3, fps=60.0, bridge=None))
        self.assertTrue(all("motion" not in m for m in link.sent))

    def test_stops_when_the_link_dies(self):
        link = FakeLink()
        link.alive = False
        agent.run_synthetic(link, make_args(seconds=5, fps=60.0))
        self.assertEqual([], link.sent)


class Replay(unittest.TestCase):
    def write_recording(self, with_old_motion):
        path = tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False, encoding="utf-8")
        for n, (t, msg) in enumerate(routine(8.0)):
            msg = dict(msg, ts=int(1_000_000 + t * 1000))  # replay spacing of 1 ms per second of routine: fast
            if with_old_motion:
                msg["motion"] = {"events": [{"type": "STALE"}], "hands": [], "detected_hands": 0}
            path.write(json.dumps(msg) + "\n")
            if n == 40:  # a non-hand line in the middle (the hub also records actions): must be skipped
                path.write(json.dumps({"t": "act", "kind": "down"}) + "\n")
        path.close()
        self.addCleanup(os.unlink, path.name)
        return path.name

    def test_replays_every_hand_message_and_skips_other_lines(self):
        link = FakeLink()
        agent.run_replay(link, make_args(replay=self.write_recording(False), bridge=None))
        self.assertEqual(8 * FPS, len(link.sent))
        self.assertTrue(all(m["t"] == "hands" for m in link.sent))

    def test_replay_recomputes_motion_from_landmarks(self):
        link = FakeLink()
        agent.run_replay(link, make_args(replay=self.write_recording(True), bridge=motion_bridge.MotionBridge(2)))
        all_events = [e["type"] for m in link.sent for e in m["motion"]["events"]]
        self.assertNotIn("STALE", all_events)
        self.assertEqual(2, all_events.count("PINCH_START"))

    def test_replay_is_deterministic(self):
        path = self.write_recording(False)
        runs = []
        for _ in range(2):
            link = FakeLink()
            agent.run_replay(link, make_args(replay=path, bridge=motion_bridge.MotionBridge(2)))
            runs.append([json.dumps(m["motion"], sort_keys=True) for m in link.sent])
        self.assertEqual(runs[0], runs[1])

    def test_replay_stamps_messages_with_the_current_time(self):
        link = FakeLink()
        agent.run_replay(link, make_args(replay=self.write_recording(False), bridge=None))
        self.assertLess(abs(link.sent[-1]["ts"] / 1e6 - time.time()), 5)

    def test_empty_recording_is_reported_not_crashed(self):
        path = tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False)
        path.write(json.dumps({"t": "act"}) + "\n")
        path.close()
        self.addCleanup(os.unlink, path.name)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent.run_replay(FakeLink(), make_args(replay=path.name))
        self.assertIn("no hand frames", buf.getvalue())


class ProgressLine(unittest.TestCase):
    def test_prints_after_five_seconds_with_the_right_numbers(self):
        p = agent.Progress()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for i in range(10):
                p.add(i % 2 == 0, 30.0)
            p.t -= 6  # pretend 6 seconds have passed
            p.add(True, 30.0)
        out = buf.getvalue()
        self.assertIn("hand seen in", out)
        self.assertIn("tracking 30.0 ms/frame", out)

    def test_silent_before_five_seconds(self):
        p = agent.Progress()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(10):
                p.add(True, 10.0)
        self.assertEqual("", buf.getvalue())

    def test_restarts_counting_after_each_line(self):
        p = agent.Progress()
        with contextlib.redirect_stdout(io.StringIO()):
            p.t -= 6
            p.add(True, 10.0)
        self.assertEqual(0, p.frames)


class CommandLine(unittest.TestCase):
    def run_agent(self, *args):
        return subprocess.run([sys.executable, os.path.join(helpers.EDGE, "agent.py"), *args], capture_output=True,
                              text=True, timeout=60)

    def test_help_lists_the_new_options(self):
        out = self.run_agent("--help")
        self.assertEqual(0, out.returncode)
        for opt in ("--max-hands", "--no-motion", "--mirror", "--exposure-us", "--two-hands", "--source"):
            self.assertIn(opt, out.stdout)

    def test_unknown_source_is_rejected(self):
        self.assertNotEqual(0, self.run_agent("--source", "nonsense").returncode)

    def test_max_hands_only_accepts_1_or_2(self):
        self.assertNotEqual(0, self.run_agent("--max-hands", "3").returncode)

    def test_agent_keeps_retrying_instead_of_crashing_when_the_hub_is_down(self):
        # Regression: on a non-English system the "hub unreachable" message could not be printed and the agent died.
        proc = subprocess.Popen([sys.executable, os.path.join(helpers.EDGE, "agent.py"), "--hub", "127.0.0.1:1"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, PYTHONIOENCODING="cp1252"))
        try:
            time.sleep(3)
            still_running = proc.poll() is None
        finally:
            proc.terminate()
        out, err = proc.communicate(timeout=10)
        self.assertTrue(still_running, "agent exited: " + err[-400:])
        self.assertIn("hub unreachable", out)
        self.assertNotIn("Traceback", err)

    def test_safe_console_makes_unencodable_text_printable(self):
        import io as _io
        raw = _io.BytesIO()
        fake = _io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        saved, sys.stdout = sys.stdout, fake
        try:
            agent.safe_console()
            print("localized 대상 text")  # would raise UnicodeEncodeError before the fix
            fake.flush()
        finally:
            sys.stdout = saved
        self.assertIn(b"localized", raw.getvalue())


if __name__ == "__main__":
    unittest.main()
