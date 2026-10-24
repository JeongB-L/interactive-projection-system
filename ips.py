#!/usr/bin/env python3
"""ips: one small command-line tool to run and poke the Interactive Projection System.

  python ips.py build            compile the hub (needs CMake + a C++ compiler, see docs/RUNBOOK.md)
  python ips.py start            start the hub in the background
  python ips.py stop             stop the simulator and the hub (recording is flushed)
  python ips.py status           show what the hub sees
  python ips.py sim              start a fake 2-camera Pi (--seconds N, --cams N)
  python ips.py open             open the projected display page (and `open status` for the status page)
  python ips.py launch snake     switch app (browser | ai | snake | launcher | any URL)
  python ips.py home             back to the launcher
  python ips.py calibrate        show the 4 calibration markers (`calibrate reset` clears it)
  python ips.py brightness 0.5   dim the projected image (0..1)
  python ips.py ai "question"    ask the AI through the hub (key stays in .env)
  python ips.py sessions         list recorded sessions
  python ips.py assets           list stored files (video, logs) from the database
  python ips.py migrate-uri URI  point every stored file at a new storage location (cloud switch)
  python ips.py bench            run the simulator for N seconds and print a latency table
  python ips.py test             run the unit tests
  python ips.py logs             tail the hub log
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
BUILD = os.path.join(ROOT, "build")
EXE = os.path.join(BUILD, "ipsd.exe" if os.name == "nt" else "ipsd")
HUB_PID = os.path.join(DATA, "hub.pid")
SIM_PID = os.path.join(DATA, "sim.pid")
LOG = os.path.join(DATA, "logs", "hub.log")
SIM_LOG = os.path.join(DATA, "logs", "sim.log")
BASE = os.environ.get("IPS_URL", "http://127.0.0.1:8080")


# ------------------------------------------------------------------ helpers
def tool_env():
    """PATH that contains the MSYS2 runtime DLLs the hub needs on Windows."""
    env = dict(os.environ)
    if os.name == "nt":
        for d in (r"C:\msys64\ucrt64\bin", os.path.dirname(shutil.which("g++") or "")):
            if d and os.path.isdir(d):  # must come FIRST: other toolchains on PATH ship clashing DLLs
                env["PATH"] = d + os.pathsep + env["PATH"]
                break
    return env


def api(path, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": "HTTP %d" % e.code}
    except (urllib.error.URLError, ConnectionError, OSError):
        return None


def hub_up():
    return api("/api/status", timeout=1) is not None


def need_hub():
    if not hub_up():
        sys.exit("hub is not running. Start it with: python ips.py start")


def read_pid(p):
    try:
        return int(open(p).read().strip())
    except (OSError, ValueError):
        return None


def alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
    else:
        os.kill(pid, 15)


def spawn(cmd, log, pidfile):
    os.makedirs(os.path.dirname(log), exist_ok=True)
    out = open(log, "ab")
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         creationflags=flags, env=tool_env(), close_fds=True)
    open(pidfile, "w").write(str(p.pid))
    return p.pid


# ------------------------------------------------------------------ commands
def cmd_build(a):
    cmake = shutil.which("cmake", path=tool_env()["PATH"])
    if not cmake:
        sys.exit("cmake not found. See docs/RUNBOOK.md, 'One-time setup'.")
    gen = ["-G", "Ninja"] if shutil.which("ninja", path=tool_env()["PATH"]) else []
    for c in ([cmake, "-S", ROOT, "-B", BUILD] + gen, [cmake, "--build", BUILD]):
        if subprocess.run(c, env=tool_env()).returncode:
            sys.exit("build failed")
    print("built", EXE)


def cmd_start(a):
    if hub_up():
        return print("hub already running at", BASE)
    if not os.path.exists(EXE):
        print("no build yet, building first...")
        cmd_build(a)
    spawn([EXE, "--config", os.path.join(ROOT, "config.json")], LOG, HUB_PID)
    for _ in range(40):
        if hub_up():
            print("hub running at", BASE)
            print("  display page:", BASE + "/ui/display.html")
            print("  status page :", BASE + "/status")
            return
        time.sleep(0.2)
    sys.exit("hub did not come up; see: python ips.py logs")


def cmd_stop(a):
    pid = read_pid(SIM_PID)
    if pid and alive(pid):
        kill(pid)
        print("simulator stopped")
    if hub_up():
        api("/api/shutdown", {})
        for _ in range(25):
            if not hub_up():
                break
            time.sleep(0.2)
        print("hub stopped (recording flushed)")
    pid = read_pid(HUB_PID)
    if pid and alive(pid):
        kill(pid)
        print("hub force-stopped")
    for p in (HUB_PID, SIM_PID):
        if os.path.exists(p):
            os.remove(p)


def cmd_status(a):
    s = api("/api/status")
    if not s:
        return print("hub: NOT running")
    if a.json:
        return print(json.dumps(s, indent=2))
    print("hub: running, up %.0fs, app=%s, displays=%d" % (s["uptime_s"], s["app"], s["displays"]))
    print("edge:", ("connected (%s), session #%s" % (s["edge_device"], s["session"])) if s["edge_connected"] else "not connected")
    print("fps %.1f | hub ms p50/p95 %.3f/%.3f | e2e ms p50/p95 %.1f/%.1f | track ms avg %.1f | dropped %d"
          % (s["fps_total"], s["hub_ms"]["p50"], s["hub_ms"]["p95"], s["e2e_ms"]["p50"], s["e2e_ms"]["p95"],
             s["track_ms_avg"], s["dropped"]))
    print("cameras:", {c: ("%d frames, calibrated=%s" % (v["frames"], v["calibrated"])) for c, v in s["cameras"].items()})
    print("gestures:", s["gesture_counts"])
    print("storage:", s["blob_base"])


def cmd_sim(a):
    need_hub()
    pid = read_pid(SIM_PID)
    if pid and alive(pid):
        return print("simulator already running (pid %d). `python ips.py stop` ends it." % pid)
    cmd = [sys.executable, os.path.join(ROOT, "edge", "agent.py"), "--source", "synthetic",
           "--cams", *[str(i) for i in range(a.cams)], "--seconds", str(a.seconds)]
    pid = spawn(cmd, SIM_LOG, SIM_PID)
    print("simulator started (pid %d), %d camera(s), %s" % (pid, a.cams, ("%ss" % a.seconds) if a.seconds else "until `stop`"))


def cmd_open(a):
    need_hub()
    webbrowser.open(BASE + ("/status" if a.page == "status" else "/ui/display.html"))


def cmd_launch(a):
    need_hub()
    print(api("/api/launch", {"app": a.app}))


def cmd_home(a):
    need_hub()
    api("/api/home", {})
    print("ok")


def cmd_calibrate(a):
    need_hub()
    api("/api/calibrate/" + ("reset" if a.action == "reset" else "start"), {})
    print("calibration markers shown; point at each yellow dot and pinch" if a.action != "reset" else "calibration cleared")


def cmd_brightness(a):
    need_hub()
    api("/api/brightness", {"value": a.value})
    print("ok")


def cmd_ai(a):
    need_hub()
    r = api("/api/ai", {"prompt": " ".join(a.prompt)}, timeout=60)
    print(r.get("text") if r and r.get("ok") else "AI unavailable: %s" % (r or {}).get("error", "no response"))


def cmd_sessions(a):
    need_hub()
    for s in api("/api/sessions"):
        print("#%-3d %s -> %s  %s (%s)" % (s["id"], s["started_utc"], s["ended_utc"] or "running", s["device"], s["source"]))


def cmd_assets(a):
    need_hub()
    rows = api("/api/assets" + ("?kind=" + a.kind if a.kind else ""))
    for r in rows:
        print("#%-3d s%-3d %-14s %9d B  %s" % (r["id"], r["session_id"], r["kind"], r["bytes"], r["uri"]))
    print(len(rows), "asset(s)")


def cmd_migrate(a):
    db = os.path.join(DATA, "ips.db")
    con = sqlite3.connect(db)
    base = a.new_base.rstrip("/")
    n = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    if a.dry_run:
        for key, in con.execute("SELECT key FROM assets LIMIT 3"):
            print("would set uri =", base + "/" + key)
        return print(n, "row(s) would change")
    con.execute("UPDATE assets SET uri = ? || '/' || key", (base,))
    con.commit()
    print("%d row(s) now point at %s\nAlso copy the files there and set blob_uri in config.json." % (n, base))


def cmd_bench(a):
    need_hub()
    before = api("/api/status")
    print("running %ds, %d camera(s) of synthetic hands..." % (a.seconds, a.cams))
    subprocess.run([sys.executable, os.path.join(ROOT, "edge", "agent.py"), "--source", "synthetic",
                    "--cams", *[str(i) for i in range(a.cams)], "--seconds", str(a.seconds), "--fps", str(a.fps)],
                   stdout=subprocess.DEVNULL)
    time.sleep(0.5)
    s = api("/api/status")
    frames = sum(c["frames"] for c in s["cameras"].values()) - sum(c["frames"] for c in before["cameras"].values())
    print("\n  frames received        : %d (%.1f/s)" % (frames, frames / a.seconds))
    print("  hub processing ms      : p50 %.3f   p95 %.3f   (parse + fuse + recognize + send)" % (s["hub_ms"]["p50"], s["hub_ms"]["p95"]))
    print("  end-to-end ms          : p50 %.2f   p95 %.2f   (agent timestamp -> action; same machine, so clocks agree)" % (s["e2e_ms"]["p50"], s["e2e_ms"]["p95"]))
    print("  dropped (seq gaps)     : %d" % s["dropped"])
    print("  gestures               : %s" % s["gesture_counts"])
    print("\nThese are loopback numbers with fake hands. Real latency needs the Pi, camera, and projector.")


def cmd_test(a):
    exe = os.path.join(BUILD, "ips_tests.exe" if os.name == "nt" else "ips_tests")
    if not os.path.exists(exe):
        cmd_build(a)
    sys.exit(subprocess.run([exe], env=tool_env()).returncode)


def cmd_logs(a):
    if not os.path.exists(LOG):
        return print("no log yet")
    lines = open(LOG, "rb").read().decode(errors="replace").splitlines()
    print("\n".join(lines[-a.n:]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args):
        p = sub.add_parser(name)
        for a_, kw in args:
            p.add_argument(a_, **kw)
        p.set_defaults(fn=fn)
        return p

    add("build", cmd_build)
    add("start", cmd_start)
    add("stop", cmd_stop)
    add("status", cmd_status, ("--json", dict(action="store_true")))
    add("sim", cmd_sim, ("--seconds", dict(type=float, default=0)), ("--cams", dict(type=int, default=2)))
    add("open", cmd_open, ("page", dict(nargs="?", default="display", choices=["display", "status"])))
    add("launch", cmd_launch, ("app", dict()))
    add("home", cmd_home)
    add("calibrate", cmd_calibrate, ("action", dict(nargs="?", default="start", choices=["start", "reset"])))
    add("brightness", cmd_brightness, ("value", dict(type=float)))
    add("ai", cmd_ai, ("prompt", dict(nargs="+")))
    add("sessions", cmd_sessions)
    add("assets", cmd_assets, ("--kind", dict(default="")))
    add("migrate-uri", cmd_migrate, ("new_base", dict()), ("--dry-run", dict(action="store_true")))
    add("bench", cmd_bench, ("--seconds", dict(type=int, default=15)), ("--cams", dict(type=int, default=2)),
        ("--fps", dict(type=float, default=30)))
    add("test", cmd_test)
    add("logs", cmd_logs, ("-n", dict(type=int, default=30)))
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
