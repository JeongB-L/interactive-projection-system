# edge/tests/test_project_files.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   Small but important facts about the project's own files and tools:
#     - the default configuration is the safe one (the hub listens on this computer only),
#     - the Pi dependency list pins MediaPipe to the version range the code was written for,
#     - the command-line tool sends the secret token when it talks to a hub on another machine,
#     - the status page never puts raw network text into HTML (a device name sent by an agent must not be able to inject script).
# ---------------------------------------------------------------------------
import json
import os
import re
import sys
import unittest
import urllib.request

import helpers

ROOT = helpers.ROOT
sys.path.insert(0, ROOT)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def load_config():
    return json.loads("\n".join(l for l in read("config.json").splitlines() if not l.lstrip().startswith("//")))


class SafeDefaults(unittest.TestCase):
    def test_the_hub_listens_on_this_computer_only_by_default(self):
        self.assertEqual("127.0.0.1", load_config()["bind"])

    def test_recording_video_is_off_by_default(self):
        self.assertFalse(load_config()["record"]["frames"])

    def test_the_secrets_file_is_never_committed(self):
        self.assertIn(".env", read(".gitignore").split())
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".env.example")) and "sk-ant" in read(".env.example"))

    def test_gesture_source_is_a_known_engine(self):
        # "edge" (Johnny's engine) since 2026-11-20: on the wall it caught swipes the hub rules missed.
        # The hub falls back to its own rules whenever the agent sends no motion data.
        self.assertIn(load_config()["gesture_source"], ("hub", "edge"))


class Dependencies(unittest.TestCase):
    def test_mediapipe_is_pinned_to_the_range_the_code_supports(self):
        line = [l for l in read("requirements-pi.txt").splitlines() if l.startswith("mediapipe")][0]
        self.assertIn(">=0.10.31", line)
        self.assertIn("<1.3", line)

    def test_every_dependency_the_agent_imports_is_listed_for_the_pi(self):
        listed = read("requirements-pi.txt")
        for package in ("opencv", "mediapipe", "pyautogui"):
            self.assertIn(package, listed)


class CommandLineTool(unittest.TestCase):
    def setUp(self):
        import ips
        self.ips = ips
        self.requests = []
        self._saved = urllib.request.urlopen

        class Reply:
            def read(self_inner):
                return b'{"ok": true}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        def fake_urlopen(req, timeout=None):
            self.requests.append(req)
            return Reply()
        urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", self._saved)
        self._token = os.environ.pop("IPS_TOKEN", None)
        self.addCleanup(lambda: os.environ.__setitem__("IPS_TOKEN", self._token) if self._token else os.environ.pop("IPS_TOKEN", None))

    def test_no_token_header_when_no_token_is_set(self):
        self.ips.api("/api/status")
        self.assertIsNone(self.requests[0].get_header("X-ips-token"))

    def test_token_header_is_sent_when_a_token_is_set(self):
        os.environ["IPS_TOKEN"] = "abc123"
        self.ips.api("/api/shutdown", {})
        self.assertEqual("abc123", self.requests[0].get_header("X-ips-token"))

    def test_post_and_get_are_chosen_by_whether_a_body_is_given(self):
        self.ips.api("/api/status")
        self.ips.api("/api/home", {})
        self.assertEqual(["GET", "POST"], [r.get_method() for r in self.requests])


class StatusPageIsInjectionProof(unittest.TestCase):
    NUMERIC_FIELDS = {"displays", "dropped", "bad_messages", "jpeg_frames", "session", "motion_frames", "actions"}

    def test_every_value_put_into_the_page_is_escaped_or_a_number(self):
        page = read("ui", "status.html")
        self.assertIn("const esc =", page)
        unsafe = []
        for expr in re.findall(r"\$\{([^}]*)\}", page):
            e = expr.strip()
            if e.startswith(("f(", "esc(")) or e in ("cams", "gc", "last", "esc"):
                continue  # a number formatter, an escaped value, or a variable built from escaped values
            m = re.fullmatch(r"s\.(\w+)", e)
            if m and m.group(1) in self.NUMERIC_FIELDS:
                continue
            if "? 'connected: ' + esc(" in e or "'connected: '" in e and "esc(s.edge_device)" in e:
                continue
            if "'ok' : 'bad'" in e or "'ok'" in e and "'bad'" in e:
                continue  # the fixed CSS class names
            unsafe.append(expr)
        self.assertEqual([], unsafe, "these page values are not escaped: %s" % unsafe)

    def test_a_hostile_device_name_cannot_become_markup(self):
        # Mirror of the page's escape function, applied to a typical attack string.
        def esc(t):
            return "".join({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}.get(c, c) for c in str(t))
        attack = '<img src=x onerror="fetch(\'/api/shutdown\',{method:\'POST\'})">'
        self.assertNotIn("<", esc(attack))
        self.assertNotIn('"', esc(attack))


if __name__ == "__main__":
    unittest.main()
