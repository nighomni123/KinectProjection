"""Wall layout, reusable widget library, and durable instance state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import fcntl
import json
from math import isfinite
from pathlib import Path
from random import choice
import re
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

import yaml

from widgets import Recipe, apply_action, make_recipe, restore_state, validate_recipe


BOARD_SIZE = 1000
EDGE = 24
TOP = 110
BOTTOM = 926
HEADER_HEIGHT = 64
ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    def contains(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def overlaps(self, other, gap=0):
        return (
            self.x < other.x + other.w + gap and other.x < self.x + self.w + gap and
            self.y < other.y + other.h + gap and other.y < self.y + self.h + gap
        )

    def values(self):
        return [self.x, self.y, self.w, self.h]

    def bounded(self, recipe):
        min_width, min_height = minimum_size(recipe)
        width = min(BOARD_SIZE - EDGE * 2, max(min_width, self.w))
        height = min(BOTTOM - TOP, max(min_height, self.h))
        return Rect(min(BOARD_SIZE - EDGE - width, max(EDGE, self.x)),
                    min(BOTTOM - height, max(TOP, self.y)), width, height)


def component_height(component):
    if component.kind == "checklist":
        return 34 + 42 * len(component.settings["items"])
    if component.kind == "note":
        return 76 + len(component.settings["text"]) // 5
    if component.kind == "clock":
        return 106
    return 116


def minimum_size(recipe):
    height = HEADER_HEIGHT + 15 + sum(component_height(c) for c in recipe.components)
    if height > BOTTOM - TOP:
        raise ValueError("The widget has too many controls for a single tile. Use a shorter list or two widgets.")
    return 225, height


@dataclass(frozen=True)
class Instance:
    id: str
    widget_id: str
    recipe: Recipe
    box: Rect
    state: dict


def write_json(path, value):
    with TemporaryDirectory(dir=path.parent, prefix=".save-") as directory:
        temporary = Path(directory) / path.name
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                             encoding="utf-8")
        temporary.replace(path)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _identifier(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("Invalid saved widget identifier.")
    return value


class Wall:
    def __init__(self, directory, *, blank=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.library_path = self.directory / "widgets"
        self.library_path.mkdir(exist_ok=True)
        self.wall_path = self.directory / "wall.json"
        self.lock = (self.directory / ".lock").open("a")
        self.removed = []
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.entries = self._load()
            if not self.wall_path.exists():
                if blank:
                    self._commit([])
                else:
                    clock = self._new(make_recipe("clock", "Local time"), (52, 146))
                    timer = self._new(make_recipe("timer", "One-minute timer"), (454, 146))
                    self._commit([clock, timer])
        except BlockingIOError as error:
            self.close()
            raise ValueError("This wall is already open. Close it or use a different data directory.") from error
        except BaseException:
            self.close()
            raise

    def close(self):
        self.lock.close()

    def read_widget(self, widget_id):
        directory = self.library_path / _identifier(widget_id)
        raw = _read_json(directory / "widget.json")
        gesture_file = directory / "gestures.yaml"
        try:
            data = yaml.safe_load(gesture_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise ValueError(f"Could not read {gesture_file.name}: {error}") from error
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1 or not isinstance(data.get("bindings"), list)):
            raise ValueError("Use gesture file version 1 with a bindings list.")
        recipe = validate_recipe(raw, saved_bindings=data["bindings"])
        minimum_size(recipe)
        return recipe

    def library(self):
        items = []
        for directory in self.library_path.iterdir():
            if directory.is_dir() and ID_PATTERN.fullmatch(directory.name):
                try:
                    items.append((directory.name, self.read_widget(directory.name)))
                except (OSError, ValueError) as error:
                    print(f"Skipping widget {directory.name}: {error}", file=sys.stderr)
        return sorted(items, key=lambda item: (item[1].title.casefold(), item[0]))

    def _load(self):
        if not self.wall_path.exists():
            return []
        data = _read_json(self.wall_path)
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1 or not isinstance(data.get("widgets"), list)):
            raise ValueError(f"Invalid wall file at {self.wall_path}.")
        entries = []
        used = set()
        recipes = {}
        for raw in data["widgets"]:
            if not isinstance(raw, dict):
                raise ValueError("Invalid saved wall widget.")
            instance_id, widget_id = _identifier(raw.get("id")), _identifier(raw.get("widget"))
            box = raw.get("box")
            if (instance_id in used or not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not -10000 <= v <= 10000 for v in box)):
                raise ValueError("Invalid saved widget position or duplicate widget instance.")
            if widget_id not in recipes:
                recipes[widget_id] = self.read_widget(widget_id)
            recipe = recipes[widget_id]
            entries.append(Instance(instance_id, widget_id, recipe, Rect(*box).bounded(recipe),
                                    restore_state(recipe, raw.get("state"))))
            used.add(instance_id)
        return entries

    def _commit(self, entries):
        write_json(self.wall_path, {"version": 1, "widgets": [
            {"id": instance.id, "widget": instance.widget_id, "box": instance.box.values(),
             "state": instance.state}
            for instance in entries
        ]})
        self.entries = entries

    def _new(self, recipe, anchor):
        box = self.placement(recipe, anchor)
        widget_id = uuid4().hex
        with TemporaryDirectory(dir=self.library_path, prefix=".new-") as directory:
            bundle = Path(directory) / "widget"
            bundle.mkdir()
            write_json(bundle / "widget.json", recipe.definition())
            (bundle / "gestures.yaml").write_text(yaml.safe_dump(
                {"version": 1, "bindings": [asdict(b) for b in recipe.bindings]},
                sort_keys=False, allow_unicode=True, width=105,
            ), encoding="utf-8")
            bundle.replace(self.library_path / widget_id)
        return Instance(uuid4().hex, widget_id, recipe, box, {})

    def placement(self, recipe, anchor=None):
        width, minimum = minimum_size(recipe)
        width = width + (110 if len(recipe.components) == 1 else 180)
        height = min(BOTTOM - TOP, max(214, minimum + 28))
        if anchor is not None:
            return Rect(*anchor, width, height).bounded(recipe)
        for y in range(TOP + 18, BOTTOM, 44):
            for x in range(EDGE + 18, BOARD_SIZE - EDGE, 44):
                candidate = Rect(x, y, width, height)
                if (candidate == candidate.bounded(recipe)
                        and all(not candidate.overlaps(entry.box, gap=12) for entry in self.entries)):
                    return candidate
        offset = 28 * (len(self.entries) % 7)
        return Rect(EDGE + offset, TOP + offset, width, height).bounded(recipe)

    def add(self, recipe, anchor=None):
        instance = self._new(recipe, anchor)
        self._commit([*self.entries, instance])
        return instance

    def place_saved(self, widget_id, anchor=None):
        recipe = self.read_widget(widget_id)
        instance = Instance(uuid4().hex, widget_id, recipe, self.placement(recipe, anchor), {})
        self._commit([*self.entries, instance])
        return instance

    def get(self, instance_id):
        return next((entry for entry in self.entries if entry.id == instance_id), None)

    def move(self, instance_id, box):
        entry = self.get(instance_id)
        if entry:
            next_entry = replace(entry, box=box.bounded(entry.recipe))
            self._commit([item for item in self.entries if item.id != instance_id] + [next_entry])

    def action(self, instance_id, target, action, now):
        entry = self.get(instance_id)
        if entry is None or not isfinite(now):
            return
        component = next(c for c in entry.recipe.components if c.id == target.split(".", 1)[0])
        state = apply_action(component, target, action, entry.state.get(component.id, {}), now, choice)
        updated = replace(entry, state=entry.state | {component.id: state})
        self._commit([updated if item.id == entry.id else item for item in self.entries])

    def remove(self, instance_id):
        entry = self.get(instance_id)
        if entry is not None:
            index = self.entries.index(entry)
            self._commit([item for item in self.entries if item.id != instance_id])
            self.removed.append((index, entry))

    def restore_removed(self):
        if not self.removed:
            return None
        index, entry = self.removed[-1]
        entries = self.entries.copy()
        entries.insert(index, entry)
        self._commit(entries)
        self.removed.pop()
        return entry
