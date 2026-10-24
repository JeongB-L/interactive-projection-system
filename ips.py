#!/usr/bin/env python3
# ips.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   The one command-line tool for the whole project (python ips.py <command>).
#   It builds the hub, starts/stops it in the background, starts a fake Pi, and sends
#   control requests to the running hub over its HTTP API (switch app, calibrate, dim, ask the
#   AI, list recordings...). It uses only the Python standard library, so it needs no installs.
#   Run `python ips.py --help` for the command list.
# ---------------------------------------------------------------------------
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

# Paths: the repo folder, data folder, the compiled hub program, files that remember the
# background processes' ids (pid files), and their log files.
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
BUILD = os.path.join(ROOT, "build")
EXE = os.path.join(BUILD, "ipsd.exe" if os.name == "nt" else "ipsd")
HUB_PID = os.path.join(DATA, "hub.pid")
SIM_PID = os.path.join(DATA, "sim.pid")
LOG = os.path.join(DATA, "logs", "hub.log")
SIM_LOG = os.path.join(DATA, "logs", "sim.log")
# Where the hub listens (override with the IPS_URL environment variable).
BASE = os.environ.get("IPS_URL", "http://127.0.0.1:8080")


# ------------------------------------------------------------------ helpers
# Build the environment variables used when launching the hub.
def tool_env():
    """PATH that contains the MSYS2 runtime DLLs the hub needs on Windows."""
    env = dict(os.environ)
    # On Windows the hub needs the MSYS2 runtime DLLs: put that folder first on PATH.
    if os.name == "nt":
        for d in (r"C:\msys64\ucrt64\bin", os.path.dirname(shutil.which("g++") or "")):
            if d and os.path.isdir(d):  # must come FIRST: other toolchains on PATH ship clashing DLLs
                env["PATH"] = d + os.pathsep + env["PATH"]
                break
    return env


# Call the hub's HTTP API: GET when no body is given, POST with JSON otherwise.
# Returns the parsed JSON, or None if the hub cannot be reached.
def api(path, body=None, timeout=5):
    # Build the request.
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    # The hub answered with an error status: still return its JSON explanation if it sent one.
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": "HTTP %d" % e.code}
    # Could not connect at all: the hub is not running.
    except (urllib.error.URLError, ConnectionError, OSError):
        return None


# True if the hub answers a quick status request.
def hub_up():
    return api("/api/status", timeout=1) is not None


# Stop with a friendly message when a command needs the hub but it is not running.
def need_hub():
    if not hub_up():
        sys.exit("hub is not running. Start it with: python ips.py start")


# Read a saved process id from a file (how we find the background hub / simulator).
def read_pid(p):
    try:
        return int(open(p).read().strip())
    except (OSError, ValueError):
        return None


# Is a process with this id still running? (Windows: tasklist; elsewhere: signal 0.)
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


# Force-stop a process (and its children on Windows).
def kill(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
    else:
        os.kill(pid, 15)


# Start a program in the background, detached from this terminal, sending its output to a log file,
# and remember its process id.
def spawn(cmd, log, pidfile):
    os.makedirs(os.path.dirname(log), exist_ok=True)
    out = open(log, "ab")
    # Windows-only flags that detach the child process from this console.
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         creationflags=flags, env=tool_env(), close_fds=True)
    open(pidfile, "w").write(str(p.pid))
    return p.pid


# ------------------------------------------------------------------ commands
# `build`: compile the hub and tests with CMake.
def cmd_build(a):
    # Find CMake (and Ninja, if installed), then configure and compile.
    cmake = shutil.which("cmake", path=tool_env()["PATH"])
    if not cmake:
        sys.exit("cmake not found. See docs/RUNBOOK.md, 'One-time setup'.")
    gen = ["-G", "Ninja"] if shutil.which("ninja", path=tool_env()["PATH"]) else []
    for c in ([cmake, "-S", ROOT, "-B", BUILD] + gen, [cmake, "--build", BUILD]):
        if subprocess.run(c, env=tool_env()).returncode:
            sys.exit("build failed")
    print("built", EXE)


# `start`: launch the hub in the background and wait until it answers.
def cmd_start(a):
    if hub_up():
        return print("hub already running at", BASE)
    if not os.path.exists(EXE):
        print("no build yet, building first...")
        cmd_build(a)
    # Launch the compiled hub with this repo's config.json.
    spawn([EXE, "--config", os.path.join(ROOT, "config.json")], LOG, HUB_PID)
    # Wait up to about 8 seconds for the hub to start answering.
    for _ in range(40):
        if hub_up():
            print("hub running at", BASE)
            print("  display page:", BASE + "/ui/display.html")
            print("  status page :", BASE + "/status")
            return
        time.sleep(0.2)
    sys.exit("hub did not come up; see: python ips.py logs")


# `stop`: stop the simulator, then ask the hub to shut down cleanly (which saves recordings).
def cmd_stop(a):
    # Stop the fake Pi first, if one is running.
    pid = read_pid(SIM_PID)
    if pid and alive(pid):
        kill(pid)
        print("simulator stopped")
    if hub_up():
        # Ask the hub to shut down cleanly, then wait for it to disappear.
        api("/api/shutdown", {})
        for _ in range(25):
            if not hub_up():
                break
            time.sleep(0.2)
        print("hub stopped (recording flushed)")
    # Last resort: force-stop the hub if it did not exit by itself.
    pid = read_pid(HUB_PID)
    if pid and alive(pid):
        kill(pid)
        print("hub force-stopped")
    # Remove the leftover pid files.
    for p in (HUB_PID, SIM_PID):
        if os.path.exists(p):
            os.remove(p)


# `status`: print a readable summary of /api/status (or the raw JSON with --json).
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


# `sim`: start the fake Pi (edge/agent.py with synthetic hands) in the background.
def cmd_sim(a):
    need_hub()
    # Don't start a second simulator if one is already running.
    pid = read_pid(SIM_PID)
    if pid and alive(pid):
        return print("simulator already running (pid %d). `python ips.py stop` ends it." % pid)
    # The simulator is the same agent program a real Pi runs, just with fake hands as its source.
    cmd = [sys.executable, os.path.join(ROOT, "edge", "agent.py"), "--source", "synthetic",
           "--cams", *[str(i) for i in range(a.cams)], "--seconds", str(a.seconds)]
    pid = spawn(cmd, SIM_LOG, SIM_PID)
    print("simulator started (pid %d), %d camera(s), %s" % (pid, a.cams, ("%ss" % a.seconds) if a.seconds else "until `stop`"))


# `open`: open the projected display page (or the status page) in the default browser.
def cmd_open(a):
    need_hub()
    webbrowser.open(BASE + ("/status" if a.page == "status" else "/ui/display.html"))


# `launch`: switch the app shown on the projector.
def cmd_launch(a):
    need_hub()
    print(api("/api/launch", {"app": a.app}))


# `home`: go back to the launcher.
def cmd_home(a):
    need_hub()
    api("/api/home", {})
    print("ok")


# `calibrate`: show the four calibration markers (`calibrate reset` clears the calibration).
def cmd_calibrate(a):
    need_hub()
    api("/api/calibrate/" + ("reset" if a.action == "reset" else "start"), {})
    print("calibration markers shown; point at each yellow dot and pinch" if a.action != "reset" else "calibration cleared")


# `brightness`: dim the projected image (0 = black, 1 = full).
def cmd_brightness(a):
    need_hub()
    api("/api/brightness", {"value": a.value})
    print("ok")


# `ai`: ask the AI through the hub; the API key stays in the hub's .env file.
def cmd_ai(a):
    need_hub()
    r = api("/api/ai", {"prompt": " ".join(a.prompt)}, timeout=60)
    print(r.get("text") if r and r.get("ok") else "AI unavailable: %s" % (r or {}).get("error", "no response"))


# `sessions`: list recorded sessions from the database.
def cmd_sessions(a):
    need_hub()
    for s in api("/api/sessions"):
        print("#%-3d %s -> %s  %s (%s)" % (s["id"], s["started_utc"], s["ended_utc"] or "running", s["device"], s["source"]))


# `assets`: list stored files (video, event logs) from the database index.
def cmd_assets(a):
    need_hub()
    rows = api("/api/assets" + ("?kind=" + a.kind if a.kind else ""))
    for r in rows:
        print("#%-3d s%-3d %-14s %9d B  %s" % (r["id"], r["session_id"], r["kind"], r["bytes"], r["uri"]))
    print(len(rows), "asset(s)")


# `migrate-uri`: rewrite every stored file's URI from its key onto a new base location
# (only needed if the storage location ever moves).
def cmd_migrate(a):
    db = os.path.join(DATA, "ips.db")
    # Open the catalog database directly (no hub needed).
    con = sqlite3.connect(db)
    base = a.new_base.rstrip("/")
    n = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    # --dry-run: just show what would change.
    if a.dry_run:
        for key, in con.execute("SELECT key FROM assets LIMIT 3"):
            print("would set uri =", base + "/" + key)
        return print(n, "row(s) would change")
    # Rebuild each URI as <new base>/<key>; the key (path inside the store) never changes.
    con.execute("UPDATE assets SET uri = ? || '/' || key", (base,))
    con.commit()
    print("%d row(s) now point at %s\nAlso copy the files there and set blob_uri in config.json." % (n, base))


# `bench`: run the simulator for a while and print latency / throughput numbers.
def cmd_bench(a):
    need_hub()
    # Remember the frame counters first so we can report only this run's frames.
    before = api("/api/status")
    print("running %ds, %d camera(s) of synthetic hands..." % (a.seconds, a.cams))
    # Run the fake Pi in the foreground for N seconds.
    subprocess.run([sys.executable, os.path.join(ROOT, "edge", "agent.py"), "--source", "synthetic",
                    "--cams", *[str(i) for i in range(a.cams)], "--seconds", str(a.seconds), "--fps", str(a.fps)],
                   stdout=subprocess.DEVNULL)
    time.sleep(0.5)
    s = api("/api/status")
    # Print the results table.
    frames = sum(c["frames"] for c in s["cameras"].values()) - sum(c["frames"] for c in before["cameras"].values())
    print("\n  frames received        : %d (%.1f/s)" % (frames, frames / a.seconds))
    print("  hub processing ms      : p50 %.3f   p95 %.3f   (parse + fuse + recognize + send)" % (s["hub_ms"]["p50"], s["hub_ms"]["p95"]))
    print("  end-to-end ms          : p50 %.2f   p95 %.2f   (agent timestamp -> action; same machine, so clocks agree)" % (s["e2e_ms"]["p50"], s["e2e_ms"]["p95"]))
    print("  dropped (seq gaps)     : %d" % s["dropped"])
    print("  gestures               : %s" % s["gesture_counts"])
    print("\nThese are loopback numbers with fake hands. Real latency needs the Pi, camera, and projector.")


# `test`: run the C++ unit tests (building first if needed).
def cmd_test(a):
    exe = os.path.join(BUILD, "ips_tests.exe" if os.name == "nt" else "ips_tests")
    if not os.path.exists(exe):
        cmd_build(a)
    sys.exit(subprocess.run([exe], env=tool_env()).returncode)


# `logs`: print the last lines of the hub's log file.
def cmd_logs(a):
    if not os.path.exists(LOG):
        return print("no log yet")
    lines = open(LOG, "rb").read().decode(errors="replace").splitlines()
    print("\n".join(lines[-a.n:]))


# Define every sub-command and its arguments, then run the one that was typed.
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Small helper: register one sub-command together with its arguments.
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
