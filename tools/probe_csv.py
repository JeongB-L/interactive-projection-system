# tools/probe_csv.py
# ---------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   Reads the per-frame CSV that tools/video_probe.py wrote and answers: "is a repeating pinch visible in the
#   numbers, and how deep does it get?" It needs no OpenCV or MediaPipe, so it runs anywhere in a second.
#   The fixed thresholds of the two engines (0.25 / 0.37) are absolute; this tool uses RELATIVE ones taken from
#   the data itself (a quarter of the way up the range counts as closed, three quarters as open again), so it
#   finds a pinch even when the thumb and index never quite touch in the landmarks.
# USAGE:
#   python3 tools/probe_csv.py data/pinch_probe.csv [--col hub_raw]
# ---------------------------------------------------------------------------
import argparse
import csv


# Value at fraction q (0..1) of the sorted list.
def pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


# Median of three neighbouring values, to ignore single-frame spikes.
def smooth(series):
    out = []
    for i, x in enumerate(series):
        win = [s for s in series[max(0, i - 1):i + 2] if s is not None]
        out.append(sorted(win)[len(win) // 2] if win and x is not None else None)
    return out


# Find the dips in a ratio series. A dip starts when the value falls below `close` and ends when it climbs
# above `opn`. Returns a list of (first frame, last frame, lowest value reached).
def find_cycles(series, close, opn):
    cycles, start, low = [], None, None
    for i, r in enumerate(series):
        if r is None:
            continue  # no hand in this frame: keep the state as it is
        if start is None and r < close:
            start, low = i, r
        elif start is not None:
            low = min(low, r)
            if r > opn:
                cycles.append((start, i, low))
                start = None
    return cycles


def main():
    ap = argparse.ArgumentParser(description="Count repeating pinch dips in a video_probe CSV.")
    ap.add_argument("csv")
    ap.add_argument("--col", default="hub_raw", help="which column: hub_raw, johnny_raw, hub_fixed, johnny_fixed")
    ap.add_argument("--fps", type=float, default=30.0)
    args = ap.parse_args()

    with open(args.csv) as f:
        rows = list(csv.DictReader(f))
    series = [float(r[args.col]) if r[args.col] != "" else None for r in rows]
    values = [s for s in series if s is not None]
    if len(values) < 10:
        raise SystemExit("not enough frames with a hand in the file")
    lo, hi = pct(values, .05), pct(values, .95)
    close, opn = lo + 0.25 * (hi - lo), lo + 0.75 * (hi - lo)
    cycles = find_cycles(smooth(series), close, opn)

    print("column %s: %d frames (%.1f s), hand in %d%% of them" % (args.col, len(series), len(series) / args.fps,
                                                                   100 * len(values) // len(series)))
    print("range of the ratio: p5 %.2f ... p95 %.2f  (median %.2f)" % (lo, hi, pct(values, .5)))
    print("relative thresholds: closed below %.2f, open again above %.2f" % (close, opn))
    print("repeating pinch dips found: %d" % len(cycles))
    if cycles:
        lows = [c[2] for c in cycles]
        lens = [(c[1] - c[0]) / args.fps for c in cycles]
        gaps = [(cycles[i + 1][0] - cycles[i][0]) / args.fps for i in range(len(cycles) - 1)]
        print("how low each dip gets: best %.2f, median %.2f, worst %.2f" % (min(lows), pct(lows, .5), max(lows)))
        print("how long each dip lasts: median %.2f s" % pct(lens, .5))
        if gaps:
            print("time between dips: median %.2f s" % pct(gaps, .5))
        for name, thr in (("hub 0.25", .25), ("Johnny 0.37", .37), ("0.45", .45), ("0.50", .50)):
            print("   dips whose lowest point is under %-12s: %d of %d" % (name, sum(1 for x in lows if x < thr), len(lows)))


if __name__ == "__main__":
    main()
