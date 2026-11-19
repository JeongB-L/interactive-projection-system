#!/usr/bin/env python3
"""Camera Module 3 to MediaPipe to virtual_touchscreen."""

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import time

PALM_POINTS = (0, 5, 9, 13, 17)
DEFAULT_MODEL = Path(__file__).resolve().with_name("gesture_recognizer.task")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=0, help="libcamera camera index")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--capture-width", type=int, default=640)
    parser.add_argument("--capture-height", type=int, default=480)
    parser.add_argument("--screen-width", type=int, default=1920)
    parser.add_argument("--screen-height", type=int, default=1080)
    parser.add_argument("--x-max", type=int, default=1024, help="inclusive device maximum")
    parser.add_argument("--y-max", type=int, default=1024, help="inclusive device maximum")
    parser.add_argument("--click-gesture", default="Thumb_Up", help="gesture that holds the touch down")
    parser.add_argument("--min-score", type=float, default=0.6)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true", help="log frame and gesture status to stderr")
    parser.add_argument("--output", type=Path, help="write a command log to this file")
    parser.add_argument("--device", type=Path, help="send commands to /dev/virtual_touchscreen")
    args = parser.parse_args()

    if args.camera < 0:
        parser.error("--camera must be nonnegative")
    if min(args.capture_width, args.capture_height, args.x_max, args.y_max) < 1:
        parser.error("capture dimensions and touchscreen maxima must be positive")
    if min(args.screen_width, args.screen_height) < 2:
        parser.error("screen dimensions must be at least 2")
    if not 0.0 <= args.min_score <= 1.0:
        parser.error("--min-score must be between 0 and 1")
    if args.output and args.device and args.output.resolve() == args.device.resolve():
        parser.error("--output and --device must point to different paths")
    return args


def log(message):
    print(message, file=sys.stderr, flush=True)


def pixel(value, length):
    return round(max(0.0, min(1.0, value)) * (length - 1))


def palm_center(landmarks):
    return (
        sum(landmarks[index].x for index in PALM_POINTS) / len(PALM_POINTS),
        sum(landmarks[index].y for index in PALM_POINTS) / len(PALM_POINTS),
    )


def send_command(device, output, command):
    line = f"{command}\n"
    if device is not None:
        data = line.encode("ascii")
        if os.write(device, data) != len(data):
            raise OSError("short write to virtual touchscreen")
    if output is not None:
        output.write(line)
        output.flush()


def send_block(device, output, *commands):
    for command in ("s 0", *commands, "S 0"):
        send_command(device, output, command)


def run(args):
    if not args.model.is_file():
        raise FileNotFoundError(f"MediaPipe model not found: {args.model}")

    import cv2
    import mediapipe as mp
    from picamera2 import Picamera2

    options = mp.tasks.vision.GestureRecognizerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(args.model)),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_hands=1,
    )

    with ExitStack() as stack:
        device = None
        if args.device:
            device = os.open(args.device, os.O_WRONLY)
            stack.callback(os.close, device)
        output = None if args.device else sys.stdout
        if args.output:
            output = stack.enter_context(args.output.open("w", encoding="ascii"))

        recognizer = stack.enter_context(
            mp.tasks.vision.GestureRecognizer.create_from_options(options)
        )
        camera = Picamera2(args.camera)
        stack.callback(camera.close)
        camera.configure(
            camera.create_video_configuration(
                main={
                    "size": (args.capture_width, args.capture_height),
                    "format": "RGB888",
                }
            )
        )
        camera.start()
        if not args.headless:
            cv2.namedWindow("Projector gesture test", cv2.WINDOW_NORMAL)
            stack.callback(cv2.destroyAllWindows)

        last_timestamp_ms = -1
        next_log_ms = 0
        last_touch_position = None
        touch_down = False
        click_count = 0

        def set_touch(down):
            nonlocal touch_down, click_count
            if down == touch_down:
                return
            if down:
                # Release on exit even if writing the press or its log fails.
                touch_down = True
                click_count += 1
                send_block(device, output, f"T {click_count}", "0 10", ": 100", "e 0", "d 0")
            else:
                send_block(device, output, "T -1", "0 10", ": 100", "e 0", "u 0")
                touch_down = False
            if args.debug:
                log("touch down" if down else "touch up")

        stack.callback(set_touch, False)
        while True:
            frame = camera.capture_array()
            timestamp_ms = max(time.monotonic_ns() // 1_000_000, last_timestamp_ms + 1)
            last_timestamp_ms = timestamp_ms

            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
            result = recognizer.recognize_for_video(image, timestamp_ms)
            categories = result.gestures[0] if result.gestures else []
            top_gesture = max(categories, key=lambda category: category.score, default=None)
            label = top_gesture.category_name if top_gesture else ""
            score = top_gesture.score if top_gesture else 0.0
            center = palm_center(result.hand_landmarks[0]) if result.hand_landmarks else None

            gesture_held = (
                center is not None and label == args.click_gesture and score >= args.min_score
            )
            if not gesture_held:
                set_touch(False)

            if center is None:
                last_touch_position = None
            else:
                screen_x = pixel(center[0], args.screen_width)
                screen_y = pixel(center[1], args.screen_height)
                device_x = round(screen_x * args.x_max / (args.screen_width - 1))
                device_y = round(screen_y * args.y_max / (args.screen_height - 1))
                touch_position = (device_x, device_y)
                if touch_position != last_touch_position:
                    send_block(
                        device, output,
                        f"X {device_x}", f"Y {device_y}",
                        f"x {device_x}", f"y {device_y}",
                    )
                    last_touch_position = touch_position

            if gesture_held:
                set_touch(True)

            if args.debug and timestamp_ms >= next_log_ms:
                log(f"frame={frame.shape} hand={center is not None} "
                    f"gesture={label or '-'} score={score:.2f} touch_down={touch_down}")
                next_log_ms = timestamp_ms + 500

            if not args.headless:
                if center is not None:
                    height, width = frame.shape[:2]
                    position = (pixel(center[0], width), pixel(center[1], height))
                    cv2.circle(frame, position, 7, (0, 255, 0), -1)
                cv2.putText(frame, f"{label or '-'} {score:.2f}", (12, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
                cv2.imshow("Projector gesture test", frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break


def main():
    args = parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log(f"test.py: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
