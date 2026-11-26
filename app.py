"""A free-form projected widget canvas for mouse and Kinect depth."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from queue import Empty, SimpleQueue
import sys
from threading import Thread
from time import monotonic, time
import tkinter as tk

from commands import examples, generate
from interaction import Gestures, HoverSwipes, PointerEvent
from model_dialog import show_model_settings
from model_settings import ModelPreferences
from view import BG, FONT, MINT, WallView
from wall import BOARD_SIZE, BOTTOM, EDGE, TOP, Rect, Wall

HERE = Path(__file__).resolve().parent
PREVIEW_SIZE = (512, 384)


class Projection:
    def __init__(self, root, wall, *, mode=None, replay=None, offline=False, rgb_assist=False, air_swipes=False):
        self.root, self.wall, self.mode, self.offline = root, wall, mode, offline
        self.rgb_assist = rgb_assist
        self.hover_swipes = HoverSwipes() if air_swipes else None
        self.model_preferences = ModelPreferences(wall.directory / "model.json")
        root.title("Kinect Projection")
        root.geometry("980x610+35+65")
        root.minsize(620, 400)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind_all("<KeyPress>", self.on_key)
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.invalidate())
        self.canvas.bind("<Motion>", self.mouse_motion)
        self.canvas.bind("<Leave>", self.mouse_leave)
        self.canvas.bind("<ButtonPress-1>", self.mouse_down)
        self.canvas.bind("<ButtonRelease-1>", self.mouse_up)
        self.view = WallView(self.canvas)
        self.gestures = Gestures()
        self.mouse = None
        self.anchor = None
        self.selected = None
        self.preview = None
        self.drag_origin = None
        self.editing = False
        self.chrome = True
        self.fullscreen = False
        self.dialog = None
        self.pending = False
        self.failed_command = ""
        self.results = SimpleQueue()
        self.message = ""
        self.message_until = 0
        self.error = ""
        self.dirty = True
        self.last_second = None

        self.stream = self.surface = self.depth_window = None
        self.frame = self.preview_frame = None
        self.preview_time = 0.0
        self.frozen = False
        self.points = None
        self.alignment_error = ""
        self.recording = None
        if mode:
            self.setup_depth(replay)
        self.tick()

    def invalidate(self):
        self.dirty = True

    def notify(self, message, duration=4):
        self.message, self.message_until = message, monotonic() + duration
        self.invalidate()

    def setup_depth(self, replay):
        import surface
        self.depth = surface
        self.surface = surface.SurfaceInput(
            None if self.mode == "simulate" else surface.CALIBRATION_FILE, rgb_assist=self.rgb_assist
        )
        if self.mode == "simulate":
            self.stream = surface.SimulatedStream()
            self.surface.start_baseline(monotonic())
        elif self.mode == "replay":
            self.surface.path = None
            self.stream = surface.ReplayStream(replay)
            self.surface.start_baseline(monotonic())
        else:
            self.stream = surface.DepthStream(rgb_assist=self.rgb_assist)
        self.show_depth()

    def show_depth(self):
        if self.depth_window is not None:
            self.depth_window.deiconify()
            return
        window = tk.Toplevel(self.root)
        window.title("Kinect · Depth view")
        window.geometry(f"+{max(0, self.root.winfo_screenwidth() - 535)}+70")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        self.depth_window = window
        self.depth_canvas = tk.Canvas(window, width=PREVIEW_SIZE[0], height=PREVIEW_SIZE[1],
                                      bg="black", highlightthickness=0)
        self.depth_canvas.pack()
        self.depth_image = self.depth_canvas.create_image(0, 0, anchor="nw")
        self.depth_canvas.bind("<Button-1>", self.pick_point)
        self.depth_status = tk.Label(window, wraplength=490, justify="left", anchor="w",
                                     padx=12, pady=8, height=4 if self.rgb_assist else 3)
        self.depth_status.pack(fill="x")

    def on_key(self, event):
        window = event.widget.winfo_toplevel()
        if window not in (self.root, self.depth_window):
            return
        key = event.keysym.lower()
        if key == "q":
            self.close()
        elif key == "f" or (key == "escape" and self.points is None and self.fullscreen):
            self.fullscreen = not self.fullscreen
            self.root.attributes("-fullscreen", self.fullscreen)
            self.invalidate()
        elif key == "escape":
            self.cancel_alignment()
        elif self.points is None and key in {"n", "l", "e", "h", "u", "delete", "backspace", "g", "m"}:
            action = {"n": "new", "l": "library", "e": "edit", "h": "chrome", "u": "restore",
                      "delete": "remove", "backspace": "remove", "g": "help", "m": "model"}[key]
            self.board_action(action)
        elif self.surface is not None:
            self.depth_key(key)

    def depth_key(self, key):
        if key == "d":
            if self.depth_window.state() == "withdrawn":
                self.show_depth()
            else:
                self.depth_window.withdraw()
        if self.stream is None:
            return
        now = monotonic()
        if key == "b" and self.points is None:
            self.handle_pointer(self.surface.start_baseline(now))
            self.invalidate()
        elif key == "c" and self.frame is not None:
            self.cancel_contact()
            self.surface.clear_interaction(now)
            self.points, self.frozen, self.alignment_error = [], False, ""
            self.show_depth()
            self.invalidate()
        elif key == "space" and self.points is not None:
            self.frozen = not self.frozen
        elif key == "r" and self.points is None:
            if self.recording is None:
                self.recording = self.depth.DepthRecording()
                self.notify("Recording up to 150 depth frames. Press R to finish.", duration=7)
            else:
                self.finish_recording()

    def pick_point(self, event):
        if self.points is None or self.frame is None:
            return
        x = event.x * self.depth.WIDTH / PREVIEW_SIZE[0]
        y = event.y * self.depth.HEIGHT / PREVIEW_SIZE[1]
        if not (0 <= x < self.depth.WIDTH and 0 <= y < self.depth.HEIGHT):
            return
        self.points.append((x, y))
        self.frozen = False
        if len(self.points) == len(self.depth.TARGETS):
            try:
                self.surface.save_mapping(self.points, monotonic())
            except (ValueError, OSError) as error:
                self.alignment_error = str(error)
                self.points.clear()
            else:
                self.cancel_alignment()
        self.invalidate()

    def cancel_alignment(self):
        self.points, self.frozen, self.alignment_error = None, False, ""
        self.invalidate()

    def mouse_motion(self, event):
        x, y = self.view.point(event.x, event.y)
        self.mouse = (x, y)
        if self.mode == "simulate":
            self.stream.cursor = self.surface.mapping.camera_pixel(x, y)
        else:
            self.handle_pointer(PointerEvent("move", x, y, monotonic(), "mouse"))
        self.invalidate()

    def mouse_leave(self, event):
        self.mouse = None
        if self.mode == "simulate":
            self.stream.cursor = None
        elif self.gestures.contact and self.gestures.contact.start.source == "mouse":
            # Tk sends releases to the grabbed canvas even if the mouse leaves.
            self.mouse = self.view.point(event.x, event.y)
        self.invalidate()

    def mouse_down(self, event):
        self.canvas.focus_set()
        self.mouse_motion(event)
        if self.mode == "simulate":
            self.stream.pressed = True
        else:
            self.handle_pointer(PointerEvent("down", *self.mouse, monotonic(), "mouse"))

    def mouse_up(self, event):
        self.mouse_motion(event)
        if self.mode == "simulate":
            self.stream.pressed = False
        else:
            self.handle_pointer(PointerEvent("up", *self.mouse, monotonic(), "mouse"))

    def cancel_contact(self):
        if self.gestures.contact is not None:
            start = self.gestures.contact.start
            self.handle_pointer(PointerEvent("cancel", start.x, start.y, monotonic(), start.source))

    def handle_pointer(self, event):
        if event is None or self.points is not None:
            return
        if event.kind == "down":
            if self.gestures.contact is not None:
                return
            target = self.view.target(event.x, event.y)
            entry = self.wall.get(target.widget)
            recipe = entry.recipe if entry else None
            drag = bool(recipe and recipe.binding(target.part, "drag"))
            hold = bool(recipe and recipe.binding(target.part, "hold"))
            self.drag_origin = entry.box if drag else None
            self.gestures.feed(event, target, drag=drag, hold=hold)
            return
        gesture = self.gestures.feed(event)
        if gesture is None:
            return
        try:
            self.apply_gesture(gesture)
        except (OSError, ValueError) as error:
            self.notify(f"Could not update wall · {error}", duration=8)
        self.invalidate()

    def apply_gesture(self, gesture):
        target = gesture.target
        if gesture.kind == "cancel":
            self.preview = self.drag_origin = None
            return
        if target.widget is None:
            if gesture.kind == "tap":
                if target.part == "canvas":
                    if EDGE <= gesture.x <= BOARD_SIZE - EDGE and TOP <= gesture.y <= BOTTOM:
                        self.anchor = (gesture.x, gesture.y)
                        self.selected = None
                else:
                    self.board_action(target.part)
            return
        entry = self.wall.get(target.widget)
        if entry is None:
            return
        self.selected = entry.id
        if gesture.kind in {"drag", "drop"}:
            binding = entry.recipe.binding(target.part, "drag")
            if binding is None or self.drag_origin is None:
                return
            x, y, w, h = self.drag_origin.values()
            if binding.action == "move":
                box = Rect(x + gesture.dx, y + gesture.dy, w, h)
            else:
                box = Rect(x, y, w + gesture.dx, h + gesture.dy)
            if gesture.kind == "drag":
                self.preview = (entry.id, box.bounded(entry.recipe))
            else:
                self.preview = self.drag_origin = None
                self.wall.move(entry.id, box)
            return
        if binding := entry.recipe.binding(target.part, gesture.kind):
            self.wall.action(entry.id, target.part, binding.action, time())

    def board_action(self, action):
        try:
            if action == "new":
                self.command_dialog()
            elif action == "library":
                self.library_dialog()
            elif action == "model":
                self.model_dialog()
            elif action == "edit":
                self.editing = not self.editing
                self.chrome = True
            elif action == "chrome":
                self.chrome = not self.chrome
                if not self.chrome:
                    self.editing = False
            elif action == "help":
                self.help_dialog()
            elif action == "remove" and self.selected is not None:
                self.cancel_contact()
                self.wall.remove(self.selected)
                self.selected = None
                self.notify("Widget removed from wall. Press U to restore it.")
            elif action == "restore":
                if entry := self.wall.restore_removed():
                    self.selected = entry.id
                    self.notify("Widget restored.")
        except (OSError, ValueError) as error:
            self.notify(str(error), duration=8)
        self.invalidate()

    def new_dialog(self, title):
        if self.dialog is not None:
            self.dialog.destroy()
        window = tk.Toplevel(self.root)
        self.dialog = window
        window.title(title)
        window.geometry("+70+115")
        window.protocol("WM_DELETE_WINDOW", self.close_dialog)
        window.bind("<Escape>", lambda event: self.close_dialog())
        return window

    def close_dialog(self):
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None

    def command_dialog(self):
        if self.pending:
            self.notify("A widget is already being generated.")
            return
        window = self.new_dialog("New widget")
        tk.Label(window, text="Describe the widget you want", font=(FONT, 16, "bold"),
                 anchor="w").pack(fill="x", padx=16, pady=(16, 5))
        entry = tk.Entry(window, width=54, font=(FONT, 14))
        entry.pack(fill="x", padx=16, pady=(0, 10))
        if self.failed_command:
            entry.insert(0, self.failed_command)
            entry.icursor("end")
        tk.Label(window, text=examples(), justify="left", anchor="w").pack(fill="x", padx=16)
        error = tk.Label(window, text="", fg="#a43c38", anchor="w", wraplength=470)
        error.pack(fill="x", padx=16)

        def submit(force_offline=False):
            command = entry.get().strip()
            if not command or len(command) > 600:
                error.config(text="Enter a widget command up to 600 characters.")
                return
            self.submit_command(command, force_offline)

        buttons = tk.Frame(window)
        buttons.pack(fill="x", padx=16, pady=(2, 14))
        tk.Button(buttons, text="Model settings", command=self.model_dialog).pack(side="left")
        tk.Button(buttons, text="Create", default="active", command=submit).pack(side="right")
        tk.Button(buttons, text="Use built-in templates", command=lambda: submit(True)).pack(side="right")
        window.bind("<Return>", lambda event: submit())
        entry.focus_force()

    def model_dialog(self):
        window = self.new_dialog("Model settings")

        def saved():
            self.close_dialog()
            self.notify("Model settings saved.")

        show_model_settings(window, self.model_preferences, saved)

    def submit_command(self, command, force_offline=False):
        self.close_dialog()
        self.pending = True
        self.failed_command = ""
        anchor = self.anchor

        def worker():
            try:
                recipe = generate(command, offline=force_offline or self.offline, preferences=self.model_preferences)
            except (OSError, ValueError) as error:
                self.results.put(("widget_error", str(error), command))
            else:
                self.results.put(("widget", recipe, anchor))

        Thread(target=worker, daemon=True).start()
        self.invalidate()

    def library_dialog(self):
        items = self.wall.library()
        window = self.new_dialog("Saved widgets")
        tk.Label(window, text="Saved widgets", font=(FONT, 16, "bold")).pack(anchor="w", padx=16, pady=12)
        if not items:
            tk.Label(window, text="Create a widget to add it to the library.").pack(padx=16, pady=(0, 16))
            return
        listing = tk.Listbox(window, width=46, height=min(9, len(items)), exportselection=False)
        listing.pack(fill="both", expand=True, padx=16)
        for _, recipe in items:
            listing.insert("end", recipe.title)
        listing.selection_set(0)

        def place():
            selected = listing.curselection()
            if not selected:
                return
            try:
                entry = self.wall.place_saved(items[selected[0]][0], self.anchor)
            except (ValueError, OSError) as error:
                self.notify(str(error), duration=8)
                return
            self.anchor, self.selected = None, entry.id
            self.close_dialog()
            self.notify(f"Placed {entry.recipe.title}.")

        listing.bind("<Double-Button-1>", lambda event: place())
        window.bind("<Return>", lambda event: place())
        tk.Button(window, text="Place on wall", command=place).pack(anchor="e", padx=16, pady=12)
        listing.focus_force()

    def help_dialog(self):
        entry = self.wall.get(self.selected)
        if entry is None:
            self.notify("Tap a widget to see its gesture instructions.")
            return
        window = self.new_dialog("Widget gestures")
        tk.Label(window, text=entry.recipe.title, font=(FONT, 16, "bold")).pack(anchor="w", padx=16, pady=12)
        body = tk.Text(window, width=53, height=min(15, max(3, len(entry.recipe.bindings) + 1)),
                       wrap="word", padx=10, pady=6, font=(FONT, 13))
        body.insert("1.0", "\n".join(binding.instruction for binding in entry.recipe.bindings))
        if self.hover_swipes and any(b.gesture.startswith("swipe_") for b in entry.recipe.bindings):
            body.insert("end", "\n\nFor an air swipe, hover over the control until the depth window says "
                        "Air swipe ready, then move in the bound direction.")
        body.config(state="disabled")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def finish_recording(self):
        recording, self.recording = self.recording, None
        if not recording.frames:
            self.notify("No depth frames were recorded.")
            return
        camera = self.stream.camera
        path = self.wall.directory / "recordings" / datetime.now().strftime("depth-%Y%m%d-%H%M%S-%f.npz")

        def worker():
            try:
                self.depth.write_recording(path, recording.frames, camera, diagnostics=recording.diagnostics())
            except OSError as error:
                self.results.put(("notice", f"Could not save depth recording · {error}", None))
            else:
                self.results.put(("notice", f"Saved depth recording {path.name}.", None))

        Thread(target=worker).start()
        self.notify(f"Saving {len(recording.frames)} depth frames.")

    def consume_results(self):
        while True:
            try:
                kind, data, placement = self.results.get_nowait()
            except Empty:
                return
            if kind == "notice":
                self.notify(data, duration=8)
                continue
            self.pending = False
            if kind == "widget_error":
                self.failed_command = placement
                self.notify(data, duration=10)
                continue
            try:
                entry = self.wall.add(data, placement)
            except (OSError, ValueError) as error:
                self.notify(f"Could not save widget · {error}", duration=10)
            else:
                self.selected = entry.id
                if self.anchor == placement:
                    self.anchor = None
                self.notify(f"Saved {entry.recipe.title} to the wall and library.")

    def update_depth(self, now):
        failure = self.stream.failure(now)
        if failure:
            if self.hover_swipes:
                self.hover_swipes.reset()
            if self.recording is not None and self.recording.frames:
                self.finish_recording()
            self.stream.close()
            self.stream = self.recording = None
            self.cancel_alignment()
            self.handle_pointer(self.surface.clear_interaction(now))
            self.error = failure
            print(failure, file=sys.stderr)
            return
        frame = self.stream.read(now)
        if frame is None or frame is self.frame or self.frozen:
            if self.hover_swipes and (self.frozen or self.frame is None or now - self.frame[0] > .16):
                self.hover_swipes.reset()
            if not self.frozen:
                had_pointer = self.surface.pointer is not None
                self.handle_pointer(self.surface.cancel_stale(now))
                if had_pointer and self.surface.pointer is None:
                    self.invalidate()
            return
        self.frame = frame
        events = []
        air_gesture = ""
        if self.points is None:
            old_status = self.surface.capturing
            old_hint = self.surface.capture_hint
            old_pointer = self.surface.pointer
            was_touching = self.surface.touching
            color = frame[2] if len(frame) > 2 else None
            events = self.surface.update(frame[1], frame[0], self.stream.camera, color=color)
            for event in events:
                self.handle_pointer(event)
            if self.hover_swipes:
                air_gesture = self.update_hover_swipe(frame[0])
            if (old_status != self.surface.capturing or self.surface.capturing
                    or old_hint != self.surface.capture_hint
                    or old_pointer is not None or self.surface.pointer is not None or was_touching):
                self.invalidate()
        elif self.hover_swipes:
            self.hover_swipes.reset()
        if self.recording is not None:
            self.recording.append(frame, self.surface, events, aligning=self.points is not None,
                                  air_gesture=air_gesture)
            if len(self.recording.frames) >= 150:
                self.finish_recording()

    def update_hover_swipe(self, stamp):
        point = self.surface.pointer
        if (self.surface.pointer_kind != "hover" or point is None or self.surface.confidence < 80
                or point.rgb_strength is None or point.rgb_strength < 1
                or self.gestures.contact or self.dialog is not None or self.editing):
            self.hover_swipes.reset()
            return ""
        target = self.view.target(point.x, point.y)
        entry = self.wall.get(target.widget)
        eligible = entry and any(b.target == target.part and b.gesture.startswith("swipe_")
                                 for b in entry.recipe.bindings)
        gesture = self.hover_swipes.feed(point.x, point.y, stamp, target if eligible else None)
        if gesture:
            entry = self.wall.get(gesture.target.widget)
            if entry and entry.recipe.binding(gesture.target.part, gesture.kind):
                try:
                    self.apply_gesture(gesture)
                except (OSError, ValueError) as error:
                    self.notify(f"Could not update wall · {error}", duration=8)
                else:
                    return gesture.kind
        return ""

    def status(self, now):
        if self.error:
            return self.error
        if self.surface is not None:
            if self.frame is None:
                if self.mode == "kinect" and self.stream is not None and self.stream.retry_at is not None:
                    return "Retrying Kinect startup"
                return "Waiting for Kinect depth"
            if self.surface.capturing:
                return self.surface.capture_hint or f"Keep the surface clear · {self.surface.baseline.progress:.0%}"
            if self.surface.capture_hint:
                return self.surface.capture_hint
            if not self.surface.baseline.ready:
                return "Clear the surface and press B to capture its depth"
        if self.pending:
            return "Creating widget…"
        if now < self.message_until:
            return self.message
        if self.anchor and self.chrome:
            return "Placement selected · Open New or Library"
        if self.editing:
            return "Drag a widget header to move it · Select a widget to resize or remove it · U to restore"
        if self.chrome and not self.fullscreen:
            if self.mode == "simulate":
                return "Simulated depth · Hold the mouse down to make Kinect contact · D for depth view"
            return "N  New     L  Library     E  Edit     G  Gestures     M  Model     F  Fullscreen     H  Hide controls"
        return ""

    def draw(self, now):
        if self.points is not None:
            self.view.alignment(self.depth.TARGETS, len(self.points))
            return
        pointer = None
        hover = self.mouse if self.mode != "simulate" else None
        if self.surface is not None and self.surface.pointer is not None:
            p = self.surface.pointer
            pointer = (p.x, p.y, self.surface.touching)
            if hover is None or (self.gestures.contact and self.gestures.contact.start.source == "depth"):
                hover = (p.x, p.y)
        self.view.render(self.wall.entries, time(), selected=self.selected,
                         hot=self.view.target(*hover) if hover else None,
                         editing=self.editing, chrome=self.chrome, anchor=self.anchor,
                         pointer=pointer, status=self.status(now), preview=self.preview)

    def draw_depth(self, now):
        if self.frame is not None and self.frame is not self.preview_frame and (
            self.frozen or now - self.preview_time > .085
        ):
            ppm = self.depth.depth_preview(self.frame[1], PREVIEW_SIZE)
            self.photo = tk.PhotoImage(master=self.depth_window, data=ppm, format="PPM")
            self.depth_canvas.itemconfigure(self.depth_image, image=self.photo)
            self.preview_frame, self.preview_time = self.frame, now

        sx, sy = PREVIEW_SIZE[0] / self.depth.WIDTH, PREVIEW_SIZE[1] / self.depth.HEIGHT
        self.depth_canvas.delete("markers")
        if self.points is not None:
            for i, (x, y) in enumerate(self.points, 1):
                self.depth_canvas.create_text(x * sx, y * sy, text=str(i), fill=MINT,
                                              font=(FONT, 18, "bold"), tags="markers")
            index = len(self.points) + 1
            message = self.alignment_error or (
                f"Frozen · Click the tip at point {index}" if self.frozen else
                f"Place a fingertip or raised marker on point {index}. Space freezes the depth image."
            )
        else:
            corners = self.surface.mapping.outline
            line = [(x * sx, y * sy) for x, y in (*corners, corners[0])]
            self.depth_canvas.create_line(*line, fill=MINT, tags="markers")
            point = self.surface.pointer
            plane = self.surface.plane
            label = "Aligned projector" if self.surface.calibrated else "Kinect-only area"
            fit = (f"Plane {plane.support:.0%} · residual {plane.error_mm:.1f} mm" if plane
                   else "Ray-relative depth" if self.surface.baseline.ready else "")
            if point is not None:
                x, y = point.u * sx, point.v * sy
                self.depth_canvas.create_oval(x - 6, y - 6, x + 6, y + 6, outline=MINT,
                                              fill=MINT if self.surface.touching else "", width=2, tags="markers")
                px, py, pz = map(round, point.xyz)
                state = self.surface.pointer_kind.capitalize()
                message = (f"{state} · confidence {self.surface.confidence}/100 · {point.height:.0f} mm above surface"
                           f"\nX {px}  Y {py}  Z {pz} mm"
                           f"\n{label} · {fit}")
            else:
                message = self.error or self.surface.capture_hint or " · ".join(filter(None, (label, fit)))
                message += "\nB Capture surface   C Align projector   R Record"
            if self.mode != "simulate" and not self.surface.camera.calibrated and self.surface.baseline.ready:
                message += " · Estimated camera intrinsics"
            if self.surface.rgb is not None:
                message += f"\nRGB · {self.surface.rgb_decision or self.surface.rgb.status}"
                if self.hover_swipes and self.hover_swipes.armed:
                    message += " · Air swipe ready"
        self.depth_status.configure(text=message)

    def tick(self):
        now = monotonic()
        self.consume_results()
        if self.stream is not None:
            self.update_depth(now)
        contact = self.gestures.contact
        if contact and contact.start.source == "mouse" and self.mouse:
            self.handle_pointer(PointerEvent("move", *self.mouse, now, "mouse"))
        second = int(time())
        if self.dirty or self.last_second != second:
            self.draw(now)
            self.last_second = second
            self.dirty = False
        if self.depth_window is not None and self.depth_window.state() != "withdrawn":
            self.draw_depth(now)
        self.root.after(36, self.tick)

    def close(self):
        if self.stream is not None and self.recording is not None and self.recording.frames:
            self.finish_recording()
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--kinect", action="store_true", help="Read live Kinect v1 depth.")
    sources.add_argument("--simulate", action="store_true", help="Use mouse-driven synthetic Kinect depth.")
    sources.add_argument("--replay", type=Path, help="Replay a saved Kinect depth .npz recording.")
    parser.add_argument("--data-dir", type=Path, default=HERE / "workspace", help="Wall and widget library directory.")
    parser.add_argument("--blank", action="store_true", help="Start an empty canvas when creating a new wall.")
    parser.add_argument("--offline", action="store_true", help="Use built-in command templates.")
    parser.add_argument("--rgb-assist", action="store_true", help="Use aligned Kinect color to confirm new touches.")
    parser.add_argument("--air-swipes", action="store_true", help="Enable RGB assist and hover swipes on bound widgets.")
    args = parser.parse_args(argv)
    if (args.rgb_assist or args.air_swipes) and not args.kinect:
        parser.error("RGB assist and air swipes require --kinect")
    mode = "kinect" if args.kinect else "simulate" if args.simulate else "replay" if args.replay else None
    wall, app = None, None
    try:
        root = tk.Tk()
        wall = Wall(args.data_dir, blank=args.blank)
        app = Projection(root, wall, mode=mode, replay=args.replay, offline=args.offline,
                         rgb_assist=args.rgb_assist or args.air_swipes, air_swipes=args.air_swipes)
        root.mainloop()
    except (ImportError, OSError, KeyError, ValueError, tk.TclError) as error:
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        pass
    finally:
        if app is not None and app.stream is not None:
            app.stream.close()
        if wall is not None:
            wall.close()


if __name__ == "__main__":
    main()
