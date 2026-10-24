#!/usr/bin/env python3
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def open_uri(uri):
    u = urlparse(uri)
    if u.scheme == "file":
        path = unquote(u.path)
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return open(path, "rb")
    raise NotImplementedError("add a reader for %s:// here" % u.scheme)


def iter_assets(db, kind=None):
    con = sqlite3.connect(db)
    q = "SELECT id, session_id, kind, key, uri, bytes FROM assets"
    args = ()
    if kind:
        q += " WHERE kind = ?"
        args = (kind,)
    for row in con.execute(q + " ORDER BY id", args):
        yield dict(zip(("id", "session", "kind", "key", "uri", "bytes"), row))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "ips.db"))
    ap.add_argument("--kind")
    a = ap.parse_args()
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
