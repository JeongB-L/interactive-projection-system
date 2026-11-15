# edge/tests/test_vendored.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE TESTS
#   That Johnny's code in edge/pi_hand_tracking_v2/ is still EXACTLY what he uploaded (byte for byte).
#   The integration must never edit his files; it wraps them (edge/motion_bridge.py). If Johnny deliberately
#   updates his code, regenerate the fingerprints with:   python edge/tests/test_vendored.py --update
#   (and commit the new vendored_hashes.json together with his new files).
# ---------------------------------------------------------------------------
import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.join(os.path.dirname(HERE), "pi_hand_tracking_v2")
MANIFEST = os.path.join(HERE, "vendored_hashes.json")
# Files that belong to Johnny's upload (the FROM_JOHNNY.md note is ours and is not fingerprinted).
JOHNNY_FILES = ["README.md", "download_model.py", "main.py", "requirements.txt", "tracker.py",
                "models/README.txt", "tests/test_tracker.py"]


# Fingerprint of a file's content. Line endings are normalized first, so a checkout that changed LF to CRLF (Git on
# Windows can do that) does not look like an edit.
def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


class VendoredFilesUnchanged(unittest.TestCase):
    def setUp(self):
        with open(MANIFEST, encoding="utf-8") as f:
            self.expected = json.load(f)

    def test_manifest_covers_every_file(self):
        self.assertEqual(sorted(JOHNNY_FILES), sorted(self.expected))

    def test_each_file_matches_its_fingerprint(self):
        for name in JOHNNY_FILES:
            with self.subTest(file=name):
                self.assertEqual(
                    self.expected[name], sha(os.path.join(V2, name)),
                    "%s was modified. Do not edit Johnny's files: change edge/motion_bridge.py instead. "
                    "If Johnny intentionally updated it, run: python edge/tests/test_vendored.py --update" % name)

    def test_no_unexpected_python_files_were_added_to_his_folder(self):
        found = []
        for root, _, files in os.walk(V2):
            for fn in files:
                if fn.endswith(".py"):
                    found.append(os.path.relpath(os.path.join(root, fn), V2).replace("\\", "/"))
        self.assertEqual(sorted(f for f in JOHNNY_FILES if f.endswith(".py")), sorted(found))

    def test_attribution_note_is_present(self):
        self.assertTrue(os.path.isfile(os.path.join(V2, "FROM_JOHNNY.md")))


if __name__ == "__main__":
    if "--update" in sys.argv:
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump({n: sha(os.path.join(V2, n)) for n in JOHNNY_FILES}, f, indent=2, sort_keys=True)
        print("updated", MANIFEST)
    else:
        unittest.main()
