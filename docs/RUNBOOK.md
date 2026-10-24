# Runbook

Everything is driven by one command: `python ips.py <command>`. Run it from the repo folder. Python 3.8+ is enough (standard library only).

## One-time setup (Windows, already done on Jeongbin's laptop)

1. Install MSYS2 and, in the "MSYS2 UCRT64" shell:
   ```bash
   pacman -S --needed mingw-w64-ucrt-x86_64-{gcc,cmake,ninja,boost,openssl,sqlite3,nlohmann-json}
   ```
2. Build: `python ips.py build`
3. (Optional, for the AI app) `copy .env.example .env` and put your key after `ANTHROPIC_API_KEY=`.

Linux / Raspberry Pi: `sudo apt install build-essential cmake ninja-build libboost-dev libssl-dev libsqlite3-dev nlohmann-json3-dev`, then `python3 ips.py build`.

## Turn it on

```bash
python ips.py start          # hub runs in the background
python ips.py open           # opens the projected display page in your browser
python ips.py sim            # a fake 2-camera Pi, so you can see it work with no hardware
python ips.py status         # fps, latency, gestures
```
Watch the page: a red dot follows the fake fingertip, pinches click, and a swipe moves Snake. `python ips.py open status` shows the same numbers live.

## Turn it off

```bash
python ips.py stop           # stops the simulator and the hub; the recording is saved first
```

## Things you can do

| Command | What it does |
|---|---|
| `python ips.py launch snake` | switch app (`browser`, `ai`, `snake`, `launcher`, or any URL) |
| `python ips.py home` | back to the launcher |
| `python ips.py calibrate` | show 4 yellow markers; point at each and pinch. `calibrate reset` clears it |
| `python ips.py brightness 0.4` | dim the projected image (test tracking under less light) |
| `python ips.py ai "explain pinch gestures"` | ask the AI through the hub |
| `python ips.py sessions` | recorded sessions |
| `python ips.py assets` | files stored (video, logs); `--kind video_mjpeg` to filter |
| `python tools/iter_assets.py` | example loop that reads every stored file via the database |
| `python ips.py bench --seconds 15` | latency and throughput table |
| `python ips.py test` | unit tests |
| `python ips.py logs` | last lines of the hub log |

## Real Pi (when hardware is ready)

On the Pi: `python3 -m pip install opencv-python mediapipe` (and `picamera2` for the CSI cameras), then
```bash
python3 edge/agent.py --hub <laptop-ip>:8080 --source picamera --cams 0 1 --os-pointer
```
Use `--source camera` for USB webcams. `--send-frames` also uploads video (the hub only stores it if `record.frames` is `true` in `config.json`). On the Pi, open `http://<laptop-ip>:8080/ui/display.html` fullscreen in Chromium (kiosk mode) on the projector output. The Pi code paths are written but have **not been run on real hardware yet**.

## Where things live

| What | Where |
|---|---|
| Settings (thresholds, apps, storage) | `config.json` |
| Secrets (AI key, token) | `.env` (never committed) |
| Database (index only) | `data/ips.db` |
| Video and recordings (the actual bytes) | `data/blobs/...` |
| Calibration | `data/calibration.json` |
| Hub log | `data/logs/hub.log` |

## Moving storage to the cloud later

The database stores only a key and a URI for each file; the bytes live in the blob store.
1. Copy `data/blobs/*` to the bucket (any tool).
2. `python ips.py migrate-uri s3://my-bucket/ips` rewrites every URI from its key.
3. Set `blob_uri` in `config.json`. (The hub currently implements only `file:` storage; an S3 class implementing `IBlobStore` is the one missing piece.)

## Troubleshooting

- **`hub did not come up`**: `python ips.py logs`. On Windows a wrong DLL on PATH shows as exit code 3221225785; `ips.py` already puts MSYS2 first.
- **Port in use**: change `port` in `config.json` and `set IPS_URL=http://127.0.0.1:<port>`.
- **Cursor stuck at wrong place after moving the projector**: `python ips.py calibrate`.
- **Browser app doesn't react to gestures**: external websites can't be driven from the display page; run the agent with `--os-pointer`.
