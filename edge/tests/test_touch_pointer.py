"""
edge/tests/test_touch_pointer.py
WHAT THIS FILE DOES
  Checks TouchPointer (agent --touch-device): hub actions become virtual_touchscreen commands.
  A plain temporary file stands in for /dev/virtual_touchscreen.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import TouchPointer  # noqa: E402


class TouchPointerTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        self.tp = TouchPointer(self.path, 1920, 1080)

    def tearDown(self):
        os.close(self.tp.fd)
        os.remove(self.path)

    def lines(self):
        with open(self.path) as f:
            return f.read().splitlines()

    def test_hover_moves_send_nothing(self):
        self.tp.apply({"kind": "move", "x": 100, "y": 100})
        self.assertEqual([], self.lines())

    def test_a_pinch_is_one_touch_at_the_scaled_position(self):
        self.tp.apply({"kind": "down", "x": 1919, "y": 0})
        self.tp.apply({"kind": "up", "x": 1919, "y": 0})
        out = self.lines()
        self.assertIn("X 1024", out)
        self.assertIn("Y 0", out)
        self.assertEqual(1, out.count("d 0"))
        self.assertEqual(1, out.count("u 0"))
        self.assertLess(out.index("X 1024"), out.index("d 0"))   # position is set before the finger lands
        self.assertEqual("S 0", out[-1])                          # every block ends with a sync

    def test_drag_moves_while_down_and_hide_releases(self):
        self.tp.apply({"kind": "down", "x": 0, "y": 0})
        self.tp.apply({"kind": "move", "x": 960, "y": 540})
        self.tp.apply({"kind": "hide"})
        out = self.lines()
        self.assertIn("x 512", out)
        self.assertEqual(1, out.count("u 0"))
        self.tp.apply({"kind": "up", "x": 0, "y": 0})          # already released: nothing more
        self.assertEqual(out, self.lines())


if __name__ == "__main__":
    unittest.main()
