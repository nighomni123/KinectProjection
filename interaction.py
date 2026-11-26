"""A shared gesture interpreter for wall contacts and mouse input."""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot


@dataclass(frozen=True)
class PointerEvent:
    kind: str
    x: float
    y: float
    time: float
    source: str


@dataclass(frozen=True)
class Target:
    widget: str | None
    part: str


@dataclass(frozen=True)
class Gesture:
    kind: str
    target: Target
    x: float
    y: float
    dx: float = 0
    dy: float = 0


def swipe_direction(dx, dy):
    if abs(dx) >= abs(dy) * 1.35:
        return "swipe_right" if dx > 0 else "swipe_left"
    if abs(dy) >= abs(dx) * 1.35:
        return "swipe_down" if dy > 0 else "swipe_up"
    return None


class HoverSwipes:
    """Dwell over a bound control, then follow one continuous hand movement."""

    def __init__(self):
        self.cooldown_until = 0
        self.reset()

    def reset(self):
        self.target = self.origin = self.last = None
        self.armed = False
        self.motion_start = None
        self.path_length = 0

    def feed(self, x, y, now, target):
        if now < self.cooldown_until:
            return None
        if self.last:
            lx, ly, stamp = self.last
            step = hypot(x - lx, y - ly)
            if not 0 < now - stamp <= .16 or step > 65:
                self.reset()
        if self.origin is None:
            if target is not None:
                self.origin = self.last = (x, y, now)
                self.target = target
            return None
        self.last = (x, y, now)
        ox, oy, start = self.origin
        dx, dy = x - ox, y - oy
        distance = hypot(dx, dy)
        if not self.armed:
            if target != self.target or distance > 20:
                self.reset()
            elif now - start >= .30:
                self.armed = True
                self.origin = self.last
            return None
        if self.motion_start is None:
            if distance < 20:
                return None
            self.motion_start = now
            self.path_length = distance
        else:
            self.path_length += step
        if now - self.motion_start > .70:
            self.reset()
        elif distance >= 100:
            kind = swipe_direction(dx, dy)
            gesture = None
            if kind and distance >= .80 * self.path_length:
                gesture = Gesture(kind, self.target, x, y, dx, dy)
                self.cooldown_until = now + .65
            self.reset()
            return gesture
        return None


@dataclass
class _Contact:
    target: Target
    start: PointerEvent
    allow_drag: bool
    allow_hold: bool
    travel: float = 0.0
    dragging: bool = False
    held: bool = False


class Gestures:
    HOLD_SECONDS = .65
    DRAG_DISTANCE = 16
    SWIPE_DISTANCE = 65
    SWIPE_SECONDS = .9

    def __init__(self):
        self.contact: _Contact | None = None

    def feed(self, event, target=None, *, drag=False, hold=False):
        current = self.contact
        if event.kind == "down":
            if current is None and target is not None:
                self.contact = _Contact(target, event, drag, hold)
            return None
        if current is None or current.start.source != event.source:
            return None
        if event.kind == "cancel":
            self.contact = None
            return Gesture("cancel", current.target, event.x, event.y)

        dx, dy = event.x - current.start.x, event.y - current.start.y
        distance = hypot(dx, dy)
        current.travel = max(current.travel, distance)
        elapsed = event.time - current.start.time
        if event.kind == "up":
            self.contact = None
        if current.held:
            return None
        if current.allow_drag and (current.dragging or current.travel >= self.DRAG_DISTANCE):
            current.dragging = True
            kind = "drop" if event.kind == "up" else "drag"
            return Gesture(kind, current.target, event.x, event.y, dx, dy)
        if current.allow_hold and elapsed >= self.HOLD_SECONDS and current.travel < self.DRAG_DISTANCE:
            current.held = True
            return Gesture("hold", current.target, current.start.x, current.start.y)
        if event.kind == "up":
            if not current.allow_drag and distance >= self.SWIPE_DISTANCE and elapsed <= self.SWIPE_SECONDS:
                kind = swipe_direction(dx, dy)
                return Gesture(kind, current.target, event.x, event.y, dx, dy) if kind else None
            if current.travel < self.DRAG_DISTANCE and (not current.allow_hold or elapsed < self.HOLD_SECONDS):
                return Gesture("tap", current.target, current.start.x, current.start.y)
        return None
