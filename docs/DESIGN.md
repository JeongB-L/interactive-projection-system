# Backend Design: Interactive Projection System (prototype)

Status: v0.3, prototype implemented (see [SPEC.md](SPEC.md) for what is done and tested). Owner: Jeongbin (backend).
Differences from the first drafts: JSON over WebSocket instead of Protobuf, `config.json` instead of YAML, SQLite + blob store for data, both cameras fused, Pi 5 (8 GB) confirmed so hub-side tracking is optional.
Companion docs: [SPEC.md](SPEC.md) · [INTERFACES.md](INTERFACES.md) · [PROTOCOL.md](PROTOCOL.md) · [RUNBOOK.md](RUNBOOK.md)

---

## 1. What the backend is for

The backend (we call it the **hub**) runs on a laptop for now. It is the **glue between the teammates' parts**:

- **In:** hand data from the camera/ML side (Aneesh, Johnny).
- **Out:** content the projector shows (Nina's UI, or a stream).
- **In the middle:** turn hand data into UI actions, keep state, log what happened.

Prototype rules:
1. **Simple first.** One process, one Pi, one projector, one user.
2. **Every outside part is behind an interface.** If a teammate's piece isn't ready, a fake stands in. When it is ready, it plugs into the same slot.
3. **C++ core, Python at the edges.** Python where the ecosystem lives (MediaPipe, ML experiments); C++ for the hub loop.
4. **Nothing here blocks the cloud later**, but nothing cloud-specific is built now.

---

## 2. The loop

The whole system is one loop: **show something -> the hand moves on top of it -> read the motion -> change what is shown.**

```
  [Hub: Display Out] --content--> PROJECTOR --light--> SURFACE <-- hand
          ^                                               |
          |                                         CAMERA(S) on Pi
     UI actions                                           |
          |                                               v
  [Hub: Gesture -> Action] <--hand data-- [Pi: capture + hand tracking]
```

Two loops run at different speeds:
- **Fast loop (cursor, click, swipe):** must feel instant, roughly under 150 ms. Keep it short: Pi tracks the hand, sends **landmarks** (about 500 bytes), hub maps to an action, display updates.
- **Slow loop (switch app, load page, ask the AI):** latency doesn't matter much; this is where streaming and network calls are fine.

---

## 3. Components

Six parts in the hub. Each one owns one job.

| # | Component | Job | Talks to |
|---|---|---|---|
| 1 | **Edge Link** | Receives hand data (or camera frames) from the Pi; sends commands back | Aneesh's Pi agent |
| 2 | **Hand Source** | Produces `HandFrame`s regardless of where tracking ran (Pi, laptop, Python worker, recording) | Johnny's ML code |
| 3 | **Gesture Engine** | Smooths landmarks and turns them into gestures: point, pinch=click, swipe, scroll | pure C++, no I/O |
| 4 | **Action Mapper** | Gesture + calibration -> UI action in projector pixel space; config driven | YAML file |
| 5 | **Display Out** | Delivers actions/content to whatever drives the projector | Nina's UI |
| 6 | **Session Log** | Records hand data + events + timings; replays them later | disk |

Plus one small helper: **Calibration**, which stores the camera-to-projector mapping (a 3x3 homography). For the prototype it's a 4-corner click routine, saved to a file.

---

## 4. The projector: two ways to show content

We have a projector, so content can reach it two ways. Both sit behind one interface (`IDisplay`), so the rest of the hub doesn't care.

| Mode | How | Good for | Catch |
|---|---|---|---|
| **A. Pi kiosk (HDMI)** *(default)* | Pi runs a fullscreen browser on the projector via HDMI. Hub sends small **action events** (`pointer_move`, `click`, `swipe`); a tiny JS shim turns them into normal browser events. | Fast loop: cursor, click, drawing, games | Content must be a web page |
| **B. Stream to projector** | Hub streams content (video/MJPEG/WebRTC) into a page the Pi plays over HDMI. Casting straight to the projector's own Wi-Fi is possible but bypasses the Pi, so it is a fallback, not the plan. | Slow loop: videos, a laptop app, demo content | Adds roughly 100-300 ms, too slow for a cursor |

**Prototype choice:** A for interaction, with B available for showing content. They can **combine**: stream content as the background, and draw the cursor/highlight overlay locally on the Pi so the fast loop never waits on the stream.

Reading motion **on top of** projected content has one known problem: the projected image lands on the hand too and can confuse tracking. Prototype mitigations, in order: keep the projected image dim or dark where hands usually are, fix the camera exposure, and only if needed mask out the known projected image. This is the ML side's problem to measure; the hub just exposes `set_brightness` / `show_calibration` commands so they can test.

---

## 5. Integration points (what each teammate plugs into)

| Teammate area | They build | They plug into | Until it's ready, we use |
|---|---|---|---|
| **Pi / camera** (Aneesh) | Capture + Pi agent | `Edge Link`, messages in PROTOCOL.md | `pi-sim`: replays a recording or uses the laptop webcam |
| **ML / gestures** (Johnny) | Hand tracking, maybe a gesture model | `IHandSource` (where tracking runs) and `IGestureRecognizer` (what counts as a gesture) | MediaPipe via Python + simple rule-based gestures |
| **UI / projector** (Nina) | Projected pages, calibration screen | `IDisplay` + action events | a plain web page with a cursor dot |
| **Backend** (Jeongbin) | Hub, protocol, interfaces, logging | owns all of the above slots | n/a |

The **only shared contract** everyone must agree on is the message format in PROTOCOL.md. Everything else can change on one side without breaking the others.

---

## 5b. Apps and models (minimal, added after review)

**Apps.** An app is a web page plus a line in `config.json` (`id`, `url`, `needs_internet`). The hub sends `Content{url}` to switch apps. A built-in **launcher page** (grid of app tiles) is the home screen; a gesture (open palm held 1 s) returns to it. Apps that need a secret (AI chat API key) call a small hub HTTP endpoint (`/api/ai`) so the key stays on the laptop, not the Pi. Browser, AI chat, and one game are the prototype targets; they need internet, everything else works offline.

**Models.** A `models/` folder with one subfolder per model version and a `models.yaml` choosing the active one per role (`tracker`, `gesture`). Each benchmark run records which model version and hand source it used. No registry service, no promotion workflow. This is the "place to manage the ML stuff" for the prototype.

**Calibration (how the 4-corner routine works).** The display shows four markers; the user points a fingertip at each and holds a pinch; the hub records the camera-space fingertip for each marker and computes the homography. No special hardware, works with one camera.

## 5c. Storage: blobs on disk, an index in SQLite

Bulk data (camera video, session recordings) is never put into the database. Each file is written to a **blob store** and SQLite keeps one row per file: `key` (path relative to the store), `uri` (full location), `kind`, `bytes`, `meta`, and the session.

```
camera frames --> IBlobStore (file:./data/blobs today, s3://... later) --> sessions/<time>_s3/cam0.mjpeg
                          |
                 SQLite assets row: key, uri, kind, bytes, session   <-- ML jobs loop over these rows
```
- Local to cloud: copy the blobs, rewrite every `uri` from its `key` (`ips.py migrate-uri`), change `blob_uri`. No other code changes.
- ML access: `SELECT ... FROM assets WHERE kind='video_mjpeg'`, resolve the URI, read the bytes (`tools/iter_assets.py`).
- Video is stored as concatenated JPEGs per camera (MJPEG), which ffmpeg and OpenCV read directly; it avoids a video encoder on the hub.
- Camera frames are off by default (`record.frames`); hand landmarks are on.

## 5d. Two cameras

Both cameras are used. Each has its own homography to projector pixels (calibration), so each camera yields a fingertip position in the same space. The hub averages them weighted by detection confidence, only among cameras that agree on the hand pose (an unsynchronized camera still showing the old pose is otherwise a source of fake jumps; this was found and fixed in testing). A camera that stops sending for 150 ms is ignored. One camera alone also works. This improves accuracy and tolerates one view being blocked; it is **not** 3D stereo.

## 6. C++ and Python together

Where each language goes, and how they connect:

```
   Pi (Python)                    Hub (C++ process: ipsd)                Notebooks (Python)
  MediaPipe tracking  --WS-->   Edge Link -> Gesture Engine -> Mapper    import ipscore
                                     |            ^                        (same C++ engine,
                                     v            |                         via pybind11)
                              Python worker (optional, local socket)
                              for models that only exist in Python
```

- **Pi agent stays Python.** MediaPipe's Pi example is Python; no reason to fight it. It just speaks our protocol.
- **C++ called from Python (`ipscore`, pybind11).** The gesture engine is exposed as a Python module, so the ML side tunes thresholds in a notebook on recorded data with the exact code the hub runs. This is the impressive part and it's cheap to build.
- **Python called from C++ (worker).** If a model only runs in Python, the hub sends it frames over a local socket and gets `HandFrame`s back. Separate process, so a crashing model doesn't kill the hub.
- **One schema.** Protobuf `.proto` files generate both C++ and Python types.

---

## 7. Tech stack (kept small)

| Need | Pick |
|---|---|
| Language / build | C++20, CMake, vcpkg |
| Networking | Boost.Beast (WebSocket + HTTP in one) |
| Messages | Protobuf |
| Math / vision | OpenCV (homography) |
| Optional inference on hub | ONNX Runtime (only if offload is needed) |
| Python binding | pybind11 |
| Logs | spdlog; session recordings as NDJSON files |
| Tests | GoogleTest + replay of recorded sessions |

Not in the prototype: gRPC, databases, Docker, Prometheus, auth beyond a shared token. Each has a slot (interface) if we need it later.

---

## 8. Ops, the prototype version

"Ops" here means: **can we see what happened, and can we reproduce it.**

1. **Record**: every session writes hand data, gestures, actions, and timings to `recordings/<date>_<n>.ndjson`. Camera frames are off by default (privacy + size).
2. **Replay**: `pi-sim --replay <file>` feeds a recording back through the hub. Used for demos without hardware, for bug repro, and as regression tests.
3. **Status page**: `http://localhost:8080/status` shows fps, latency per stage, last gestures, connection state. Plain HTML, no Grafana.
4. **Benchmark** (answers "what can the Pi handle?"): the Pi reports its tracking time and fps in every message; the hub logs it. Run the same recording with tracking on the Pi vs. on the laptop and compare. One table is the deliverable.
5. **Config**: one `config.json` (thresholds, dead zones, which display mode, which hand source). Tuning = edit + reload, no rebuild.

---

## 9. Build order (walking skeleton first)

| Step | Result | Who it unblocks |
|---|---|---|
| 1 | `.proto` v0 + `pi-sim` sends fake landmarks + hub logs them | Aneesh can match the format |
| 2 | Gesture Engine (point, pinch) + browser page with a moving cursor | Nina has a live target |
| 3 | Real MediaPipe on laptop webcam through the same path | Johnny sees his output drive the UI |
| 4 | Real Pi + projector + 4-corner calibration | first end-to-end demo |
| 5 | Recording/replay + status page + Pi-vs-laptop benchmark | "device limitations" answer |
| 6 | Display mode B (stream) + 1-2 apps | demo content |

---

## 10. Risks and open questions

- **Pi model vs. cameras:** only the Pi 5 has two camera (CSI) ports. Two Camera Module 3s need a Pi 5; otherwise use a USB webcam for the second camera. The hub treats cameras as a list, so it doesn't care.
- **Projector lag:** cheap projectors can add 30-80 ms of their own. Measure early (phone slow-motion video).
- **Projected light on the hand** confuses tracking (section 4).
- **Wi-Fi jitter:** use Ethernet for demos; the hub always uses the latest sample and drops stale ones.
- **Open:** one camera or stereo for the first demo? Air gestures only, or "touch the surface"? Recommend: one camera, air gestures, stereo later.
