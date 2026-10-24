# Specification: Interactive Projection System, Backend (Hub)

Version 1.2 (prototype). Date: 2026-10-24. Owner: Jeongbin Lee.
Related: [DESIGN.md](DESIGN.md) (rationale) · [INTERFACES.md](INTERFACES.md) (C++ slots) · [PROTOCOL.md](PROTOCOL.md) (messages) · [RUNBOOK.md](RUNBOOK.md) (how to run it)

**MUST** = required for the prototype, **SHOULD** = target, **MAY** = optional.
The **Status** column says what exists in the repo today: Done (built and tested), Partial, Untested (written, no hardware to test), Not yet.

## Changes in 1.2

- **Local only.** Cloud storage (e.g. AWS S3, a paid service) is intentionally not built; `IBlobStore` remains so it can be added later. The project has no running costs and needs no accounts.
- **One-command setup** for forks: `setup.ps1` (Windows) and `setup.sh` (Linux/Pi/macOS). (F-33)

## Changes in 1.1 (from team feedback)

| # | Feedback | Change |
|---|---|---|
| 1 | API keys belong in an env file like a normal app | `.env` (gitignored) holds `ANTHROPIC_API_KEY`, `AI_MODEL`, `IPS_TOKEN`. `.env.example` is committed. Keys never go to the Pi. (F-20) |
| 2 | Blob storage + SQLite pointing at it, local now and cloud later | Binary data (video/frames, recordings) goes to a **blob store** behind `IBlobStore`; **SQLite stores only a key + URI + metadata**. Local disk now; switching to cloud = new base URI. (F-28 to F-31) |
| 3 | Data later usable for ML by walking the DB | `assets` table is the index; `ips.py assets` lists and `tools/iter_assets.py` shows the loop. (F-31) |
| 4 | Hardware is a Pi 5 (8 GB) with 2 cameras | Offload tracking (F-2) downgraded MUST to SHOULD (Pi 5 can track locally). Both cameras are used for **multi-angle fusion** (F-4), not just one. |
| 5 | Latency to be figured out | Built-in measurement: per-stage hub latency, end-to-end latency, fps, drops; `ips.py bench` prints a table. Targets stay unvalidated until measured on the real Pi. (N-1) |
| 6 | Public GitHub repo, simple runbook, CLI for functions | Repo is public. `ips.py` provides start/stop/status/sim/launch/calibrate/sessions/assets/bench. (F-32, RUNBOOK.md) |
| 7 | Keep it simple | JSON over WebSocket instead of Protobuf (no codegen, debuggable with any tool); JSON config instead of YAML; `ipscore` Python binding deferred. |

---

## 1. Purpose and scope

A user stands at a flat surface on which a projector shows a UI. Two cameras watch the hand over the image. Hand gestures control what is shown.

**In scope:** the hub (C++, on a laptop) that receives hand data from the Pi, fuses the cameras, recognizes gestures, maps them to projector coordinates, delivers actions and content to the display, and stores/measures sessions; plus a simple Pi-side agent and demo UI so the whole loop runs end to end.

**Out of scope:** Pi OS setup, the final hand-tracking model, UI design polish, cloud deployment, multi-user, real authentication, true stereo triangulation.

**Hardware (confirmed):** Raspberry Pi 5 (8 GB), 2 camera modules, small projector. Hub on a laptop, same LAN.

## 2. System context

```
Cam0, Cam1 -> Pi agent --JSON/WS--> HUB --actions/content--> Display page (Chromium kiosk on Pi) --HDMI--> Projector
                                     |                                   ^
                                     +--> blob store (video, logs)       |  hand over the image
                                     +--> SQLite (index of blobs)   <-- cameras watch it
```

## 3. Functional requirements

### 3.1 Input and tracking
| ID | Requirement | Pri | Status |
|---|---|---|---|
| F-1 | Accept hand landmark frames (21×xyz per hand, per camera) over WebSocket (PROTOCOL.md). | MUST | Done |
| F-2 | Accept JPEG frames and obtain hand data on the hub (local model or Python worker). | SHOULD | Frames accepted and stored; hub-side tracking Not yet |
| F-3 | Hand source selected by config, behind an interface. | MUST | Partial (agent `--source`; hub takes landmarks only) |
| F-4 | Use both cameras: each camera has its own calibration to projector space; positions and pinch are combined by confidence-weighted fusion. One camera alone MUST also work. | MUST | Done (fusion tested in simulation) |
| F-5 | Never let stale data drive the cursor: frames older than `stale_ms` are ignored in fusion; sequence gaps are counted as drops. | MUST | Done |

### 3.2 Gestures
| ID | Requirement | Pri | Status |
|---|---|---|---|
| F-6 | Produce **point** (cursor), **pinch** (down/up = click/drag), **swipe** (4 directions), **home** from hand frames. | MUST | Done |
| F-6b | Scroll gesture. | MAY | Not yet (pinch-drag only) |
| F-7 | Pinch uses hysteresis normalized by hand size. | MUST | Done |
| F-8 | Cursor smoothed (one-euro); dead zone and sensitivity in config. | MUST | Done |
| F-9 | Open palm held still (default 1 s) returns to the launcher. | MUST | Done |
| F-10 | Gesture engine is deterministic and I/O-free. | MUST | Done (unit tests) |
| F-11 | Learned recognizer can replace the rule recognizer. | MAY | Not yet |

### 3.3 Mapping, calibration, display
| ID | Requirement | Pri | Status |
|---|---|---|---|
| F-12 | Map camera coordinates to projector pixels with a per-camera homography. | MUST | Done (math unit-tested) |
| F-13 | Calibration: four projected markers, pinch at each; homography per camera saved to `data/calibration.json` and reloaded. | MUST | Done in code; needs real camera to validate |
| F-14 | The UI reaches the projector through the Pi over HDMI: the Pi shows a fullscreen browser page and the hub sends actions to it. | MUST | Done (display page); Pi kiosk Untested |
| F-14b | For pages the display shell cannot control (a real external website), the Pi agent injects **OS-level mouse events** from the same actions (`--os-pointer`). | SHOULD | Untested |
| F-15 | Stream content into the page (video) while the cursor stays locally rendered. | SHOULD | Not yet (page can embed any URL) |
| F-16 | Each gesture-derived action changes the projected image. | MUST | Done |
| F-17 | Hub can show the calibration pattern and change page brightness for tracking experiments. | MUST | Partial (calibration pattern done; brightness via `/api/brightness`) |

### 3.4 Apps
| ID | Requirement | Pri | Status |
|---|---|---|---|
| F-18 | An app is a web page declared in `config.json`; hub switches apps by sending a URL. | MUST | Done |
| F-19 | Launcher page lists apps. Prototype: browser, AI chat, snake. | MUST | Done |
| F-20 | The AI key lives in `.env` on the hub only; the AI app calls `/api/ai` on the hub, which calls the provider. The key is never sent to the Pi or the browser. | MUST | Done; live provider call Untested (no key in repo) |
| F-21 | Internet-dependent apps show a message when offline. | SHOULD | Done for AI; browser tile relies on the browser's own error page |

### 3.5 Storage, recording, benchmarking
| ID | Requirement | Pri | Status |
|---|---|---|---|
| F-22 | Each session is recorded: hand frames and actions to an NDJSON blob; camera frames (when enabled) to one MJPEG blob per camera. Camera frames are OFF by default. | MUST | Done |
| F-23 | A recording can be replayed through the full pipeline with no hardware. | MUST | Done (`edge/agent.py --source replay`) |
| F-24 | Status page and `/api/status`: fps, hub and end-to-end latency (p50/p95), drops, connections, last gestures. | MUST | Done |
| F-25 | The agent reports `track_ms`; the hub logs it, so Pi-local vs. other configurations can be compared on the same input. | MUST | Done |
| F-26 | Model files in `models/<name>/<version>/` with `models.yaml`. | SHOULD | Deferred: no hub-side model yet. Folder reserved. |
| F-27 | Python binding of the gesture engine (`ipscore`). | MAY | Deferred |
| F-28 | **Blob store** behind `IBlobStore`. Config `blob_uri`. Implemented: `file:` (local disk) only, by decision (no cloud cost). | MUST | Done (local) |
| F-29 | **SQLite catalog** (`data/ips.db`) with tables `sessions` and `assets`. `assets` rows hold `key`, `uri`, `kind`, `bytes`, `meta` (JSON), `created_utc`, and the session id. Bulk data is never inserted into SQLite. | MUST | Done |
| F-30 | Switching storage = change `blob_uri`; `ips.py migrate-uri OLD NEW` rewrites `uri` from `key`. | MUST | Done (rewrite only; copying the files is manual/`aws s3 sync`) |
| F-31 | ML access: iterate over `assets` (filter by `kind`, session, date), resolve `uri`, read the blob. | MUST | Done (`tools/iter_assets.py`) |
| F-33 | `setup.ps1` / `setup.sh` install dependencies, build, and test from a fresh clone; documented in the README. | MUST | See section 8 |
| F-32 | CLI `ips.py`: start, stop, status, sim, open, launch, home, calibrate, apps, sessions, assets, bench, migrate-uri, logs. | MUST | Done |

## 4. Non-functional requirements

| ID | Requirement | Target | Status |
|---|---|---|---|
| N-1 | End-to-end latency (camera capture → action emitted by hub) | p95 ≤ 150 ms (**unvalidated target**) | Measurable now; only simulated numbers exist. Real numbers need the Pi + synced clocks. |
| N-2 | Hub processing per frame | ≤ 2 ms p95 | Measured: p50 0.035 ms, p95 0.17 ms (see section 8) |
| N-3 | Tracking rate delivered | ≥ 20 fps per camera on Pi 5 | Not yet measured on Pi |
| N-4 | Click/swipe actions are not lost | 0 lost in soak | Not yet soak-tested |
| N-5 | Pi reconnect | session resumes within 30 s | Agent reconnects; hub starts a new session (resume Not yet) |
| N-6 | Builds on Windows (MSYS2 UCRT) and Linux | both | Windows done; Linux/Pi build Untested |
| N-7 | Privacy: camera frames not recorded unless enabled; `data/` and `.env` gitignored | n/a | Done |
| N-8 | Gesture thresholds changeable without rebuild | `config.json` | Done (restart hub) |

## 5. Interfaces

C++ slots implemented: `IBlobStore` (`LocalFsBlobStore`), `Catalog`, `Mapper` (homography), `RuleRecognizer`, `Fuser`, `SessionRecorder`. Slots described in INTERFACES.md but not yet separate classes: `IHandSource`, `IDisplay`, `IEdgeLink` (the WebSocket server plays all three for now).

Endpoints: `ws://hub:8080/edge`, `ws://hub:8080/display`, `GET /status`, `GET /api/status`, `GET /api/apps`, `POST /api/launch`, `POST /api/home`, `POST /api/calibrate/start`, `POST /api/calibrate/reset`, `POST /api/display_size`, `POST /api/ai`, `GET /api/sessions`, `GET /api/assets`, `POST /api/shutdown`. Static: `/ui/*`, `/apps/*`.

## 6. Configuration

`config.json` (hub) and `.env` (secrets). See files in the repo root; keys documented in RUNBOOK.md.

## 7. Acceptance tests

| # | Test | Passes when | Status |
|---|---|---|---|
| T-1 | Unit tests (`ips_tests`): homography, pinch hysteresis, swipe, home, catalog + blob | all pass | Done |
| T-2 | Start hub, run 2-camera simulation for 20 s | status shows frames from 2 cameras, down/up/swipe/home counts > 0, no errors | Done |
| T-3 | Display page open during simulation | cursor moves; a simulated pinch on a launcher tile launches the app | Done (checked in a browser) |
| T-4 | Recording | session row, NDJSON blob, asset rows with correct URI and size | Done |
| T-5 | `migrate-uri` | URIs rewritten, keys unchanged | Done |
| T-6 | Replay a recorded session through the hub | same gesture counts as the original run | Done |
| T-7 | `/api/ai` with a dummy key | reaches the provider over TLS and returns its auth error; with no key returns a clear message | Done (dummy key) |
| T-8 | Real Pi + two cameras + projector: calibration, 9-target accuracy | within 15 px (target) | Not yet (hardware) |
| T-9 | Real latency table on the Pi | filled in | Not yet (hardware) |
| T-10 | Projected light vs. tracking rate | rate recorded bright vs. dim | Not yet (hardware) |

## 8. Verification log (what was actually run, 2026-10-24, Windows 11, MSYS2 GCC 13.2)

| Check | Result |
|---|---|
| Unit tests (`python ips.py test`) | 42 checks, 0 failed |
| 2-camera synthetic run, 14 s, 60 frames/s | 840 frames, 0 dropped; per 12 s routine exactly 2 down, 2 up, 1 swipe, 1 home |
| Hub processing time | p50 0.035 ms, p95 0.17 ms |
| End-to-end, same machine (agent timestamp to action) | p50 1.2 ms, p95 1.8 ms. **Loopback with fake hands: says nothing about the real Pi, camera or projector** |
| Replay of the recorded session | identical gesture counts to the original run |
| Video storage (30 fake JPEG frames on 2 cameras) | 2 MJPEG blobs, byte counts match, catalog rows correct, readable via `tools/iter_assets.py` |
| `migrate-uri` | dry run only (rewrites URIs from keys); not run against real cloud storage |
| AI route | no key: clear error. Dummy key: real TLS connection to the provider, provider's 401 returned. **No successful completion was tested (no real key)** |
| Display page in a browser | app rendered, cursor moved, simulated pinch on a launcher tile launched Snake |
| Bug found by testing and fixed | cameras switching pose a frame apart made fusion average two poses and fire a false swipe; fix is pose-agreement fusion plus a regression test (verified to fail without the fix) |

**Not verified:** anything on the real Pi (picamera2, MediaPipe, OS pointer injection, Chromium kiosk), real latency, calibration accuracy, projector effects, Linux build, long soak tests.

## 9. Open items

1. **True stereo (3D touch)** is not built; fusion combines two 2D views for accuracy. Real surface "touch" needs triangulation and camera extrinsics.
2. **Latency:** only simulated numbers exist. Projector input lag is unknown.
3. **Projected light on the hand** may degrade tracking; measure with T-10.
4. **Pi code is untested:** the camera/MediaPipe/picamera2 paths and OS pointer injection could not be run here.
5. **Cloud:** intentionally not built (paid, not needed for testing). Adding it means one `IBlobStore` class plus `migrate-uri`.
6. **Hub-side tracking** (F-2) is not built; the Pi 5 is expected to track locally.
