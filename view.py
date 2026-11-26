"""Draw the wall and keep hit regions aligned with projected controls."""

from datetime import datetime
from math import ceil
import textwrap

from interaction import Target
from wall import BOARD_SIZE, HEADER_HEIGHT, Rect, component_height
from widgets import ACCENTS, ACTION_HINTS, count_value, timer_value

BG = "#070b0e"
PANEL = "#101a20"
OUTLINE = "#2b404a"
TEXT = "#edf5f5"
MUTED = "#a9c0c9"
MINT = "#83edbd"
FONT = "Helvetica Neue"


def duration(seconds):
    seconds = int(seconds)
    hours, total = divmod(seconds, 3600)
    minutes, seconds = divmod(total, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class WallView:
    def __init__(self, canvas):
        self.canvas = canvas
        self.width = self.height = 1
        self.scale = 1
        self.regions = []

    def point(self, x, y):
        return x * BOARD_SIZE / max(1, self.canvas.winfo_width()), y * BOARD_SIZE / max(1, self.canvas.winfo_height())

    def _xy(self, x, y):
        return x * self.width / BOARD_SIZE, y * self.height / BOARD_SIZE

    def target(self, x, y):
        for rectangle, target in reversed(self.regions):
            if rectangle.contains(x, y):
                return target
        return Target(None, "canvas")

    def _register(self, rect, widget, part):
        self.regions.append((rect, Target(widget, part)))

    def _box(self, rect, fill=PANEL, outline=OUTLINE, stroke=1):
        self.canvas.create_rectangle(*self._xy(rect.x, rect.y), *self._xy(rect.x + rect.w, rect.y + rect.h),
                                     fill=fill, outline=outline, width=stroke)

    def _line(self, x, y, x2, y2, color=OUTLINE, stroke=1):
        self.canvas.create_line(*self._xy(x, y), *self._xy(x2, y2), fill=color, width=stroke)

    def _font(self, size):
        return max(1, round(size * self.scale))

    def _columns(self, width, size):
        return max(2, int(width * self.width / BOARD_SIZE / (self._font(size) * .57)))

    def _text(self, x, y, value, size, color=TEXT, *, bold=False, anchor="w", width=None):
        value = str(value)
        if width is not None:
            columns = self._columns(width, size)
            value = value.replace("\n", " ")
            if len(value) > columns:
                value = value[:columns - 1].rstrip() + "…"
        self.canvas.create_text(*self._xy(x, y), text=value, fill=color, anchor=anchor,
                                font=(FONT, -self._font(size), "bold" if bold else "normal"))

    def _button(self, rect, label, widget, part, *, hot=False, accent=MINT, primary=False):
        live = hot or primary
        self._box(rect, accent if live else "#182931", accent if live else "#3a5360", 1)
        self._text(rect.x + rect.w / 2, rect.y + rect.h / 2, label, 19,
                   BG if live else TEXT, bold=True, anchor="center", width=rect.w - 6)
        self._register(rect, widget, part)

    def _component(self, instance, component, rect, accent, now, hot):
        x, y, width, height = rect.values()
        state = instance.state.get(component.id, {})
        kind = component.kind
        self._text(x, y + 14, component.label, 17, MUTED, bold=True, width=width)
        target = lambda name: f"{component.id}.{name}"
        is_hot = lambda name: hot == Target(instance.id, target(name))

        if kind == "checklist":
            items = component.settings["items"]
            row = (height - 32) / len(items)
            for i, item in enumerate(items):
                box = Rect(x, y + 32 + i * row, width, max(12, row - 4))
                part = target(f"item_{i}")
                checked = i in state.get("checked", [])
                color = accent if checked or is_hot(f"item_{i}") else OUTLINE
                self._box(box, "#1b3037" if is_hot(f"item_{i}") else "#142229", color)
                self._text(x + 11, box.y + box.h / 2, "✓" if checked else "□", 18, accent)
                self._text(x + 38, box.y + box.h / 2, item, 19, MUTED if checked else TEXT, width=width - 45)
                self._register(box, instance.id, part)
            return

        if kind == "note":
            columns = self._columns(width, 22)
            lines = max(1, int((height - 30) * self.height / BOARD_SIZE / (self._font(22) * 1.3)))
            note = textwrap.TextWrapper(width=columns, max_lines=lines, placeholder="…")
            self._text(x, y + 32, note.fill(component.settings["text"]), 22, anchor="nw")
            return

        if kind == "clock":
            stamp = datetime.fromtimestamp(now)
            self._text(x, y + 61, stamp.strftime("%H:%M"), 53, accent, bold=True, width=width)
            if height >= 122:
                self._text(x, y + 107, stamp.strftime("%a, %d %b"), 18, MUTED, width=width)
            return

        if kind in {"timer", "stopwatch"}:
            value, running = timer_value(component, state, now)
            face = Rect(x, y + 30, width - 82, height - 32)
            label = "Pause" if running else "Start" if value == 0 or not state else "Resume"
            if kind == "timer" and value == 0:
                label = "Time’s up · Restart"
            tap = instance.recipe.binding(target("face"), "tap")
            hold = instance.recipe.binding(target("face"), "hold")
            if tap is not None and tap.action != "toggle":
                label = f"Tap to {ACTION_HINTS[tap.action]}"
            elif tap is None:
                label = f"Hold to {ACTION_HINTS[hold.action]}" if hold is not None else ""
            self._box(face, "#20383c" if is_hot("face") else "#16272e", accent if running else OUTLINE)
            digit_size = 42 if value < 3600 and face.w >= 144 else 33
            self._text(face.x + 10, face.y + face.h * .40,
                       duration(ceil(value) if kind == "timer" else value),
                       digit_size, accent, bold=True, width=face.w - 17)
            self._text(face.x + 12, face.y + face.h * .80, label, 15, MUTED, width=face.w - 17)
            self._register(face, instance.id, target("face"))
            self._button(Rect(x + width - 74, y + 30, 74, height - 32), "Reset", instance.id,
                         target("reset"), hot=is_hot("reset"), accent=accent)
            return

        if kind == "picker":
            face = Rect(x, y + 30, width, height - 32)
            self._box(face, "#20383c" if is_hot("face") else "#16272e", OUTLINE)
            tap = instance.recipe.binding(target("face"), "tap")
            hold = instance.recipe.binding(target("face"), "hold")
            prompt = "Tap to choose" if tap is not None and tap.action == "pick" else "Choose an option"
            self._text(x + 12, face.y + face.h * .38, state.get("choice", prompt), 23,
                       accent, bold=True, width=width - 24)
            if hold is not None:
                self._text(x + 12, face.y + face.h * .79, f"Hold to {ACTION_HINTS[hold.action]}", 15,
                           MUTED, width=width - 24)
            self._register(face, instance.id, target("face"))
            return

        value = count_value(component, state)
        area_h = height - 36
        control = 49
        face = Rect(x + control + 7, y + 30, width - 2 * (control + 7), area_h)
        self._box(face, "#20383c" if is_hot("face") else "#16272e", OUTLINE)
        self._button(Rect(x, y + 30, control, area_h), "−", instance.id, target("minus"),
                     hot=is_hot("minus"), accent=accent)
        self._button(Rect(x + width - control, y + 30, control, area_h), "+", instance.id,
                     target("plus"), hot=is_hot("plus"), accent=accent)
        self._register(face, instance.id, target("face"))
        if kind == "progress":
            goal = component.settings["goal"]
            self._text(face.x + face.w / 2, face.y + face.h * .39, f"{value} / {goal}", 28,
                       accent, bold=True, anchor="center", width=face.w - 9)
            self._box(Rect(face.x + 8, face.y + face.h * .76, face.w - 16, 7), OUTLINE, "")
            if value:
                self._box(Rect(face.x + 8, face.y + face.h * .76, (face.w - 16) * value / goal, 7), accent, "")
            if face.h > 100:
                self._text(face.x + face.w / 2, face.y + face.h * .61, component.settings["unit"],
                           14, MUTED, anchor="center", width=face.w - 9)
        else:
            self._text(face.x + face.w / 2, face.y + face.h / 2, value, 50 if abs(value) < 1000 else 32,
                       accent, bold=True, anchor="center", width=face.w - 9)

    def _widget(self, instance, box, now, selected, hot, editing):
        x, y, width, height = box.values()
        accent = ACCENTS[instance.recipe.accent]
        self._box(box, PANEL, accent if selected else OUTLINE, 2 if selected else 1)
        self._register(box, instance.id, "widget.body")
        header = Rect(x + 1, y + 1, width - 2, HEADER_HEIGHT - 1)
        header_hot = hot == Target(instance.id, "widget.header")
        self._box(header, "#1d323a" if header_hot else "#16252d", "")
        self._box(Rect(x + 1, y + 1, 4, HEADER_HEIGHT - 1), accent, "")
        self._text(x + 17, y + HEADER_HEIGHT / 2, instance.recipe.title, 23, bold=True, width=width - 47)
        self._line(x + width - 23, y + 27, x + width - 13, y + 27, MUTED)
        self._line(x + width - 23, y + 35, x + width - 13, y + 35, MUTED)
        self._register(header, instance.id, "widget.header")

        top = y + HEADER_HEIGHT + 9
        components = instance.recipe.components
        weights = [component_height(c) for c in components]
        extra = (height - HEADER_HEIGHT - 15 - sum(weights)) / len(weights)
        for component, weight in zip(components, weights):
            slot = weight + extra
            self._component(instance, component, Rect(x + 13, top, width - 26, slot - 2), accent, now, hot)
            top += slot
        if editing and selected:
            grip = Rect(x + width - 31, y + height - 31, 31, 31)
            self._box(grip, accent, "")
            self._line(grip.x + 10, grip.y + 25, grip.x + 25, grip.y + 10, BG, 2)
            self._line(grip.x + 16, grip.y + 25, grip.x + 25, grip.y + 16, BG, 2)
            self._register(grip, instance.id, "widget.grip")

    def render(self, entries, now, *, selected=None, hot=None, editing=False, chrome=True,
               anchor=None, pointer=None, status="", preview=None):
        self.canvas.delete("all")
        self.regions.clear()
        self.width, self.height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        self.scale = min(self.width / 1000, self.height / 690)
        if not entries and chrome:
            self._text(500, 451, "An empty wall", 42, bold=True, anchor="center")
            self._text(500, 516, "Choose a spot, then open New to make a widget.", 22, MUTED, anchor="center")
        ordered = entries
        if preview:
            ordered = sorted(entries, key=lambda entry: entry.id == preview[0])
        for entry in ordered:
            box = preview[1] if preview and preview[0] == entry.id else entry.box
            self._widget(entry, box, now, entry.id == selected and chrome, hot, editing)

        if chrome:
            self._text(24, 53, "Wall canvas", 28, bold=True)
            toolbar = [("new", "New"), ("library", "Library"), ("edit", "Done" if editing else "Edit")]
            if editing and selected:
                toolbar.extend([("help", "Gestures"), ("remove", "Remove")])
            elif selected:
                toolbar.append(("help", "Gestures"))
            for i, (name, label) in enumerate(reversed(toolbar)):
                width = 103
                self._button(Rect(BOARD_SIZE - 24 - width - i * 112, 25, width, 55), label, None, name,
                             hot=hot == Target(None, name), primary=name == "new")
            self._line(24, 96, 976, 96)
        if anchor and chrome:
            x, y = anchor
            self._line(x - 10, y, x + 10, y, MINT, 2)
            self._line(x, y - 10, x, y + 10, MINT, 2)

        if pointer is not None:
            x, y, contact = pointer
            px, py = self._xy(x, y)
            radius = 8 if contact else 6
            self.canvas.create_oval(px - radius, py - radius, px + radius, py + radius,
                                    outline=MINT, fill=MINT if contact else "", width=2)
        if status:
            self._text(24, 968, status, 17, MUTED, width=952)

    def alignment(self, targets, current):
        self.canvas.delete("all")
        self.regions.clear()
        self.width, self.height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        self.scale = min(self.width / 1000, self.height / 690)
        for i, (x, y) in enumerate(targets):
            px, py = self._xy(x, y)
            color = MINT if i == current else "#426159"
            radius = max(8, round(15 * self.scale))
            self.canvas.create_oval(px - radius, py - radius, px + radius, py + radius, outline=color, width=2)
            self._line(x - 14, y, x + 14, y, color)
            self._line(x, y - 22, x, y + 22, color)
            self._text(x + (33 if x < 500 else -33), y, i + 1, 24, color, bold=True, anchor="center")
        self._text(500, 463, f"Align point {current + 1} of 4", 36, bold=True, anchor="center")
        self._text(500, 533, "Select the matching spot in the Kinect depth view.", 20, MUTED, anchor="center")
