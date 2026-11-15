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
#   Each scenario plays the scripted 12-second routine (2 clicks, a swipe, an open-palm hold, a two-hand zoom).
#   Run:  python ips.py test --e2e      (takes about a minute)
# ---------------------------------------------------------------------------
import glob
import json
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

PORT = 8099
RUN_SECONDS = 13  # one full 12-second routine plus a second of the next one


class HubProcess:
    """Starts the real hub with a throw-away config and storage folder; stops it (flushing recordings) on exit."""

    def __init__(self, gesture_source):
        self.dir = tempfile.mkdtemp(prefix="ips_e2e_")
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            text = "\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("//"))
        cfg = json.loads(text)
        cfg.update(port=PORT, ui_dir=os.path.join(ROOT, "ui"), gesture_source=gesture_source,
                   db_path=os.path.join(self.dir, "ips.db"), blob_uri="file:" + os.path.join(self.dir, "blobs").replace("\\", "/"),
                   calib_path=os.path.join(self.dir, "calibration.json"), bind="127.0.0.1")
        self.cfg_path = os.path.join(self.dir, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.log = open(os.path.join(self.dir, "hub.log"), "wb")

    def __enter__(self):
        self.proc = subprocess.Popen([ips.EXE, "--config", self.cfg_path], cwd=ROOT, env=ips.tool_env(),
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


@unittest.skipUnless(os.path.exists(ips.EXE), "build the hub first: python ips.py build")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
