"""Live, simulated or recorded Kinect depth, with contact in wall coordinates."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
from math import hypot
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic

import cv2
import numpy as np

from depth_geometry import HEIGHT, MAX_WALL_DEPTH, MIN_DEPTH, WIDTH, Intrinsics, WallPlane
from interaction import PointerEvent
from rgb_assist import RgbAssist
from wall import BOARD_SIZE

HERE = Path(__file__).resolve().parent
try:
    import kinect_touch
except ModuleNotFoundError as error:
    if error.name != "kinect_touch":
        raise
    sibling = HERE.parent / "KinectTouch" / "src"
    if not sibling.is_dir():
        raise ImportError("Install the depth dependencies with: python3 -m pip install -r requirements.txt") from error
    sys.path.insert(0, str(sibling))

from kinect_touch.config import Config
from kinect_touch.pipeline.baseline import BaselineCapture
from kinect_touch.pipeline.detector import TouchDetector

FRAME_BYTES = WIDTH * HEIGHT * 2
RGB_SCALE = 4
RGB_SIZE = (WIDTH // RGB_SCALE, HEIGHT // RGB_SCALE)
RGB_BYTES = RGB_SIZE[0] * RGB_SIZE[1] * 3
RGB_MAX_AGE = .16
CALIBRATION_FILE = HERE / "calibration.json"
DEFAULT_CORNERS = ((64, 48), (575, 48), (575, 431), (64, 431))
TARGETS = ((80, 80), (920, 80), (920, 920), (80, 920))
BOARD_CORNERS = ((0, 0), (BOARD_SIZE, 0), (BOARD_SIZE, BOARD_SIZE), (0, BOARD_SIZE))


class DepthStream:
    STARTUP_RETRY = 75
    STALL_SECONDS = 6
    SHUTDOWN_SECONDS = 5

    def __init__(self, *, rgb_assist=False):
        helper = HERE / "depth_stream"
        if not helper.is_file():
            raise OSError("Run 'sh build-depth.sh' before starting Kinect mode.")
        self.latest = None
        self.camera = Intrinsics()
        self.error = None
        self.rgb_assist = rgb_assist
        self.command = [str(helper), "--parent-pipe"]
        if rgb_assist:
            self.command.append("--rgb-assist")
        self.retry_at = None
        self.retried = False
        self._start()

    def _start(self):
        self.started = monotonic()
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        with self.process.stdout as pipe:
            head = pipe.readline(128)
            if not head:
                return
            try:
                header = head.decode("ascii").split()
                expected = ["KDP2" if self.rgb_assist else "KDP1", "640", "480"]
                tail = list(map(str, RGB_SIZE)) if self.rgb_assist else []
                if header[:3] != expected or header[7:] != tail or len(header) != 7 + len(tail):
                    raise ValueError("Unrecognized header")
                self.camera = Intrinsics.from_values(*map(float, header[3:7]))
            except ValueError:
                self.error = "Depth helper protocol mismatch. Run 'sh build-depth.sh' and relaunch."
                return
            color = None
            while len(data := pipe.read(FRAME_BYTES)) == FRAME_BYTES:
                image = np.frombuffer(data, dtype=np.uint16).reshape(HEIGHT, WIDTH)
                if not self.rgb_assist:
                    self.latest = (monotonic(), image)
                    continue
                flag = pipe.read(1)
                if flag == b"\x01":
                    payload = pipe.read(RGB_BYTES)
                    if len(payload) != RGB_BYTES:
                        break
                    color = (monotonic(), np.frombuffer(payload, dtype=np.uint8).reshape(RGB_SIZE[1], RGB_SIZE[0], 3))
                elif flag != b"\x00":
                    break
                now = monotonic()
                self.latest = (now, image, color if color and now - color[0] <= RGB_MAX_AGE else None)

    def read(self, now):
        return self.latest

    def failure(self, now):
        if self.error:
            return self.error
        if self.retry_at is not None:
            if now >= self.retry_at:
                self.retry_at = None
                try:
                    self._start()
                except OSError as error:
                    return f"Could not restart Kinect helper: {error}"
            return None

        result = self.process.poll()
        frame = self.latest
        stalled = now - (self.started if frame is None else frame[0]) > self.STALL_SECONDS
        if not self.retried and frame is None and (
            result == self.STARTUP_RETRY or (result is None and stalled)
        ):
            self.close()
            self.retried = True
            self.retry_at = monotonic() + .75
            print("Kinect startup failed. Retrying with a fresh helper.", file=sys.stderr)
            return None

        if result == self.STARTUP_RETRY:
            return "Kinect camera found but could not start. See the terminal for the driver error."
        if result is not None:
            return "Kinect stream stopped. Check the terminal, USB and 12 V power."
        if stalled:
            return "No Kinect depth for 6 seconds. Check USB and 12 V power."
        return None

    def close(self):
        process = self.process
        # EOF asks the helper to stop capture while the reader drains stdout.
        # Keep the device available to the shutdown code while it cancels USB transfers.
        process.stdin.close()
        if process.poll() is None:
            try:
                process.wait(timeout=self.SHUTDOWN_SECONDS)
            except subprocess.TimeoutExpired:
                print("Kinect shutdown is taking longer than expected. Signaling the helper.", file=sys.stderr)
                process.terminate()
                try:
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    print("Kinect helper did not finish USB cleanup. Stopping it.", file=sys.stderr)
                    process.kill()
                    process.wait()
        self.reader.join(timeout=1)


class SimulatedStream:
    """A sloped wall with a movable foreground blob and real depth units."""

    def __init__(self):
        self.camera = Intrinsics()
        y, x = np.indices((HEIGHT, WIDTH), dtype=np.float32)
        ray_x = (x - self.camera.cx) / self.camera.fx
        ray_y = (y - self.camera.cy) / self.camera.fy
        self.factor = (.95 - .29 * ray_x + .12 * ray_y) / np.sqrt(.95**2 + .29**2 + .12**2)
        self.background = np.rint(1150 / self.factor).astype(np.uint16)
        self.cursor = None
        self.pressed = False
        self.last = -1.0

    def read(self, now):
        if now - self.last < 1 / 31:
            return None
        self.last = now
        image = self.background.copy()
        if self.cursor is not None:
            u, v = np.rint(self.cursor).astype(int)
            if 0 <= u < WIDTH and 0 <= v < HEIGHT:
                distance = 20 if self.pressed else 110
                level = int(round(distance / self.factor[v, u]))
                intrusion = cv2.circle(np.zeros_like(image), (u, v), 11, level, -1)
                image -= intrusion
        return now, image

    def close(self):
        pass

    def failure(self, now):
        return None


class ReplayStream:
    """Loop a KinectTouch-format recording with its original frame cadence."""

    def __init__(self, path):
        with np.load(path, allow_pickle=False) as recording:
            self.images = recording["depth"]
            stamps = np.asarray(recording["ts_ms"], dtype=float) / 1000
            calibration = recording["intrinsics"] if "intrinsics" in recording else None
        if (self.images.ndim != 3 or self.images.shape[1:] != (HEIGHT, WIDTH)
                or self.images.dtype != np.uint16 or not len(self.images)
                or stamps.shape != (len(self.images),) or not np.isfinite(stamps).all()
                or np.any(np.diff(stamps) < 0)):
            raise ValueError("A recording needs uint16 depth[N,480,640] and ordered ts_ms[N].")
        self.times = np.r_[0, np.cumsum(np.clip(np.diff(stamps), .01, .4))]
        self.camera = Intrinsics()
        if calibration is not None and calibration.shape == (4,):
            self.camera = Intrinsics.from_values(*calibration)
        self.index = 0
        self.start = None

    def read(self, now):
        if self.start is None:
            self.start = now
        count = len(self.images)
        if self.index == count:
            if now - self.start < self.times[-1] + .6:
                return None
            self.index = 0
            self.start = now
        if now - self.start < self.times[self.index]:
            return None
        self.index = max(self.index, np.searchsorted(self.times, now - self.start, side="right") - 1)
        image = self.images[self.index]
        self.index += 1
        return now, image

    def close(self):
        pass

    def failure(self, now):
        return None


class DepthRecording:
    """Record raw frames and the live detector state alongside them."""

    def __init__(self):
        self.frames = []
        self.trace = []
        self.rgb_trace = []
        self.baselines = []
        self.corners = []
        self._mapping = None
        self._baseline = None

    def append(self, frame, surface, events, *, aligning=False, air_gesture=""):
        index = len(self.frames)
        self.frames.append(frame)
        baseline = surface.baseline.baseline
        if baseline is not self._baseline:
            self.baselines.append((index, baseline))
            self._baseline = baseline
        if surface.mapping is not self._mapping:
            self.corners.append((index, surface.mapping.corners, surface.calibrated))
            self._mapping = surface.mapping

        point = None if aligning else surface.pointer
        if aligning:
            state = "aligning"
        elif surface.wall_shift_frames:
            state = "wall_shift"
        elif surface.capturing:
            state = "capturing"
        else:
            state = surface.pointer_kind
        position = (point.u, point.v, point.height) if point else (np.nan, np.nan, np.nan)
        self.trace.append((state, position, ",".join(event.kind for event in events),
                           surface.confidence if point else 0, air_gesture))
        if surface.rgb is not None:
            self.rgb_trace.append((surface.rgb.status, surface.rgb_decision))

    def diagnostics(self):
        states, positions, events, confidence, air_gestures = zip(*self.trace)
        baseline_frames = [index for index, _ in self.baselines]
        # A zero image marks that a recapture cleared the previous baseline.
        baseline_depth = [depth if depth is not None else np.zeros((HEIGHT, WIDTH), np.uint16)
                          for _, depth in self.baselines]
        fields = {
            "detector_state": np.asarray(states),
            "pointer_uv_height_mm": np.asarray(positions, dtype=np.float32),
            "pointer_events": np.asarray(events),
            "pointer_confidence": np.asarray(confidence, dtype=np.uint8),
            "air_gesture": np.asarray(air_gestures),
            "baseline_frames": np.asarray(baseline_frames, dtype=np.int32),
            "baseline_depth": np.asarray(baseline_depth, dtype=np.uint16).reshape(-1, HEIGHT, WIDTH),
            "mapping_frames": np.asarray([index for index, _, _ in self.corners], dtype=np.int32),
            "mapping_corners": np.stack([corners for _, corners, _ in self.corners]),
            "mapping_calibrated": np.asarray([calibrated for _, _, calibrated in self.corners]),
        }
        if self.rgb_trace:
            rgb_status, rgb_decisions = zip(*self.rgb_trace)
            fields["rgb_status"] = np.asarray(rgb_status)
            fields["rgb_decision"] = np.asarray(rgb_decisions)
        return fields


def write_recording(path, frames, camera, *, diagnostics=None):
    path.parent.mkdir(exist_ok=True, parents=True)
    intrinsics = (
        (camera.cx, camera.cy, camera.fx, camera.fy) if camera.calibrated else
        (camera.cx, camera.cy, 0, 0)
    )
    with TemporaryDirectory(dir=path.parent, prefix=".recording-") as directory:
        temporary = Path(directory) / "depth.npz"
        np.savez_compressed(temporary, depth=np.stack([frame[1] for frame in frames]),
                            ts_ms=np.array([frame[0] for frame in frames]) * 1000,
                            intrinsics=intrinsics, **(diagnostics or {}))
        temporary.replace(path)


class SurfaceMap:
    def __init__(self, corners=DEFAULT_CORNERS, targets=BOARD_CORNERS):
        points = np.asarray(corners, dtype=np.float32)
        if points.shape != (4, 2) or not np.isfinite(points).all():
            raise ValueError("Choose four camera points.")
        if np.any(points < 0) or np.any(points >= (WIDTH, HEIGHT)):
            raise ValueError("Choose points inside the depth image.")
        if not cv2.isContourConvex(points) or abs(cv2.contourArea(points)) < 1000:
            raise ValueError("Use four distinct points around the edge of the surface.")
        self.corners = points
        self.matrix = cv2.getPerspectiveTransform(points, np.asarray(targets, dtype=np.float32))
        try:
            self.inverse = np.linalg.inv(self.matrix)
        except np.linalg.LinAlgError as error:
            raise ValueError("This alignment cannot be mapped onto the wall.") from error
        homogeneous = np.column_stack((BOARD_CORNERS, np.ones(4))) @ self.inverse.T
        scale = homogeneous[:, 2]
        if np.any(abs(scale) < 1e-8) or not np.all(np.sign(scale) == np.sign(scale[0])):
            raise ValueError("Move the alignment points closer to the actual projected corners.")
        outer = homogeneous[:, :2] / scale[:, None]
        if np.any(abs(outer) > 4 * WIDTH):
            raise ValueError("The projected area extends too far outside the camera.")
        self.outline = outer
        self.mask = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
        cv2.fillConvexPoly(self.mask, np.rint(outer).astype(np.int32), 255)

    @staticmethod
    def _map(x, y, matrix):
        a, b, scale = matrix @ (x, y, 1)
        return (float(a / scale), float(b / scale)) if abs(scale) > 1e-8 else (-1.0, -1.0)

    def project(self, x, y):
        return self._map(x, y, self.matrix)

    def camera_pixel(self, x, y):
        return self._map(x, y, self.inverse)


@dataclass(frozen=True)
class SurfacePoint:
    x: float
    y: float
    u: float
    v: float
    xyz: tuple[float, float, float]
    height: float
    score: int = 0
    rgb_strength: float | None = None


class ContactTracker:
    """Track one contact through dilation, temporary depth holes and release."""

    RELEASE_SECONDS = .10
    STARVE_SECONDS = .40
    MAX_JUMP = 55  # camera pixels between consecutive frames

    def __init__(self):
        self.point = None
        self.pending = None
        self.seen = 0.0
        self.frame_time = None
        self.position = None

    def cancel(self, now):
        event = self._event("cancel", now) if self.point is not None else None
        self.__init__()
        return event

    def _event(self, kind, now):
        return PointerEvent(kind, *self.position, now, "depth")

    @staticmethod
    def _nearest(points, previous):
        if not points:
            return None
        if previous is None:
            return points[0]
        ranked = [(hypot(p.u - previous.u, p.v - previous.v), i, p) for i, p in enumerate(points)]
        distance, _, point = min(ranked)
        return point if distance <= ContactTracker.MAX_JUMP else None

    def update(self, contacts, holds, now):
        events = []
        if self.frame_time is not None and now - self.frame_time > self.STARVE_SECONDS:
            if event := self.cancel(now):
                events.append(event)
        self.frame_time = now
        if self.point is not None:
            candidate = self._nearest(contacts, self.point) or self._nearest(holds, self.point)
            if candidate is not None:
                self.point, self.seen = candidate, now
                x, y = self.position
                self.position = (x + .72 * (candidate.x - x), y + .72 * (candidate.y - y))
                events.append(self._event("move", now))
            elif now - self.seen >= self.RELEASE_SECONDS:
                events.append(self._event("up", self.seen))
                self.point = self.position = None
            return events

        if self.pending is not None and now - self.seen <= .14:
            candidate = self._nearest(contacts, self.pending)
            if candidate is not None:
                self.point, self.pending, self.seen = candidate, None, now
                self.position = (candidate.x, candidate.y)
                events.append(self._event("down", now))
                return events
        self.pending = self._nearest(contacts, None)
        self.seen = now
        return events


class SurfaceInput:
    WALL_SHIFT_FRAMES = 3
    WALL_SHIFT_FRACTION = .20

    def __init__(self, calibration=CALIBRATION_FILE, *, rgb_assist=False):
        config = Config()
        config.pipeline.temporal_median_frames = 1
        self.baseline = BaselineCapture(config.baseline)
        self.detector = TouchDetector(config)
        self.tracker = ContactTracker()
        self.rgb = RgbAssist(RGB_SCALE) if rgb_assist else None
        self.rgb_decision = ""
        self.confidence = 0
        self.path = calibration
        self.mapping = SurfaceMap()
        self.calibrated = False
        if self.path is not None and self.path.is_file():
            try:
                self._load(json.loads(self.path.read_text()))
            except (OSError, ValueError, TypeError, OverflowError) as error:
                print(f"Ignoring projector calibration: {error}", file=sys.stderr)
        self.frames = deque(maxlen=3)
        self.camera = Intrinsics()
        self.capturing = False
        self.capture_hint = ""
        self.wall_shift_frames = 0
        self.plane = None
        self.factors = None
        self.valid = None
        self.baseline_float = None
        self.pointer = None
        self.pointer_kind = "idle"
        self.touching = False

    def _load(self, points):
        self.mapping = SurfaceMap(points, TARGETS)
        self.calibrated = True

    def clear_interaction(self, now):
        self.confidence = 0
        self.frames.clear()
        self.detector.reset()
        self.pointer = None
        self.pointer_kind = "idle"
        self.touching = False
        return self.tracker.cancel(now)

    def start_baseline(self, now, *, capture=True):
        event = self.clear_interaction(now)
        self.baseline.reset()
        self.plane = self.factors = self.valid = self.baseline_float = None
        self.capturing = capture
        self.capture_hint = ""
        self.wall_shift_frames = 0
        if self.rgb is not None:
            self.rgb.reset()
        return event

    def save_mapping(self, points, now):
        mapping = SurfaceMap(points, TARGETS)
        if self.path is not None:
            with TemporaryDirectory(dir=self.path.parent, prefix=".calibration-") as directory:
                path = Path(directory) / self.path.name
                path.write_text(json.dumps(mapping.corners.tolist(), indent=2) + "\n")
                path.replace(self.path)
        self.mapping, self.calibrated = mapping, True
        return self.start_baseline(now, capture=False)

    def _capture(self, depth):
        valid = (self.mapping.mask > 0) & (depth >= MIN_DEPTH) & (depth <= MAX_WALL_DEPTH)
        if np.count_nonzero(valid) < .4 * cv2.countNonZero(self.mapping.mask):
            self.capture_hint = "Low depth coverage. Aim the Kinect at the full surface."
            self.baseline.reset()
            return
        self.capture_hint = ""
        if not self.baseline.add(np.where(valid, depth, 0)):
            return
        self.capturing = False
        self.baseline_float = self.baseline.baseline.astype(np.float32)
        self.valid = self.baseline_float > 0
        self.plane = WallPlane.fit(self.baseline.baseline, self.mapping.mask, self.camera)
        if self.plane is not None:
            self.factors = self.plane.ray_factors(self.camera).astype(np.float32)
            distance = self.plane.offset - self.baseline_float * self.factors
            self.valid &= (np.abs(distance) <= 52) & (self.factors > .14)
        wall_range = self.baseline.stats.median_mm
        # Kinect v1 depth steps grow roughly with the square of distance.
        # Keep the press threshold above one step at the captured wall.
        minimum = max(10, min(24, round(4e-6 * wall_range**2)))
        thresholds = self.detector.cfg.pipeline
        thresholds.touch_enter_min_mm = minimum
        thresholds.touch_hold_min_mm = minimum - 5

    def _wall_shifted(self, depth):
        grid = np.s_[::4, ::4]
        sampled = depth[grid]
        baseline_valid = self.valid[grid]
        valid = baseline_valid & (sampled >= MIN_DEPTH) & (sampled <= MAX_WALL_DEPTH)
        error = np.abs(self.baseline_float[grid] - sampled)
        if self.factors is not None:
            error *= self.factors[grid]
        # A shallow depth change over this much wall can break up into
        # fingertip sized patches. The upper bound excludes raised objects.
        changed = valid & (error >= 15) & (error <= 45)
        return np.count_nonzero(changed) > self.WALL_SHIFT_FRACTION * np.count_nonzero(baseline_valid)

    def _point(self, blob, now, hover):
        u, v = blob.cx, blob.cy
        iy, ix = min(HEIGHT - 1, round(v)), min(WIDTH - 1, round(u))
        base = float(self.baseline.baseline[iy, ix])
        if not self.valid[iy, ix]:
            x, y, w, h = blob.bbox
            patch = self.baseline.baseline[y:y + h, x:x + w]
            values = patch[self.valid[y:y + h, x:x + w]]
            if not len(values):
                return None
            base = float(np.median(values))
        ray = self.camera.ray(u, v)
        factor = 1.0 if self.plane is None else -float(self.plane.normal @ ray)
        if factor <= .14:
            return None
        xyz = ray * (base - blob.intrusion_mm / factor)
        height = blob.intrusion_mm
        if self.plane is not None:
            foot = self.plane.flatten(xyz)
            if foot[2] > 0:
                u, v = self.camera.pixel(foot)
        x, y = self.mapping.project(u, v)
        if -25 <= x <= BOARD_SIZE + 25 and -25 <= y <= BOARD_SIZE + 25:
            strength = self.rgb.strength(blob) if self.rgb else None
            # The depth band and shape filters already accepted this blob.
            score = 40 + 15 * min(blob.area / 150, 1)
            if self.plane is not None:
                score += 10 * self.plane.support
            last = self.tracker.frame_time
            if (last is not None and 0 < now - last <= .16 and self.pointer is not None
                    and (self.pointer_kind == "hover") == hover):
                distance = hypot(self.pointer.u - blob.cx, self.pointer.v - blob.cy)
                score += 10 * max(0, 1 - distance / ContactTracker.MAX_JUMP)
            if strength is not None:
                score += 15 + 5 * strength if strength >= 1 else -20 * (1 - strength)
            return SurfacePoint(x, y, blob.cx, blob.cy, tuple(map(float, xyz)), height,
                                min(100, round(score)), strength)
        return None

    def _points(self, blobs, now, *, hover=False):
        points = [point for blob in blobs if hover or blob.area <= 1800
                  if (point := self._point(blob, now, hover)) is not None]
        return sorted(points, key=lambda p: p.score, reverse=True)

    def update(self, depth, now, camera, *, color=None):
        self.rgb_decision = ""
        if self.capturing:
            self.camera = camera
            self._capture(depth)
            if self.rgb is not None:
                self.rgb.capture(color, complete=self.baseline.ready,
                                 progress=self.baseline.progress, mask=self.mapping.mask)
            return []
        if self.rgb is not None:
            self.rgb.update(color)
        if not self.baseline.ready:
            return []
        if self.wall_shift_frames >= self.WALL_SHIFT_FRAMES:
            return []
        if self._wall_shifted(depth):
            self.wall_shift_frames += 1
            if self.wall_shift_frames >= self.WALL_SHIFT_FRAMES:
                self.capture_hint = "Wall depth changed. Clear the surface and press B."
            event = self.clear_interaction(now)
            return [event] if event else []
        self.wall_shift_frames = 0
        if self.tracker.frame_time is not None and now - self.tracker.frame_time > self.tracker.STARVE_SECONDS:
            self.frames.clear()
        self.frames.append(depth)
        if len(self.frames) == 3:
            a, b, c = self.frames
            depth = cv2.max(cv2.min(a, b), cv2.min(cv2.max(a, b), c))
        valid = self.valid & (depth >= MIN_DEPTH) & (depth < 10000)
        if self.factors is not None:
            # Scale ray-aligned change by the cosine to the wall normal. Use
            # the captured surface as zero to preserve per-pixel correction.
            depth = np.clip(np.rint(self.baseline_float + (depth - self.baseline_float) * self.factors),
                            1, 9999).astype(np.uint16)
        depth = np.where(valid, depth, 0)
        detected = self.detector.process(depth, self.baseline.baseline)
        contacts = self._points(detected.touches, now)
        if self.rgb and self.tracker.point is None and contacts:
            contacts = [p for p in contacts if p.rgb_strength is None or p.rgb_strength >= 1]
            if not contacts:
                self.rgb_decision = "Rejected depth candidate"
        holds = self._points(detected.holds, now) if self.tracker.point is not None else []
        hovers = self._points(detected.hovers, now, hover=True)
        events = self.tracker.update(contacts, holds, now)
        hover = hovers[0] if hovers else None
        self.pointer = self.tracker.point or (contacts[0] if contacts else hover)
        self.touching = self.tracker.point is not None
        if self.touching:
            self.pointer_kind = "contact"
        elif contacts:
            self.pointer_kind = "candidate"
        elif hover is not None:
            self.pointer_kind = "hover"
        else:
            self.pointer_kind = "idle"
        point = self.pointer
        self.confidence = point.score if point else 0
        if self.touching and self.tracker.seen < now:
            self.confidence = round(self.confidence * max(0, 1 - (now - self.tracker.seen) / self.tracker.RELEASE_SECONDS))
        if point and self.rgb and not self.rgb_decision:
            if point.rgb_strength is None:
                self.rgb_decision = "Depth only"
            elif point.rgb_strength >= 1:
                self.rgb_decision = "Matched"
            else:
                self.rgb_decision = "Weak color match"
        return events

    def cancel_stale(self, now):
        last = self.tracker.frame_time
        if last is not None and now - last > self.tracker.STARVE_SECONDS:
            return self.clear_interaction(now)
        return None


def depth_preview(depth, size):
    samples = depth[::8, ::8]
    valid = samples[(samples >= MIN_DEPTH) & (samples < 10000)]
    if valid.size:
        near, far = np.percentile(valid, (10, 90))
        near, far = near - 250, far + 150
        brightness = np.clip((far - depth) * 235 / (far - near), 25, 235)
        gray = np.where((depth >= MIN_DEPTH) & (depth < 10000), brightness, 0).astype(np.uint8)
    else:
        gray = np.zeros_like(depth, dtype=np.uint8)
    gray = cv2.resize(gray, size, interpolation=cv2.INTER_NEAREST)
    rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    return f"P6\n{size[0]} {size[1]}\n255\n".encode("ascii") + rgb.tobytes()
