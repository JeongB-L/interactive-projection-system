# Interfaces (C++ seams)

Status: v0.3. The signatures below are the design. **Implemented so far** (hub/src): `IBlobStore` + `LocalFsBlobStore`, `Catalog`, `SessionRecorder`, `Mapper`/`Homography`, `Fuser`, `RuleRecognizer`. `IHandSource`, `IDisplay` and `IEdgeLink` are not separate classes yet: the WebSocket server in `server.cpp` plays all three. Messages are JSON (see PROTOCOL.md), not Protobuf.

Rules: core code (gesture engine, mapper) does no I/O and reads time from an injected clock, so recordings replay exactly. `main()` is the only place that picks implementations, by name from `config.json`.

## Data types

```cpp
struct Landmark { float x, y, z; };                 // MediaPipe convention, normalized

struct Hand { bool right; std::array<Landmark, 21> pts; float score; };

struct HandFrame {                                   // what every hand source produces
  uint8_t  camera;
  uint64_t seq;
  int64_t  capture_us;                               // Pi clock
  std::vector<Hand> hands;
  float    track_ms;                                 // time spent tracking (for benchmarks)
};

struct Gesture {                                     // Gesture Engine output
  enum Kind { Point, PinchDown, PinchUp, Swipe, Scroll } kind;
  float x, y;                                        // camera-normalized
  float dx, dy;
  int64_t t_us;
};

struct Action {                                      // what the display receives
  enum Kind { Move, Down, Up, Swipe, Scroll, Key } kind;
  int px, py;                                        // projector pixels
  std::string detail;                                // e.g. "left" for a swipe
};
```

## The six slots

```cpp
// 1. Where hand data comes from. Pi landmarks, laptop model, Python worker, or a recording.
class IHandSource {
 public:
  virtual ~IHandSource() = default;
  virtual void start(std::function<void(const HandFrame&)> on_frame) = 0;
  virtual void stop() = 0;
};
//   PiLandmarkSource (default) | LocalModelSource (ONNX) | PythonWorkerSource | ReplaySource

// 2. What counts as a gesture. Rules first; a learned model can replace it.
class IGestureRecognizer {
 public:
  virtual ~IGestureRecognizer() = default;
  virtual std::vector<Gesture> update(const HandFrame&) = 0;
  virtual void configure(const GestureConfig&) = 0;  // thresholds, dead zones
};
//   RuleRecognizer (pinch hysteresis, swipe by velocity) | ModelRecognizer

// 3. Gesture -> UI action (uses calibration).
class IActionMapper {
 public:
  virtual ~IActionMapper() = default;
  virtual std::vector<Action> map(const Gesture&) = 0;
  virtual void set_calibration(const Homography&) = 0;
};

// 4. Whatever drives the projector.
class IDisplay {
 public:
  virtual ~IDisplay() = default;
  virtual void send(const Action&) = 0;              // fast loop
  virtual void show(const Content&) = 0;             // slow loop: URL, stream, calibration pattern
};
//   PiKioskDisplay (WebSocket to the browser on the Pi) | StreamDisplay (cast/stream) | LocalWindowDisplay (dev)

// 5. Link to the Pi (commands back: start, stop, set mode, brightness).
class IEdgeLink {
 public:
  virtual ~IEdgeLink() = default;
  virtual void on_message(std::function<void(const EdgeMsg&)>) = 0;
  virtual void send(const HubMsg&) = 0;
};
//   WebSocketEdgeLink | LoopbackEdgeLink (tests, pi-sim in-process)

// 6. Record and replay.
class ISessionLog {
 public:
  virtual ~ISessionLog() = default;
  virtual void write(const HandFrame&) = 0;
  virtual void write(const Gesture&) = 0;
  virtual void write(const Action&) = 0;
};
//   NdjsonLog | NullLog
```

## How they connect

```
IEdgeLink -> IHandSource -> IGestureRecognizer -> IActionMapper -> IDisplay
                  \________________ ISessionLog (taps every stage) ____/
```

## Python binding (`ipscore`)

```python
import ipscore
rec = ipscore.RuleRecognizer(ipscore.load_config("config.json"))
for frame in ipscore.replay("recordings/2026-10-24_1.ndjson"):
    for g in rec.update(frame):
        print(g.kind, g.x, g.y)
```
Only the pure pieces (recognizer, mapper, replay) are exposed. No networking.
