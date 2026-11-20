# edge/tests/test_camera_paths.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   The agent's real-camera and video code paths, using stand-ins for the hardware so they run anywhere that has
#   OpenCV + numpy (they are skipped otherwise):
#     - Johnny's LatestFrameCamera (the "always give me the newest frame" capture thread),
#     - agent.run_camera: frames -> hand tracking -> motion engine -> messages (wall-clock timestamps, mirror,
#       camera failure, JPEG sending, cleanup),
#     - agent.run_video: a photo / video file standing in for the camera.
# ---------------------------------------------------------------------------
import os
import tempfile
import time
import unittest

import helpers
from helpers import FakeLink, agent, hand, make_args, motion_bridge

if helpers.HAVE_CV:
    import cv2
    import numpy as np


# A scripted tracker standing in for MediaPipe: a pointing hand sliding to the right a little each call.
class FakeTracker:
    instances = []

    def __init__(self, model, max_hands=1):
        self.n = 0
        self.closed = False
        self.max_hands = max_hands
        FakeTracker.instances.append(self)

    def process(self, rgb):
        self.n += 1
        return [hand(0.2 + 0.005 * self.n, 0.5)]

    def close(self):
        self.closed = True


# A scripted camera class standing in for Johnny's LatestFrameCamera.
class FakeCamera:
    instances = []
    fail_after = None
    silent = False  # True: the camera never delivers a frame (cable pulled, another program holds it)

    def __init__(self, backend, index, width, height, fps, exposure_us=None, gain=None):
        self.args = (backend, index, width, height, fps, exposure_us, gain)
        self.n = 0
        self.stopped = False
        FakeCamera.instances.append(self)

    def start(self):
        return self

    def newest(self, last_seq, timeout=2.0):
        if FakeCamera.silent:
            time.sleep(0.02)
            return None
        time.sleep(0.004)
        self.n += 1
        if FakeCamera.fail_after is not None and self.n > FakeCamera.fail_after:
            raise RuntimeError("Camera stopped supplying frames")
        frame = np.zeros((48, 64, 3), np.uint8)
        frame[:, :, 2] = 200  # red in BGR order: lets tests see whether mirror / colour handling changed anything
        return frame, time.monotonic() - 0.02, self.n  # a frame that is already 20 ms old when we get it

    def stop(self):
        self.stopped = True


@unittest.skipUnless(helpers.HAVE_CV, "needs OpenCV + numpy")
class LatestFrameCameraBehaviour(unittest.TestCase):
    # Fake OpenCV capture: produces numbered frames quickly; the number is stored in pixel [0,0,0].
    class FakeCapture:
        def __init__(self, index, backend=None):
            self.n = 0
            self.released = False

        def isOpened(self):
            return True

        def set(self, *a):
            return True

        def read(self):
            time.sleep(0.002)
            self.n += 1
            frame = np.zeros((8, 8, 3), np.uint8)
            frame[0, 0, 0] = self.n % 250
            return True, frame

        def release(self):
            self.released = True

    def setUp(self):
        self._orig = cv2.VideoCapture
        cv2.VideoCapture = self.FakeCapture
        self.cam = motion_bridge.camera_class()("opencv", 0, 8, 8, 30).start()
        self.addCleanup(self.cam.stop)
        self.addCleanup(setattr, cv2, "VideoCapture", self._orig)

    def test_delivers_frames_with_increasing_sequence_numbers(self):
        a = self.cam.newest(0, timeout=2)
        b = self.cam.newest(a[2], timeout=2)
        self.assertGreater(b[2], a[2])

    def test_a_slow_consumer_skips_old_frames_and_gets_the_newest(self):
        first = self.cam.newest(0, timeout=2)
        time.sleep(0.15)  # the consumer is slow: many frames are produced meanwhile
        second = self.cam.newest(first[2], timeout=2)
        self.assertGreater(second[2] - first[2], 5, "expected the stale frames to be skipped")

    def test_returns_none_when_no_new_frame_arrived(self):
        got = self.cam.newest(0, timeout=2)
        self.cam.closed = True  # stop producing
        time.sleep(0.05)
        self.assertIsNone(self.cam.newest(self.cam.seq, timeout=0.05))
        self.cam.closed = False

    def test_frame_timestamps_are_monotonic_and_recent(self):
        _, t, _ = self.cam.newest(0, timeout=2)
        self.assertLess(abs(time.monotonic() - t), 1.0)

    def test_stop_releases_the_device(self):
        device = self.cam.device
        self.cam.stop()
        self.assertTrue(device.released)

    def test_unopenable_camera_gives_a_clear_error(self):
        class Closed(self.FakeCapture):
            def isOpened(self):
                return False
        cv2.VideoCapture = Closed
        with self.assertRaises(RuntimeError):
            motion_bridge.camera_class()("opencv", 3, 8, 8, 30).start()


@unittest.skipUnless(helpers.HAVE_CV, "needs OpenCV + numpy")
class RunCamera(unittest.TestCase):
    def setUp(self):
        FakeTracker.instances.clear()
        FakeCamera.instances.clear()
        FakeCamera.fail_after = None
        FakeCamera.silent = False
        self._saved = (agent.HandTracker, motion_bridge.camera_class, agent.STALL_WARN_SECONDS)
        agent.HandTracker = FakeTracker
        motion_bridge.camera_class = lambda: FakeCamera
        self.addCleanup(self.restore)

    def restore(self):
        agent.HandTracker, motion_bridge.camera_class, agent.STALL_WARN_SECONDS = self._saved
        FakeCamera.silent = False

    def run_it(self, **kw):
        link = FakeLink()
        args = make_args(source="picamera", seconds=0.4, fps=30, bridge=motion_bridge.MotionBridge(1), **kw)
        agent.run_camera(link, args)
        return link, args

    def test_sends_numbered_hand_messages_with_motion(self):
        link, _ = self.run_it()
        self.assertGreater(len(link.sent), 10)
        self.assertEqual(list(range(len(link.sent))), [m["seq"] for m in link.sent])
        self.assertTrue(all("motion" in m and m["motion"]["detected_hands"] == 1 for m in link.sent))

    def test_timestamp_is_wall_clock_capture_time(self):
        link, _ = self.run_it()
        age = time.time() - link.sent[-1]["ts"] / 1e6
        self.assertTrue(0 <= age < 1.0, age)

    def test_picamera_source_uses_the_pi_backend_and_usb_uses_opencv(self):
        self.run_it()
        self.assertEqual("picamera2", FakeCamera.instances[0].args[0])
        FakeCamera.instances.clear()
        agent.run_camera(FakeLink(), make_args(source="camera", seconds=0.1, fps=30, bridge=None))
        self.assertEqual("opencv", FakeCamera.instances[0].args[0])

    def test_camera_settings_are_passed_to_johnnys_camera(self):
        self.run_it(exposure_us=5000, gain=4.0, cam_list=[2], width=1280, height=720)
        self.assertEqual((2, 1280, 720, 30, 5000, 4.0), FakeCamera.instances[0].args[1:])

    def test_two_cameras_are_both_read(self):
        link, _ = self.run_it(cam_list=[0, 1])
        self.assertEqual({0, 1}, {m["cam"] for m in link.sent})
        self.assertEqual(2, len(FakeCamera.instances))

    def test_max_hands_reaches_the_tracker(self):
        self.run_it(max_hands=2)
        self.assertEqual(2, FakeTracker.instances[0].max_hands)

    def test_camera_failure_ends_with_a_clear_message_and_cleans_up(self):
        FakeCamera.fail_after = 5
        with self.assertRaises(SystemExit) as ctx:
            agent.run_camera(FakeLink(), make_args(source="picamera", seconds=5, fps=30, bridge=None))
        self.assertIn("camera error", str(ctx.exception))
        self.assertTrue(FakeCamera.instances[0].stopped)
        self.assertTrue(FakeTracker.instances[0].closed)

    def test_cameras_and_trackers_are_always_released(self):
        self.run_it()
        self.assertTrue(all(c.stopped for c in FakeCamera.instances))
        self.assertTrue(all(t.closed for t in FakeTracker.instances))

    def test_jpeg_frames_are_sent_only_when_asked(self):
        link, _ = self.run_it(send_frames=False)
        self.assertEqual([], link.jpegs)
        link, _ = self.run_it(send_frames=True)
        self.assertGreater(len(link.jpegs), 5)
        self.assertEqual(b"\xff\xd8", link.jpegs[0][1][:2])  # a real JPEG starts with FF D8

    def test_stops_when_the_link_dies(self):
        link = FakeLink()
        link.alive = False
        agent.run_camera(link, make_args(source="picamera", seconds=5, fps=30))
        self.assertEqual([], link.sent)

    def test_a_silent_camera_triggers_a_warning_instead_of_waiting_silently(self):
        import contextlib
        import io
        FakeCamera.silent = True
        agent.STALL_WARN_SECONDS = 0.1
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent.run_camera(FakeLink(), make_args(source="picamera", seconds=0.6, fps=30, cam_list=[3]))
        self.assertIn("camera 3 has delivered no frames", buf.getvalue())
        self.assertLess(buf.getvalue().count("no frames"), 8)  # a warning every so often, not a flood

    def test_no_warning_while_frames_keep_coming(self):
        import contextlib
        import io
        agent.STALL_WARN_SECONDS = 0.1
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent.run_camera(FakeLink(), make_args(source="picamera", seconds=0.4, fps=30))
        self.assertNotIn("no frames", buf.getvalue())

    def test_no_motion_key_when_motion_is_off(self):
        link = FakeLink()
        agent.run_camera(link, make_args(source="picamera", seconds=0.2, fps=30, bridge=None))
        self.assertTrue(link.sent and all("motion" not in m for m in link.sent))


@unittest.skipUnless(helpers.HAVE_CV, "needs OpenCV + numpy")
class RunVideo(unittest.TestCase):
    def setUp(self):
        FakeTracker.instances.clear()
        self._saved = agent.HandTracker
        agent.HandTracker = FakeTracker
        self.addCleanup(setattr, agent, "HandTracker", self._saved)
        self.dir = tempfile.mkdtemp()

    def photo(self):
        path = os.path.join(self.dir, "hands.png")
        cv2.imwrite(path, np.zeros((48, 64, 3), np.uint8))
        return path

    def video(self, frames=20):
        path = os.path.join(self.dir, "hands.avi")
        out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 30, (64, 48))
        for _ in range(frames):
            out.write(np.zeros((48, 64, 3), np.uint8))
        out.release()
        return path

    def test_a_photo_is_repeated_as_frames(self):
        link = FakeLink()
        agent.run_video(link, make_args(source="video", video=self.photo(), seconds=0.4, fps=60,
                                        bridge=motion_bridge.MotionBridge(1)))
        self.assertGreater(len(link.sent), 10)
        self.assertTrue(all("motion" in m for m in link.sent))
        self.assertTrue(FakeTracker.instances[0].closed)

    def test_a_video_loops_when_it_ends(self):
        link = FakeLink()
        agent.run_video(link, make_args(source="video", video=self.video(10), seconds=0.6, fps=60))
        self.assertGreater(len(link.sent), 12, "video of 10 frames should have been replayed from the start")

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as ctx:
            agent.run_video(FakeLink(), make_args(source="video", video="no_such_file.png"))
        self.assertIn("--video", str(ctx.exception))

    def test_no_file_given_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            agent.run_video(FakeLink(), make_args(source="video", video=None))

    def test_unreadable_video_is_a_clear_error(self):
        bad = os.path.join(self.dir, "broken.avi")
        with open(bad, "wb") as fh:
            fh.write(b"not a video")
        with self.assertRaises(SystemExit):
            agent.run_video(FakeLink(), make_args(source="video", video=bad, seconds=2))

    def test_frame_rate_is_respected(self):
        link = FakeLink()
        agent.run_video(link, make_args(source="video", video=self.photo(), seconds=1.0, fps=20))
        self.assertTrue(12 <= len(link.sent) <= 28, len(link.sent))


if __name__ == "__main__":
    unittest.main()
