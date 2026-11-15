"""CPU-light, dependency-free temporal hand tracking for up to two hands.

All coordinates are normalized camera-frame coordinates. Importable on a Pi
without OpenCV/MediaPipe and testable with synthetic landmarks.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import product
from math import exp, hypot, pi
from typing import Deque


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class HandDetection:
    """A single MediaPipe observation, not a prediction."""
    center: tuple[float, float]
    tip: tuple[float, float]
    thumb: tuple[float, float]
    palm_size: float
    handedness: str = "Unknown"
    landmarks: list[tuple[float, float]] = field(default_factory=list)

    @classmethod
    def from_landmarks(cls, landmarks, handedness: str = "Unknown") -> "HandDetection":
        points = [(float(p.x), float(p.y)) for p in landmarks]
        if len(points) != 21:
            raise ValueError("Expected 21 MediaPipe landmarks")
        # Palm center is less jittery than a fingertip, better for stable IDs.
        indices = (0, 5, 9, 13, 17)
        center = (sum(points[i][0] for i in indices) / len(indices),
                  sum(points[i][1] for i in indices) / len(indices))
        palm_size = max(.025, distance(points[0], points[9]), distance(points[5], points[17]))
        return cls(center, points[8], points[4], palm_size, handedness, points)


class OneEuro2D:
    """Adaptive low-pass: smooth at rest, respond quickly during motion."""

    def __init__(self, min_cutoff: float = 1.6, beta: float = 1.8, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.value: tuple[float, float] | None = None
        self.derivative = (0.0, 0.0)
        self.last_t: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        return 1.0 - exp(-2.0 * pi * max(0.001, cutoff) * dt)

    def update(self, pos: tuple[float, float], t: float) -> tuple[float, float]:
        if self.value is None or self.last_t is None or t - self.last_t > .30:
            self.value = pos
            self.derivative = (0.0, 0.0)
            self.last_t = t
            return pos
        dt = max(.001, t - self.last_t)
        old = self.value
        raw_d = ((pos[0] - old[0]) / dt, (pos[1] - old[1]) / dt)
        ad = self._alpha(self.d_cutoff, dt)
        self.derivative = (self.derivative[0] + ad * (raw_d[0] - self.derivative[0]),
                           self.derivative[1] + ad * (raw_d[1] - self.derivative[1]))
        cutoff = self.min_cutoff + self.beta * hypot(*self.derivative)
        a = self._alpha(cutoff, dt)
        self.value = (old[0] + a * (pos[0] - old[0]), old[1] + a * (pos[1] - old[1]))
        self.last_t = t
        return self.value


@dataclass
class Track:
    id: int
    center: tuple[float, float]
    tip: tuple[float, float]
    thumb: tuple[float, float]
    palm_size: float
    handedness: str
    last_seen: float
    first_seen: float
    velocity: tuple[float, float] = (0.0, 0.0)
    smooth_tip: tuple[float, float] = (0.0, 0.0)
    filter: OneEuro2D = field(default_factory=OneEuro2D)
    trail: Deque[tuple[float, float, float]] = field(default_factory=lambda: deque(maxlen=24))
    pinching: bool = False
    last_swipe: float = -1000.0
    landmarks: list[tuple[float, float]] = field(default_factory=list)
    seen_frames: int = 1

    @classmethod
    def create(cls, idx: int, det: HandDetection, t: float) -> "Track":
        track = cls(idx, det.center, det.tip, det.thumb, det.palm_size,
                    det.handedness, t, t, landmarks=det.landmarks)
        track.smooth_tip = track.filter.update(det.tip, t)
        track.trail.append((t, *det.tip))
        return track

    def predicted_center(self, t: float) -> tuple[float, float]:
        dt = clamp(t - self.last_seen, 0, .16)
        return (self.center[0] + self.velocity[0] * dt,
                self.center[1] + self.velocity[1] * dt)

    def update(self, det: HandDetection, t: float) -> None:
        dt = max(.001, t - self.last_seen)
        measured = ((det.center[0] - self.center[0]) / dt,
                    (det.center[1] - self.center[1]) / dt)
        # Velocity smoothing is for matching/prediction, not for detecting a swipe.
        alpha = .70 if dt < .16 else 1.0
        self.velocity = (alpha * measured[0] + (1 - alpha) * self.velocity[0],
                         alpha * measured[1] + (1 - alpha) * self.velocity[1])
        if dt > .25:
            self.trail.clear()  # Do not invent a swipe across missing frames.
        self.center, self.tip, self.thumb = det.center, det.tip, det.thumb
        self.palm_size = det.palm_size
        self.handedness = det.handedness
        self.landmarks = det.landmarks
        self.last_seen = t
        self.seen_frames += 1
        self.smooth_tip = self.filter.update(det.tip, t)
        self.trail.append((t, *det.tip))
        while self.trail and t - self.trail[0][0] > .45:
            self.trail.popleft()

    def as_dict(self, t: float, predicted: bool) -> dict:
        dt = max(0.0, t - self.last_seen)
        tip = self.smooth_tip
        center = self.center
        if predicted:
            # Extrapolate only briefly; never produce gestures from extrapolation.
            tip = (clamp(tip[0] + self.velocity[0] * dt),
                   clamp(tip[1] + self.velocity[1] * dt))
            center = (clamp(center[0] + self.velocity[0] * dt),
                      clamp(center[1] + self.velocity[1] * dt))
        return {
            "id": self.id, "handedness": self.handedness,
            "detected": not predicted, "predicted": predicted,
            "tip": [round(clamp(x), 5) for x in tip],
            "raw_tip": [round(clamp(x), 5) for x in self.tip] if not predicted else None,
            "center": [round(clamp(x), 5) for x in center],
            "velocity": [round(x, 4) for x in self.velocity],
            "pinching": self.pinching if not predicted else False,
            "last_seen_ms_ago": round(dt * 1000, 1),
        }


class MotionEngine:
    def __init__(self, max_hands: int = 2, keep_track_s: float = .45,
                 predict_s: float = .12, swipe_distance: float = .16):
        self.max_hands = max_hands
        self.keep_track_s = keep_track_s
        self.predict_s = min(predict_s, keep_track_s)
        self.swipe_distance = swipe_distance
        self.tracks: dict[int, Track] = {}
        self.next_id = 1
        self.zoom_baseline: float | None = None
        self.zoom_last_event = -1000.0

    @staticmethod
    def _match_cost(track: Track, det: HandDetection, t: float) -> float | None:
        dt = t - track.last_seen
        if dt > .45:
            return None
        # Distance + small prior for reported handedness. Keeps identity through
        # routine hand reordering while allowing legitimately fast movement.
        dist = distance(track.predicted_center(t), det.center)
        gate = .30 + min(.20, dt * 1.0)
        if dist > gate:
            return None
        label_cost = (.06 if track.handedness != "Unknown" and
                      det.handedness != "Unknown" and
                      track.handedness != det.handedness else 0.0)
        return dist + label_cost

    def _assign(self, detections: list[HandDetection], t: float) -> list[int | None]:
        """Globally match 0-2 detections to existing IDs, or create new ones."""
        active = sorted(self.tracks.values(), key=lambda tr: tr.id)
        options: list[list[int | None]] = []
        for det in detections:
            good = [tr.id for tr in active if self._match_cost(tr, det, t) is not None]
            options.append([None] + good)
        best = [None] * len(detections)
        best_cost = float("inf")
        for assignment in product(*options):
            assigned = [idx for idx in assignment if idx is not None]
            if len(set(assigned)) != len(assigned):
                continue
            cost = 0.0
            for det, idx in zip(detections, assignment):
                cost += .31 if idx is None else self._match_cost(self.tracks[idx], det, t)
            if cost < best_cost:
                best_cost, best = cost, list(assignment)
        return best

    def _pinch_event(self, tr: Track, t: float) -> dict | None:
        gap = distance(tr.tip, tr.thumb) / max(.025, tr.palm_size)
        if not tr.pinching and gap < .37:
            tr.pinching = True
            tr.trail.clear()
            tr.trail.append((t, *tr.tip))
            return {"type": "PINCH_START", "hand_id": tr.id}
        if tr.pinching and gap > .52:
            tr.pinching = False
            tr.trail.clear()
            tr.trail.append((t, *tr.tip))
            return {"type": "PINCH_END", "hand_id": tr.id}
        return None

    def _swipe_event(self, tr: Track, t: float) -> dict | None:
        if tr.pinching or t - tr.last_swipe < .43 or len(tr.trail) < 2:
            return None
        now_x, now_y = tr.tip
        # Choose a short, recent interval for low latency, not a multi-second
        # mean. A fast swipe only needs two actually observed frames.
        for prev_t, prev_x, prev_y in tr.trail:
            dt = t - prev_t
            if dt < .065 or dt > .42:
                continue
            dx, dy = now_x - prev_x, now_y - prev_y
            major = max(abs(dx), abs(dy))
            minor = min(abs(dx), abs(dy))
            if major < self.swipe_distance or major < minor * 1.45:
                continue
            if major / dt < .45:
                continue
            if abs(dx) > abs(dy):
                kind = "SWIPE_RIGHT" if dx > 0 else "SWIPE_LEFT"
            else:
                kind = "SWIPE_DOWN" if dy > 0 else "SWIPE_UP"
            tr.last_swipe = t
            tr.trail.clear()
            tr.trail.append((t, *tr.tip))
            return {"type": kind, "hand_id": tr.id,
                    "distance": round(major, 4), "duration_ms": round(dt * 1000)}
        return None

    def update(self, detections: list[HandDetection], t: float) -> dict:
        """Call on every *processed* video frame, including zero-hand frames."""
        detections = detections[:self.max_hands]
        events: list[dict] = []
        for idx, tr in list(self.tracks.items()):
            if t - tr.last_seen > self.keep_track_s:
                if tr.pinching:
                    events.append({"type": "PINCH_END", "hand_id": idx, "reason": "lost"})
                del self.tracks[idx]

        assignment = self._assign(detections, t)
        seen: set[int] = set()
        for det, match_id in zip(detections, assignment):
            if match_id is None:
                idx = self.next_id
                self.next_id += 1
                tr = Track.create(idx, det, t)
                self.tracks[idx] = tr
                events.append({"type": "HAND_FOUND", "hand_id": idx})
            else:
                idx = match_id
                tr = self.tracks[idx]
                tr.update(det, t)
            seen.add(idx)
            pinch = self._pinch_event(tr, t)
            if pinch:
                events.append(pinch)
            swipe = self._swipe_event(tr, t)
            if swipe:
                events.append(swipe)

        for idx, tr in self.tracks.items():
            if idx not in seen and tr.pinching and t - tr.last_seen > .09:
                tr.pinching = False
                events.append({"type": "PINCH_END", "hand_id": idx, "reason": "occluded"})
                tr.trail.clear()

        visible = [self.tracks[idx] for idx in sorted(seen)]
        if len(visible) == 2 and not any(tr.pinching for tr in visible):
            sep = distance(visible[0].center, visible[1].center)
            if self.zoom_baseline is None:
                self.zoom_baseline = max(sep, .025)
            if t - self.zoom_last_event > .48 and self.zoom_baseline > .045:
                ratio = sep / self.zoom_baseline
                if ratio >= 1.22 or ratio <= .80:
                    events.append({"type": "ZOOM_IN" if ratio > 1 else "ZOOM_OUT",
                                   "hand_ids": [tr.id for tr in visible],
                                   "scale": round(ratio, 3)})
                    self.zoom_last_event = t
                    self.zoom_baseline = max(sep, .025)
        else:
            self.zoom_baseline = None

        outputs = [tr.as_dict(t, idx not in seen) for idx, tr in sorted(self.tracks.items())
                   if idx in seen or t - tr.last_seen <= self.predict_s]
        return {"timestamp_ms": round(t * 1000), "hands": outputs,
                "detected_hands": len(seen), "events": events}
