#!/usr/bin/env python3
# tools/iter_assets.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   A small example of how a machine-learning job would use the stored data: loop over every row
#   of the SQLite catalog, resolve each file's URI, and read the file. The database holds only
#   pointers; this script follows them.
# ---------------------------------------------------------------------------
"""Example: walk every stored file listed in the SQLite catalog (what an ML job would do).

  python tools/iter_assets.py                      # list everything
  python tools/iter_assets.py --kind video_mjpeg   # only camera video

The database stores only the key + URI; this script resolves the URI and reads the bytes.
For a cloud URI (s3://...) replace `open_uri` with your cloud client; nothing else changes.
"""
import argparse
import os
import sqlite3
import sys
from urllib.parse import unquote, urlparse

# The repo folder (the database lives in data/ips.db under it).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Turn a file:// URI into an open file. This is the ONE place to change if the files
# ever live somewhere else (another storage type needs its own reader here).
def open_uri(uri):
    # Split the URI into scheme and path.
    u = urlparse(uri)
    if u.scheme == "file":
        path = unquote(u.path)
        # On Windows a file URI looks like /C:/..., so remove the leading slash.
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return open(path, "rb")
    # Any scheme other than file:// has no reader yet.
    raise NotImplementedError("add a reader for %s:// here" % u.scheme)


# Yield one dict per database row (optionally only one kind): the loop an ML job would use.
def iter_assets(db, kind=None):
    con = sqlite3.connect(db)
    q = "SELECT id, session_id, kind, key, uri, bytes FROM assets"
    args = ()
    # Optional filter, e.g. only camera video.
    if kind:
        q += " WHERE kind = ?"
        args = (kind,)
    for row in con.execute(q + " ORDER BY id", args):
        yield dict(zip(("id", "session", "kind", "key", "uri", "bytes"), row))


# Command-line demo: read every listed file and print its size.
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "ips.db"))
    ap.add_argument("--kind")
    a = ap.parse_args()
    # Count how many files were read successfully.
    total = 0
    for r in iter_assets(a.db, a.kind):
        try:
            with open_uri(r["uri"]) as f:
                n = len(f.read())
            print("#%d session %d %-14s read %d bytes  %s" % (r["id"], r["session"], r["kind"], n, r["key"]))
            total += 1
        except (OSError, NotImplementedError) as e:
            print("#%d unreadable: %s" % (r["id"], e), file=sys.stderr)
    print(total, "asset(s) read")
