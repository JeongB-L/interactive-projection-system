#!/usr/bin/env python3
# tests/e2e.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   The whole system for real, with nothing faked except the camera: it starts the compiled hub (build/ipsd) on
#   port 8099 with temporary storage, runs the real Pi agent (edge/agent.py) against it over a real WebSocket,
#   and reads the results back through the hub's own HTTP API. Four scenarios:
#     1. gesture_source "hub"  : our C++ rules act; Johnny's engine is only compared
#     2. gesture_source "edge" : Johnny's engine acts (our rules still supply "home")
#     3. "edge" with the agent's motion engine switched off: the hub falls back to its own rules
#     4. recording + replay    : a recorded session replayed into a fresh hub gives the SAME engine results
#   Plus "SurfaceCalibration": the automatic camera -> projected-surface calibration through the real hub and the
#   real `ips.py calibrate auto` command (camera frames are synthetic pictures of the projected pattern).
#   Plus a "Hardening" group that attacks the hub: URLs that try to leave the web folder, sensitive calls from another
#   machine, malformed messages, and a stuck display connection (each of these was a real bug found in review).
#   Each scenario plays the scripted 12-second routine (2 clicks, a swipe, an open-palm hold, a two-hand zoom).
#   Run:  python ips.py test --e2e      (takes about a minute)
# ---------------------------------------------------------------------------
import glob
import http.client
import json
import socket
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ips  # noqa: E402  (for ips.EXE and the toolchain environment)

# Which hub program to test (set IPS_HUB_EXE to test an older build, e.g. to prove a new test catches an old bug).
EXE = os.environ.get("IPS_HUB_EXE", ips.EXE)
PORT = 8099
RUN_SECONDS = 13  # one full 12-second routine plus a second of the next one


class HubProcess:
    """Starts the real hub with a throw-away config and storage folder; stops it (flushing recordings) on exit."""

    def __init__(self, gesture_source, bind="127.0.0.1", env=None):
        self.env = env or {}
        self.dir = tempfile.mkdtemp(prefix="ips_e2e_")
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            text = "\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("//"))
        cfg = json.loads(text)
        cfg.update(port=PORT, ui_dir=os.path.join(ROOT, "ui"), gesture_source=gesture_source,
                   db_path=os.path.join(self.dir, "ips.db"), blob_uri="file:" + os.path.join(self.dir, "blobs").replace("\\", "/"),
                   calib_path=os.path.join(self.dir, "calibration.json"), bind=bind)
        self.cfg_path = os.path.join(self.dir, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.log = open(os.path.join(self.dir, "hub.log"), "wb")

    def __enter__(self):
        self.proc = subprocess.Popen([EXE, "--config", self.cfg_path], cwd=ROOT, env=dict(ips.tool_env(), **self.env),
                                     stdout=self.log, stderr=self.log)
        for _ in range(100):
            if self.try_get("/api/status") is not None:
                return self
            time.sleep(0.1)
        raise RuntimeError("hub did not start; log:\n" + open(os.path.join(self.dir, "hub.log"), errors="replace").read())

    def __exit__(self, *exc):
        try:
            self.post("/api/shutdown")
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def try_get(self, path):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=2) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None

    def post(self, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=json.dumps(body or {}).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())

    def status(self):
        return self.try_get("/api/status")

    def agent(self, *args, seconds=RUN_SECONDS):
        cmd = [sys.executable, os.path.join(ROOT, "edge", "agent.py"), "--hub", "127.0.0.1:%d" % PORT,
               "--source", "synthetic", "--seconds", str(seconds), *args]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 60)
        assert out.returncode == 0, "agent failed:\n" + out.stdout + out.stderr
        time.sleep(0.6)  # let the hub finish the last frames
        return out

    def recordings(self):
        return glob.glob(os.path.join(self.dir, "blobs", "sessions", "*", "events.ndjson"))


@unittest.skipUnless(os.path.exists(EXE), "build the hub first: python ips.py build")
class EndToEnd(unittest.TestCase):
    def test_1_hub_mode_our_rules_act_and_johnnys_engine_is_compared(self):
        with HubProcess("hub") as hub:
            hub.agent("--two-hands")
            s = hub.status()
        acted, cmp_ = s["gesture_counts"], s["compare"]
        self.assertEqual("hub", s["gesture_source"])
        self.assertEqual((2, 2, 1, 1), (acted["down"], acted["up"], acted["swipe"], acted["home"]), acted)
        self.assertNotIn("zoom", acted)  # his zoom is NOT acted on in hub mode ...
        self.assertGreaterEqual(cmp_["edge"]["zoom"], 1)  # ... but it is detected and counted
        self.assertEqual((2, 2), (cmp_["edge"]["down"], cmp_["edge"]["up"]))
        self.assertEqual((2, 2, 1, 1), (cmp_["hub"]["down"], cmp_["hub"]["up"], cmp_["hub"]["swipe"], cmp_["hub"]["home"]))
        self.assertNotIn("home", cmp_["edge"])  # his engine has no home gesture
        self.assertGreater(s["motion_frames"], 300)
        self.assertEqual(0, s["dropped"])
        self.assertEqual(0, s["bad_messages"])

    def test_2_edge_mode_johnnys_engine_acts(self):
        with HubProcess("edge") as hub:
            hub.agent("--two-hands")
            s = hub.status()
        acted = s["gesture_counts"]
        self.assertEqual("edge", s["gesture_source"])
        self.assertEqual((2, 2), (acted["down"], acted["up"]))
        self.assertGreaterEqual(acted["swipe"], 1)
        self.assertGreaterEqual(acted["zoom"], 1)  # two-hand zoom now reaches the display
        self.assertEqual(1, acted["home"])         # still provided by our rules
        self.assertEqual(s["compare"]["edge"]["zoom"], acted["zoom"])
        self.assertEqual(0, s["dropped"])

    def test_3_edge_mode_falls_back_when_the_agent_sends_no_motion(self):
        with HubProcess("edge") as hub:
            hub.agent("--no-motion")
            s = hub.status()
        acted = s["gesture_counts"]
        self.assertEqual(0, s["motion_frames"])
        self.assertEqual((2, 2, 1, 1), (acted["down"], acted["up"], acted["swipe"], acted["home"]), acted)

    def test_4_a_recorded_session_replays_to_the_same_engine_results(self):
        with HubProcess("hub") as first:
            first.agent("--two-hands")
            before = first.status()["compare"]
            files = first.recordings()
            self.assertEqual(1, len(files), "expected one recording")
            self.assertGreater(os.path.getsize(files[0]), 50_000)
            recording = os.path.join(tempfile.gettempdir(), "ips_e2e_recording.ndjson")
            time.sleep(0.3)
            shutil.copy(files[0], recording)  # copy before the hub shuts down and deletes its temp folder
            with open(recording, encoding="utf-8") as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
        self.addCleanup(lambda: os.path.exists(recording) and os.remove(recording))
        hands = [m for m in lines if m.get("t") == "hands"]
        self.assertGreater(len(hands), 350)
        self.assertTrue(all("motion" in m for m in hands), "recordings must keep the motion data too")
        # replay into a fresh hub in EDGE mode: the engines' results must not depend on live timing
        with HubProcess("edge") as second:
            out = subprocess.run([sys.executable, os.path.join(ROOT, "edge", "agent.py"), "--hub", "127.0.0.1:%d" % PORT,
                                  "--source", "replay", "--replay", recording, "--max-hands", "2"],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(0, out.returncode, out.stdout + out.stderr)
            time.sleep(0.6)
            after = second.status()["compare"]
        self.assertEqual(before["edge"], after["edge"], "Johnny's engine gave different results on replay")
        self.assertEqual(before["hub"], after["hub"], "our rules gave different results on replay")

    def test_5_recordings_and_sessions_are_catalogued(self):
        with HubProcess("hub") as hub:
            hub.agent(seconds=3)
            time.sleep(0.5)
            sessions = hub.try_get("/api/sessions")
            assets = hub.try_get("/api/assets?kind=events_ndjson")
        self.assertEqual(1, len(sessions))
        self.assertEqual("synthetic", sessions[0]["source"])
        self.assertEqual(1, len(assets))
        self.assertGreater(assets[0]["bytes"], 0)


# ---- helpers for the hardening tests ---------------------------------------------------------------------

def http_request(host, method, path, body=None, headers=None, timeout=5):
    """One raw HTTP request with the path sent EXACTLY as given (urllib would tidy it up). Returns (status, text)."""
    conn = http.client.HTTPConnection(host, PORT, timeout=timeout)
    try:
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers or {})
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        conn.close()


def lan_address():
    """This computer's address on the network (not 127.x), or None when there is none."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        addr = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if addr.startswith("127.") else addr


@unittest.skipUnless(os.path.exists(EXE), "build the hub first: python ips.py build")
class Hardening(unittest.TestCase):
    def test_h1_a_url_can_never_leave_the_web_folder(self):
        secret_file = os.path.join(ROOT, "config.json").replace("\\", "/")  # a file OUTSIDE ui/ with recognizable content
        no_drive = secret_file[2:] if secret_file[1:2] == ":" else secret_file
        attacks = ["/ui/" + secret_file, "/ui//" + secret_file, "/ui//" + no_drive, "/apps//" + no_drive,
                   "/apps///" + no_drive, "/ui/../config.json", "/ui/a/../../config.json", "/ui/%2e%2e/config.json",
                   "/ui/..%2fconfig.json", "/ui/..\\config.json", "/ui/\\..\\config.json", "/apps/../../config.json",
                   "/ui/C:/Windows/win.ini", "/ui/./../config.json"]
        with HubProcess("hub") as hub:
            for url in attacks:
                with self.subTest(url=url):
                    status, text = http_request("127.0.0.1", "GET", url)
                    self.assertIn(status, (400, 403, 404), "got %d for %s" % (status, url))
                    self.assertNotIn("gesture_source", text, "the hub leaked a file outside ui/ for " + url)
            # ... while the real pages still work
            for good in ("/ui/display.html", "/apps/launcher.html", "/apps/snake.html", "/status", "/ui/status.html"):
                status, text = http_request("127.0.0.1", "GET", good)
                self.assertEqual(200, status, good)
                self.assertIn("<", text)

    def test_h2_the_default_setup_only_listens_on_this_computer(self):
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            cfg = json.loads("\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("//")))
        self.assertEqual("127.0.0.1", cfg["bind"], "config.json must not listen on the network by default")
        lan = lan_address()
        if not lan:
            self.skipTest("no network address on this computer")
        with HubProcess("hub") as hub:  # started with the repository's default bind (127.0.0.1)
            with self.assertRaises(OSError):
                http_request(lan, "GET", "/api/status", timeout=3)

    def test_h3_sensitive_calls_from_another_machine_need_the_token(self):
        lan = lan_address()
        if not lan:
            self.skipTest("no network address on this computer")
        with HubProcess("hub", bind="0.0.0.0", env={"IPS_TOKEN": "tok-123"}) as hub:
            # open to anyone: status, app list, and what the projected page itself needs
            self.assertEqual(200, http_request(lan, "GET", "/api/status")[0])
            self.assertEqual(200, http_request(lan, "GET", "/api/apps")[0])
            self.assertEqual(200, http_request(lan, "POST", "/api/display_size", {"w": 1920, "h": 1080})[0])
            self.assertEqual(200, http_request(lan, "POST", "/api/launch", {"app": "snake"})[0])
            self.assertEqual(200, http_request(lan, "POST", "/api/home", {})[0])
            # sensitive: refused from another machine
            for method, path, body in [("POST", "/api/shutdown", {}), ("POST", "/api/ai", {"prompt": "hi"}),
                                       ("POST", "/api/launch", {"url": "http://evil.example/"}),
                                       ("POST", "/api/calibrate/start", {}), ("POST", "/api/calibrate/reset", {}),
                                       ("POST", "/api/brightness", {"value": 0.1}), ("GET", "/api/sessions", None),
                                       ("GET", "/api/assets", None)]:
                with self.subTest(call=method + " " + path):
                    self.assertEqual(403, http_request(lan, method, path, body)[0])
                    self.assertEqual(403, http_request(lan, method, path, body, {"X-IPS-Token": "wrong"})[0])
            self.assertEqual(200, http_request("127.0.0.1", "GET", "/api/status")[0], "the refused shutdown must not stop the hub")
            # with the token it works
            self.assertEqual(200, http_request(lan, "GET", "/api/sessions", None, {"X-IPS-Token": "tok-123"})[0])
            # from this computer no token is needed (the ips.py tool, the Pi's own browser)
            status, text = http_request("127.0.0.1", "POST", "/api/ai", {"prompt": "hi"})
            self.assertNotEqual(403, status)

    def test_h4_without_a_token_nothing_sensitive_is_allowed_remotely(self):
        lan = lan_address()
        if not lan:
            self.skipTest("no network address on this computer")
        with HubProcess("hub", bind="0.0.0.0") as hub:
            self.assertEqual(403, http_request(lan, "POST", "/api/shutdown", {}, {"X-IPS-Token": ""})[0])
            self.assertEqual(403, http_request(lan, "POST", "/api/shutdown", {}, {"X-IPS-Token": "anything"})[0])
            self.assertEqual(200, http_request("127.0.0.1", "GET", "/api/status")[0])

    def test_h5_malformed_messages_do_not_kill_the_agent_connection(self):
        sys.path.insert(0, os.path.join(ROOT, "edge"))
        from wsclient import WS
        with HubProcess("hub") as hub:
            ws = WS("127.0.0.1", PORT, "/edge")
            ws.send_text(json.dumps({"t": "hello", "device": "bad-message-test", "source": "test"}))
            time.sleep(0.4)
            self.assertTrue(hub.status()["edge_connected"])
            for bad in (json.dumps({"t": 5}), json.dumps({"t": ["x"]}), "this is not json", json.dumps([1, 2, 3]),
                        json.dumps({"t": "hands", "cam": "zero", "seq": "x", "hands": 7}), json.dumps({"t": "ping", "ts": "soon"})):
                ws.send_text(bad)
            ws.send_binary(b"\x00")  # too short to be a frame
            ws.send_text(json.dumps({"t": "hands", "cam": 0, "seq": 1, "ts": 1, "track_ms": 1.0, "hands": []}))  # a good one
            time.sleep(0.6)
            s = hub.status()
            self.assertEqual(1, s["cameras"]["0"]["frames"], "the connection must still deliver good frames after bad ones")
            self.assertGreaterEqual(s["bad_messages"], 4)
            self.assertTrue(s["edge_connected"])
            ws.close()
            time.sleep(0.8)
            s = hub.status()
            self.assertFalse(s["edge_connected"], "the hub must notice when the agent is gone")
            self.assertEqual(0, s["session"])
            sessions = hub.try_get("/api/sessions")
            self.assertTrue(sessions[0]["ended_utc"], "the recording session must be closed")

    def test_h6_a_stuck_display_cannot_freeze_the_hub(self):
        sys.path.insert(0, os.path.join(ROOT, "edge"))
        from wsclient import WS
        import threading
        with HubProcess("hub") as hub:
            stuck = WS("127.0.0.1", PORT, "/display")  # connects, then never reads (a frozen browser tab)
            time.sleep(0.3)
            big_url = "/apps/launcher.html?" + "x" * 900_000  # each launch broadcasts a ~0.9 MB message to every display

            def flood():
                for _ in range(60):
                    try:
                        hub.post("/api/launch", {"app": big_url})
                    except Exception:
                        return
            threading.Thread(target=flood, daemon=True).start()
            time.sleep(5)
            t0 = time.time()
            status = hub.try_get("/api/status")
            self.assertIsNotNone(status, "the hub stopped answering while one display was stuck")
            self.assertLess(time.time() - t0, 2.0)
            # a healthy display connected now still gets the current content
            healthy = WS("127.0.0.1", PORT, "/display")
            first = json.loads(healthy.recv_text())
            self.assertEqual("content", first["t"])
            healthy.close()
            stuck.close()

    def test_h7_fps_is_measured_over_recent_frames_only(self):
        with HubProcess("hub") as hub:
            hub.agent(seconds=2)       # a first run, then a pause
            time.sleep(3)
            cmd = [sys.executable, os.path.join(ROOT, "edge", "agent.py"), "--hub", "127.0.0.1:%d" % PORT,
                   "--source", "synthetic", "--seconds", "6"]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(3.5)
                fps = hub.status()["fps_total"]
            finally:
                proc.wait(timeout=30)
            self.assertTrue(25 <= fps <= 35, "expected about 30 frames/s, the status page said %.1f" % fps)


# Messages a connected display page receives, collected on a background thread.
class DisplayListener:
    def __init__(self):
        sys.path.insert(0, os.path.join(ROOT, "edge"))
        from wsclient import WS
        self.ws = WS("127.0.0.1", PORT, "/display")
        self.msgs = []
        import threading
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                self.msgs.append(json.loads(self.ws.recv_text()))
        except Exception:
            pass

    def of(self, t):
        return [m for m in self.msgs if m.get("t") == t]

    def close(self):
        self.ws.close()


def have_cv():
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(os.path.exists(EXE), "build the hub first: python ips.py build")
class SurfaceCalibration(unittest.TestCase):
    def calibrate_cli(self, timeout=15):
        cmd = [sys.executable, os.path.join(ROOT, "ips.py"), "calibrate", "auto", "--timeout", str(timeout)]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30,
                              env=dict(os.environ, IPS_URL="http://127.0.0.1:%d" % PORT))

    @unittest.skipUnless(have_cv(), "needs OpenCV + numpy")
    def test_c1_auto_calibration_maps_the_fingertip_onto_the_projected_surface(self):
        import random
        import threading
        import types
        sys.path.insert(0, os.path.join(ROOT, "edge"))
        sys.path.insert(0, os.path.join(ROOT, "edge", "tests"))
        import agent
        import surface_calib as sc
        import test_surface_calib as pics  # synthetic camera pictures of the projected pattern, with the true mapping
        with HubProcess("hub") as hub:
            display = DisplayListener()
            # The real agent connection and calibrator; frames come from a synthetic camera instead of the Pi camera.
            args = types.SimpleNamespace(hub="127.0.0.1:%d" % PORT, device="e2e-cam", source="video", cam_list=[0],
                                         os_pointer=False)
            args.calib = sc.AutoCalibrator(lambda m: None, [0], settle_s=0.3, want_frames=5, timeout_s=10)
            link = agent.Link(args)
            frame = pics.camera_image()
            target = {"tip": None}  # where the fake fingertip is (normalized camera coordinates), None = no hand
            stop = threading.Event()

            def camera_loop():
                rng, seq = random.Random(1), 0
                while not stop.is_set():
                    agent.feed_calibration(args, 0, frame, False)
                    hands = []
                    if target["tip"]:
                        hands = [{"right": True, "score": 0.95,
                                  "pts": agent.make_hand(target["tip"][0], target["tip"][1], "point", 0.0, rng, 0.0)}]
                    link.send(agent.hands_message(0, seq, int(time.time() * 1e6), 5.0, hands))
                    seq += 1
                    time.sleep(1 / 30)
            cam = threading.Thread(target=camera_loop, daemon=True)
            cam.start()
            try:
                time.sleep(0.5)
                out = self.calibrate_cli()
                self.assertEqual(0, out.returncode, out.stdout + out.stderr)
                self.assertIn("camera 0: calibrated (good)", out.stdout)
                s = hub.status()
                self.assertTrue(s["cameras"]["0"]["calibrated"])
                self.assertTrue(os.path.exists(os.path.join(hub.dir, "calibration.json")))
                # The display was shown the pattern, then it was taken down and a result announced.
                shown = [m for m in display.of("calib_pattern") if m.get("show")]
                self.assertEqual(15, len(shown[0]["markers"]))
                self.assertFalse(display.of("calib_pattern")[-1]["show"])
                self.assertEqual(1, display.of("calib")[-1]["cameras_calibrated"])
                # Now point at a spot on the surface: the cursor must land where that spot is on the PROJECTED picture.
                cx, cy = 450, 300  # camera pixels, inside the projected area
                target["tip"] = (cx / pics.CW, cy / pics.CH)
                time.sleep(1.5)
                moves = [m for m in display.of("action") if m.get("kind") == "move"]
                tx, ty = pics.truth(cx, cy)
                x, y = moves[-1]["x"], moves[-1]["y"]
                self.assertLess(abs(x - tx * 1920), 12, (x, y, tx * 1920, ty * 1080))
                self.assertLess(abs(y - ty * 1080), 12, (x, y, tx * 1920, ty * 1080))
            finally:
                stop.set()
                cam.join(timeout=2)
                link.ws.close()
                display.close()

    def test_c2_auto_calibration_explains_why_it_cannot_run(self):
        with HubProcess("hub") as hub:
            out = self.calibrate_cli(timeout=5)
            self.assertNotEqual(0, out.returncode)
            self.assertIn("no camera agent is connected", out.stdout + out.stderr)
            # An agent without camera images (the simulator) answers at once instead of leaving the pattern up.
            cmd = [sys.executable, os.path.join(ROOT, "edge", "agent.py"), "--hub", "127.0.0.1:%d" % PORT,
                   "--source", "synthetic", "--seconds", "8"]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(1.5)
                display = DisplayListener()
                t0 = time.time()
                out = self.calibrate_cli(timeout=10)
                self.assertNotEqual(0, out.returncode)
                self.assertIn("no camera images", out.stdout)
                self.assertLess(time.time() - t0, 5, "the failure must be reported promptly")
                self.assertFalse(hub.status()["auto_calib"]["active"])
                self.assertFalse(display.of("calib_pattern")[-1]["show"])
                display.close()
            finally:
                proc.wait(timeout=30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
