#!/usr/bin/env python3
"""Raspberry Pi hand tracker V2: low-latency 0-2 hand motion to JSONL.

Defaults to headless. --show enables a deliberately minimal OpenCV preview.
No old project files need to be overwritten.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import statistics
import sys
import threading
import time

from tracker import HandDetection, MotionEngine

ROOT = Path(__file__).resolve().parent


class LatestFrameCamera:
    """One capture thread; inference consumes only the newest camera frame."""

    def __init__(self, backend: str, index: int, width: int, height: int, fps: int,
                 exposure_us: int | None = None, gain: float | None = None):
        self.backend, self.index = backend, index
        self.width, self.height, self.fps = width, height, fps
        self.exposure_us, self.gain = exposure_us, gain
        self.condition = threading.Condition()
        self.frame = None
        self.frame_t = 0.0
        self.seq = 0
        self.error = None
        self.closed = False
        self.thread = None
        self.device = None

    def start(self):
        if self.backend == "picamera2":
            try:
                from picamera2 import Picamera2
            except ImportError as exc:
                raise RuntimeError("Install picamera2 via: sudo apt install python3-picamera2") from exc
            picam = Picamera2(self.index)
            interval = int(1_000_000 / self.fps)
            # Picamera2's RGB888 buffers are BGR-ordered in NumPy, compatible
            # with the OpenCV path. queue=False prevents stale cached frames.
            controls = {"FrameDurationLimits": (interval, interval)}
            if self.exposure_us is not None:
                controls.update({"AeEnable": False, "ExposureTime": self.exposure_us})
                if self.gain is not None:
                    controls["AnalogueGain"] = self.gain
            config = picam.create_video_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                controls=controls,
                buffer_count=3, queue=False)
            picam.configure(config)
            picam.start()
            self.device = picam
        else:
            import cv2
            backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
            cap = cv2.VideoCapture(self.index, backend)
            if not cap.isOpened():
                raise RuntimeError(f"Camera {self.index} failed to open. Try --camera 1 or --backend picamera2")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.device = cap
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        return self

    def _capture_loop(self):
        try:
            while not self.closed:
                if self.backend == "picamera2":
                    frame = self.device.capture_array("main")
                    ok = frame is not None
                else:
                    ok, frame = self.device.read()
                captured = time.monotonic()
                if not ok:
                    raise RuntimeError("Camera stopped supplying frames")
                with self.condition:
                    self.frame = frame
                    self.frame_t = captured
                    self.seq += 1
                    self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                if not self.closed:
                    self.error = exc
                self.condition.notify_all()

    def newest(self, last_seq: int, timeout: float = 2.0):
        with self.condition:
            self.condition.wait_for(lambda: self.seq > last_seq or self.error or self.closed, timeout)
            if self.error:
                raise RuntimeError(str(self.error))
            if self.seq == last_seq:
                return None
            return self.frame, self.frame_t, self.seq

    def stop(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        # Stop camera before join to help unblock a pending capture.
        if self.device:
            try:
                if self.backend == "picamera2":
                    self.device.stop()
                    self.device.close()
                else:
                    self.device.release()
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=1.0)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--camera", type=int, default=0, help="USB webcam index or Picamera2 camera index")
    p.add_argument("--backend", choices=["opencv", "picamera2"], default="opencv")
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=480)
    p.add_argument("--fps", type=int, default=30, help="Requested camera FPS, not guaranteed processed FPS")
    p.add_argument("--max-hands", type=int, choices=[1, 2], default=2)
    p.add_argument("--exposure-us", type=int, default=None,
                   help="Picamera2 manual exposure in microseconds (e.g. 5000 for fast motion)")
    p.add_argument("--gain", type=float, default=None,
                   help="Picamera2 analogue gain when using --exposure-us")
    p.add_argument("--model", type=Path, default=None, help="Hand Landmarker or old Gesture Recognizer .task")
    p.add_argument("--detection-confidence", type=float, default=.50)
    p.add_argument("--presence-confidence", type=float, default=.45)
    p.add_argument("--tracking-confidence", type=float, default=.45)
    p.add_argument("--swipe-distance", type=float, default=.16, help="Fraction of image width/height")
    p.add_argument("--no-mirror", action="store_true", help="Do not flip the input horizontally")
    p.add_argument("--show", action="store_true", help="Draw tiny debug overlay; requires GUI OpenCV")
    p.add_argument("--jsonl", type=Path, help="Save every processed frame with motion events as JSON Lines")
    p.add_argument("--stdout", action="store_true", help="Stream every processed frame as JSON Lines to stdout")
    p.add_argument("--seconds", type=float, default=0, help="Exit after N seconds (0 = no limit)")
    p.add_argument("--self-test", action="store_true", help="Run offline tests; no model or camera required")
    p.add_argument("--summary", type=Path, help="Save performance counters to JSON on exit")
    return p.parse_args()


def resolve_model(user_path: Path | None) -> Path:
    if user_path:
        candidates = [user_path, ROOT / user_path]
    else:
        candidates = [ROOT / "models" / "hand_landmarker.task",
                      ROOT / "models" / "gesture_recognizer.task"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("Missing .task model. Run 'python download_model.py' or copy "
                       "your previous models/gesture_recognizer.task into models/.")


def create_detector(mp, model: Path, max_hands: int, args):
    is_gesture = "gesture_recognizer" in model.name.lower()
    cls = mp.tasks.vision.GestureRecognizer if is_gesture else mp.tasks.vision.HandLandmarker
    opt_cls = (mp.tasks.vision.GestureRecognizerOptions if is_gesture
               else mp.tasks.vision.HandLandmarkerOptions)
    opt = opt_cls(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model)),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_hands=max_hands,
        min_hand_detection_confidence=args.detection_confidence,
        min_hand_presence_confidence=args.presence_confidence,
        min_tracking_confidence=args.tracking_confidence)
    return cls.create_from_options(opt), cls.__name__


def observations(result) -> list[HandDetection]:
    hands = []
    for i, points in enumerate(result.hand_landmarks):
        categories = result.handedness[i] if i < len(result.handedness) else []
        label = categories[0].category_name if categories else "Unknown"
        hands.append(HandDetection.from_landmarks(points, label))
    return hands


def render_preview(frame, state, fps: float, latency_ms: float):
    import cv2
    height, width = frame.shape[:2]
    for hand in state["hands"]:
        x = int(hand["tip"][0] * width)
        y = int(hand["tip"][1] * height)
        color = (0, 230, 60) if hand["detected"] else (0, 180, 255)
        cv2.circle(frame, (x, y), 7, color, 2)
        label = f"#{hand['id']} {hand['handedness']}" + (" PRED" if hand["predicted"] else "")
        if hand["pinching"]:
            label += " PINCH"
        cv2.putText(frame, label, (max(0, x - 40), max(15, y - 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, .42, color, 1, cv2.LINE_AA)
    cv2.putText(frame, f"{fps:.1f} processed FPS | {latency_ms:.0f} ms infer | "
                f"{state['detected_hands']} hands | Q quit", (8, 19),
                cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1, cv2.LINE_AA)
    return frame


def run(args):
    if min(args.width, args.height, args.fps) <= 0:
        raise ValueError("Width, height and fps must be positive")
    model = resolve_model(args.model)
    try:
        import cv2
        import numpy as np
        import mediapipe as mp
    except ImportError as exc:
        raise RuntimeError("Install MediaPipe/OpenCV/NumPy as described in README.md") from exc
    cv2.setNumThreads(1)
    engine = MotionEngine(args.max_hands, swipe_distance=args.swipe_distance)
    camera = None
    log = None
    detector = None
    t_start = time.monotonic()
    last_seq = 0
    last_ms = -1
    processed = 0
    skipped = 0
    frames_with_two = 0
    events_seen = 0
    inference_ms = deque(maxlen=1000)
    lag_ms = deque(maxlen=1000)
    last_report = t_start
    last_report_processed = 0
    fps_ema = 0.0
    previous_processed = None
    result_summary = {}
    try:
        detector, task_name = create_detector(mp, model, args.max_hands, args)
        camera = LatestFrameCamera(args.backend, args.camera, args.width, args.height, args.fps,
                                   args.exposure_us, args.gain).start()
        if args.jsonl:
            args.jsonl.parent.mkdir(parents=True, exist_ok=True)
            log = args.jsonl.open("w", buffering=1, encoding="utf-8")
        print(f"Tracker V2 | {task_name} | {model.name} | camera {args.camera} "
              f"({args.backend}) | max hands {args.max_hands}", file=sys.stderr)
        print("Headless by default; --show for minimal preview. Ctrl+C to exit.", file=sys.stderr)
        while True:
            now = time.monotonic()
            if args.seconds > 0 and now - t_start >= args.seconds:
                break
            newest = camera.newest(last_seq)
            if newest is None:
                continue
            frame, captured_t, seq = newest
            skipped += max(0, seq - last_seq - 1)
            last_seq = seq
            if not args.no_mirror:
                frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
            # Use capture time rather than processing time for accurate velocity.
            ts_ms = max(last_ms + 1, int((captured_t - t_start) * 1000))
            last_ms = ts_ms
            before = time.monotonic()
            result = detector.detect_for_video(image, ts_ms)
            inference = (time.monotonic() - before) * 1000
            output = engine.update(observations(result), captured_t)
            output["timestamp_ms"] = ts_ms
            output["inference_ms"] = round(inference, 2)
            output["age_ms"] = round((time.monotonic() - captured_t) * 1000, 2)
            processed += 1
            frames_with_two += int(output["detected_hands"] == 2)
            events_seen += len(output["events"])
            inference_ms.append(inference)
            lag_ms.append(output["age_ms"])
            if previous_processed is not None:
                inst_fps = 1.0 / max(1e-3, time.monotonic() - previous_processed)
                fps_ema = inst_fps if fps_ema == 0 else .15 * inst_fps + .85 * fps_ema
            previous_processed = time.monotonic()
            if log:
                log.write(json.dumps(output, separators=(",", ":")) + "\n")
            if args.stdout:
                print(json.dumps(output, separators=(",", ":")), flush=True)
            elif output["events"]:
                print(json.dumps({"timestamp_ms": ts_ms, "events": output["events"]}), flush=True)
            if args.show:
                cv2.imshow("Pi Hand Tracker V2", render_preview(frame, output, fps_ema, inference))
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
            now = time.monotonic()
            if now - last_report >= 3:
                reported_fps = (processed - last_report_processed) / (now - last_report)
                print(f"processed={reported_fps:.1f} fps  inferred={inference:.1f} ms  "
                      f"age={output['age_ms']:.1f} ms  drops={skipped}  "
                      f"detected={output['detected_hands']}", file=sys.stderr)
                last_report, last_report_processed = now, processed
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = max(1e-6, time.monotonic() - t_start)
        result_summary = {
            "processed_frames": processed,
            "captured_skipped_frames": skipped,
            "processing_fps": round(processed / elapsed, 2),
            "two_hand_detected_frames": frames_with_two,
            "two_hand_fraction": round(frames_with_two / processed, 3) if processed else 0,
            "events": events_seen,
            "mean_inference_ms": round(statistics.mean(inference_ms), 2) if inference_ms else None,
            "mean_frame_age_ms": round(statistics.mean(lag_ms), 2) if lag_ms else None,
            "p95_frame_age_ms": round(sorted(lag_ms)[int((len(lag_ms) - 1) * .95)], 2) if lag_ms else None,
            "backend": args.backend, "model": str(model),
            "input_resolution": [args.width, args.height],
        }
        if args.summary:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            args.summary.write_text(json.dumps(result_summary, indent=2), encoding="utf-8")
        if log:
            log.close()
        if camera:
            camera.stop()
        if detector:
            detector.close()
        if args.show:
            cv2.destroyAllWindows()
        print(json.dumps(result_summary, indent=2), file=sys.stderr)


def main():
    args = parse_args()
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        res = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if res.wasSuccessful() else 1)
    try:
        run(args)
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
