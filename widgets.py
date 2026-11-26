"""Validated widget definitions, gesture bindings, and local widget actions."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


ACCENTS = {
    "mint": "#83edbd", "amber": "#f4c984", "blue": "#9fc9ff",
    "rose": "#f7aeba", "lilac": "#cfb6fc",
}
KINDS = {
    "timer": {"seconds": 60},
    "stopwatch": {},
    "counter": {"start": 0, "step": 1},
    "checklist": {"items": ["First item"]},
    "note": {"text": ""},
    "clock": {},
    "progress": {"goal": 8, "unit": "done"},
    "picker": {"items": ["Option A", "Option B"]},
}
GESTURES = {"tap", "hold", "drag", "swipe_left", "swipe_right", "swipe_up", "swipe_down"}
ACTION_HINTS = {
    "toggle": "start or pause", "reset": "reset", "increment": "add one step",
    "decrement": "subtract one step", "check": "check or uncheck", "pick": "pick an option",
    "move": "move the widget", "resize": "resize the widget",
}
GESTURE_HINTS = {
    "tap": "Tap", "hold": "Hold", "drag": "Drag", "swipe_left": "Swipe left on",
    "swipe_right": "Swipe right on", "swipe_up": "Swipe up on", "swipe_down": "Swipe down on",
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,23}\Z")


@dataclass(frozen=True)
class Component:
    id: str
    kind: str
    label: str
    settings: dict


@dataclass(frozen=True)
class Binding:
    target: str
    gesture: str
    action: str
    instruction: str


@dataclass(frozen=True)
class Recipe:
    title: str
    accent: str
    components: tuple[Component, ...]
    bindings: tuple[Binding, ...]

    def binding(self, target, gesture):
        return next((b for b in self.bindings if b.target == target and b.gesture == gesture), None)

    def definition(self):
        return {
            "version": 1, "title": self.title, "accent": self.accent,
            "components": [
                {"id": c.id, "kind": c.kind, "label": c.label, "settings": c.settings}
                for c in self.components
            ],
        }


def _text(value, name, limit, *, empty=False):
    if not isinstance(value, str) or len(value.strip()) > limit or (not empty and not value.strip()):
        raise ValueError(f"{name} must be text up to {limit} characters.")
    return value.strip()


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    return value


def _component(raw):
    if not isinstance(raw, dict):
        raise ValueError("Each component must be an object.")
    key, kind = raw.get("id"), raw.get("kind")
    if not isinstance(key, str) or not IDENTIFIER.fullmatch(key) or key == "widget":
        raise ValueError("Components need distinct lowercase IDs such as 'tea_timer'.")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("Choose a supported widget component.")
    label = _text(raw.get("label", kind.title()), "Component label", 48)
    supplied = raw.get("settings", {})
    if not isinstance(supplied, dict) or set(supplied) - set(KINDS[kind]):
        raise ValueError(f"Unsupported settings for {kind}.")
    settings = KINDS[kind] | supplied
    if kind == "timer":
        _integer(settings["seconds"], "Timer duration", 1, 86400)
    elif kind == "counter":
        _integer(settings["start"], "Starting count", -1000000, 1000000)
        _integer(settings["step"], "Counter step", 1, 10000)
    elif kind == "progress":
        _integer(settings["goal"], "Goal", 1, 10000)
        settings["unit"] = _text(settings["unit"], "Goal unit", 24)
    elif kind in {"checklist", "picker"}:
        items = settings["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            raise ValueError(f"{kind.title()} needs one to eight items.")
        settings["items"] = [_text(item, "Item", 72) for item in items]
    elif kind == "note":
        settings["text"] = _text(settings["text"], "Note", 340, empty=True)
    return Component(key, kind, label, settings)


def controls(component):
    """The actions and default gestures exposed by each named control."""
    kind = component.kind
    if kind in {"timer", "stopwatch"}:
        return {"face": {"tap": "toggle", "hold": "reset"}, "reset": {"tap": "reset"}}
    if kind in {"counter", "progress"}:
        return {
            "minus": {"tap": "decrement"}, "plus": {"tap": "increment"},
            "face": {"hold": "reset", "swipe_left": "decrement", "swipe_right": "increment"},
        }
    if kind == "checklist":
        return {f"item_{index}": {"tap": "check"} for index in range(len(component.settings["items"]))}
    if kind == "picker":
        return {"face": {"tap": "pick", "hold": "reset"}}
    return {}


def _targets(components):
    targets = {
        "widget.header": ("the widget header", {"drag": "move"}),
        "widget.grip": ("the resize handle in Edit mode", {"drag": "resize"}),
    }
    for component in components:
        for part, gestures in controls(component).items():
            name = component.label
            if part.startswith("item_"):
                index = int(part.removeprefix("item_"))
                name = component.settings["items"][index]
            elif part in {"plus", "minus", "reset"}:
                name = f"{name} {dict(plus='+', minus='−', reset='Reset')[part]}"
            targets[f"{component.id}.{part}"] = (name, gestures)
    return targets


def validate_recipe(raw, *, saved_bindings=None):
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("Use widget format version 1.")
    title = _text(raw.get("title"), "Widget title", 42)
    accent = raw.get("accent", "mint")
    if not isinstance(accent, str) or accent not in ACCENTS:
        raise ValueError("Choose a supported widget accent.")
    entries = raw.get("components")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 4:
        raise ValueError("A widget needs one to four components.")
    components = tuple(_component(entry) for entry in entries)
    if len({c.id for c in components}) != len(components):
        raise ValueError("Component IDs must be unique.")

    targets = _targets(components)
    bindings = {} if saved_bindings is not None else {
        (target, gesture): action
        for target, (_, defaults) in targets.items()
        for gesture, action in defaults.items()
    }
    supplied = saved_bindings if saved_bindings is not None else raw.get("bindings", [])
    if not isinstance(supplied, list) or len(supplied) > 100:
        raise ValueError("Gesture bindings must be a list with up to 100 entries.")
    seen = set()
    for entry in supplied:
        if not isinstance(entry, dict):
            raise ValueError("Each gesture binding must be an object.")
        target, gesture, action = (entry.get(key) for key in ("target", "gesture", "action"))
        if not all(isinstance(value, str) for value in (target, gesture, action)):
            raise ValueError("A gesture binding needs a target, gesture and action.")
        if target not in targets or gesture not in GESTURES:
            raise ValueError(f"Unknown control or gesture for {target}.")
        defaults = targets[target][1]
        if action not in defaults.values() or (gesture == "drag") != target.startswith("widget."):
            raise ValueError(f"Action {action} is unavailable for {gesture} on {target}.")
        if (target, gesture) in seen:
            raise ValueError(f"Duplicate {gesture} binding on {target}.")
        seen.add((target, gesture))
        bindings[target, gesture] = action

    return Recipe(title, accent, components, tuple(
        Binding(target, gesture, action,
                f"{GESTURE_HINTS[gesture]} {targets[target][0]} to {ACTION_HINTS[action]}.")
        for (target, gesture), action in bindings.items()
    ))


def make_recipe(kind, title=None, **settings):
    label = title or kind.title()
    entry = {"id": "main", "kind": kind, "label": label, "settings": settings}
    return validate_recipe({"version": 1, "title": label, "components": [entry]})


def timer_value(component, state, now):
    default = component.settings.get("seconds", 0)
    remaining = state.get("value", default)
    if "since" not in state:
        return remaining, False
    elapsed = max(0, now - state["since"])
    if component.kind == "timer":
        value = max(0, remaining - elapsed)
        return value, value > 0
    return remaining + elapsed, True


def count_value(component, state):
    return state.get("value", component.settings.get("start", 0))


def apply_action(component, target, action, state, now, choose):
    """Return updated state. The caller persists it before displaying the change."""
    if action == "reset":
        return {}
    if action == "toggle":
        value, running = timer_value(component, state, now)
        if running:
            return {"value": value}
        if component.kind == "timer" and value == 0:
            value = component.settings["seconds"]
        return {"value": value, "since": now}
    if action in {"increment", "decrement"}:
        direction = 1 if action == "increment" else -1
        new_value = count_value(component, state) + direction * component.settings.get("step", 1)
        minimum = 0 if component.kind == "progress" else -1000000
        maximum = component.settings.get("goal", 1000000)
        return {"value": min(maximum, max(minimum, new_value))}
    if action == "check":
        index = int(target.rsplit("_", 1)[1])
        checked = set(state.get("checked", []))
        return {"checked": sorted(checked ^ {index})}
    if action == "pick":
        return {"choice": choose(component.settings["items"])}
    raise ValueError(f"Unknown widget action {action}.")


def restore_state(recipe, raw):
    """Recover valid component state even if individual saved values are damaged."""
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return result
    for component in recipe.components:
        entry = raw.get(component.id)
        if not isinstance(entry, dict):
            continue
        value, since = entry.get("value"), entry.get("since")
        if component.kind in {"timer", "stopwatch"}:
            limit = component.settings.get("seconds", 1e10)
            if type(value) in (float, int) and 0 <= value <= limit:
                result[component.id] = {"value": value}
                if type(since) in (float, int) and 0 <= since <= 1e10:
                    result[component.id]["since"] = since
        elif component.kind in {"counter", "progress"}:
            minimum = 0 if component.kind == "progress" else -1000000
            if type(value) is int and minimum <= value <= component.settings.get("goal", 1000000):
                result[component.id] = {"value": value}
        elif component.kind == "checklist" and isinstance(entry.get("checked"), list):
            length = len(component.settings["items"])
            result[component.id] = {"checked": sorted({
                i for i in entry["checked"] if type(i) is int and 0 <= i < length
            })}
        elif component.kind == "picker" and entry.get("choice") in component.settings["items"]:
            result[component.id] = {"choice": entry["choice"]}
    return result
