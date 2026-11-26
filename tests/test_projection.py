"""Offline regressions for generated widgets and the free-placement depth pipeline."""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import yaml

import app
import commands
from depth_geometry import Intrinsics, WallPlane
from interaction import Gestures, PointerEvent, Target
import surface
from view import WallView
from wall import Rect, Wall
from widgets import make_recipe, timer_value, validate_recipe


class WidgetsTests(unittest.TestCase):
    def test_composition_and_local_examples(self):
        for command in commands.EXAMPLES:
            with self.subTest(command=command):
                self.assertTrue(commands.generate(command, offline=True).components)
        recipe = commands.generate("Five-minute tea timer + Checklist: mug, kettle", offline=True)
        self.assertEqual([c.kind for c in recipe.components], ["timer", "checklist"])
        self.assertEqual(recipe.components[0].settings["seconds"], 300)
        self.assertEqual(recipe.components[1].settings["items"], ["mug", "kettle"])

    def test_rejects_illegal_actions(self):
        raw = make_recipe("timer").definition()
        raw["bindings"] = [{"target": "main.face", "gesture": "tap", "action": "run_shell"}]
        with self.assertRaises(ValueError):
            validate_recipe(raw)

    def test_model_build_uses_validated_definition_and_user_command(self):
        definition = make_recipe("counter", "Rep counter", step=2).definition()
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(definition)}}]}).encode()
        env = {"WALL_LLM_MODEL": "test-model", "WALL_LLM_URL": "http://localhost:12345/v1",
               "OPENAI_API_KEY": "unrelated-key"}
        with patch.dict("os.environ", env, clear=True), patch.object(commands, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value = io.BytesIO(payload)
            recipe = commands.generate("Make me a rep counter")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, env["WALL_LLM_URL"] + "/chat/completions")
        self.assertIsNone(request.get_header("Authorization"))
        body = json.loads(request.data)
        self.assertEqual(body["messages"][-1]["content"], "Make me a rep counter")
        self.assertEqual(recipe.components[0].settings["step"], 2)

    def test_save_reload_rebind_and_reuse_widget(self):
        with TemporaryDirectory() as directory:
            wall = Wall(directory, blank=True)
            try:
                original = wall.add(make_recipe("timer", "Tea", seconds=300), (42, 230))
                wall.action(original.id, "main.face", "toggle", 100)
                wall.move(original.id, Rect(400, 350, 360, 260))
                copy = wall.place_saved(original.widget_id)
                self.assertNotEqual(original.id, copy.id)
                self.assertFalse(copy.state)
                with self.assertRaisesRegex(ValueError, "already open"):
                    Wall(directory)
                wall.remove(copy.id)
                self.assertEqual(wall.restore_removed().id, copy.id)
                gesture_path = wall.library_path / original.widget_id / "gestures.yaml"
                data = yaml.safe_load(gesture_path.read_text())
                saved = next(b for b in data["bindings"] if b["target"] == "main.face" and b["gesture"] == "tap")
                self.assertIn("start or pause", saved["instruction"])
                saved["action"] = "reset"
                gesture_path.write_text(yaml.safe_dump(data))
            finally:
                wall.close()

            wall = Wall(directory)
            try:
                entry = wall.get(original.id)
                self.assertEqual(entry.box, Rect(400, 350, 360, 260))
                self.assertEqual(entry.recipe.binding("main.face", "tap").action, "reset")
                self.assertIn("reset", entry.recipe.binding("main.face", "tap").instruction)
                remaining, running = timer_value(entry.recipe.components[0], entry.state["main"], 150)
                self.assertEqual((remaining, running), (250, True))
                wall.remove(entry.id)
                self.assertEqual(wall.library()[0][0], original.widget_id)
            finally:
                wall.close()


class GestureTests(unittest.TestCase):
    def setUp(self):
        self.gestures = Gestures()
        self.target = Target("widget-id", "main.face")

    def event(self, kind, x=100, y=100, stamp=0.0, source="depth"):
        return PointerEvent(kind, x, y, stamp, source)

    def test_hold_never_also_taps(self):
        self.gestures.feed(self.event("down"), self.target, hold=True)
        self.assertEqual(self.gestures.feed(self.event("move", stamp=.7)).kind, "hold")
        self.assertIsNone(self.gestures.feed(self.event("up", stamp=.85)))

    def test_drag_keeps_original_target_and_can_be_cancelled(self):
        self.gestures.feed(self.event("down"), self.target, drag=True)
        self.assertIsNone(self.gestures.feed(self.event("up", source="mouse")))
        gesture = self.gestures.feed(self.event("move", x=170, stamp=.2))
        self.assertEqual((gesture.kind, gesture.target, gesture.dx), ("drag", self.target, 70))
        self.assertEqual(self.gestures.feed(self.event("cancel", stamp=.45)).kind, "cancel")
        self.assertIsNone(self.gestures.feed(self.event("up", stamp=.46)))

    def test_tap_and_signed_swipes(self):
        self.gestures.feed(self.event("down"), self.target)
        self.assertEqual(self.gestures.feed(self.event("up", stamp=.2)).kind, "tap")
        for dx, dy, kind in [(90, 4, "swipe_right"), (-90, 4, "swipe_left"),
                             (4, -90, "swipe_up"), (4, 90, "swipe_down")]:
            self.gestures.feed(self.event("down"), self.target, hold=True)
            action = self.gestures.feed(self.event("up", x=100 + dx, y=100 + dy, stamp=.42))
            self.assertEqual(action.kind, kind)


class DepthTests(unittest.TestCase):
    def test_perspective_and_expanded_canvas_mask(self):
        corners = ((90, 72), (553, 65), (575, 415), (64, 405))
        mapping = surface.SurfaceMap(corners, surface.TARGETS)
        for a, b in zip(corners, surface.TARGETS):
            self.assertTrue(np.allclose(mapping.project(*a), b))
        for x, y in surface.BOARD_CORNERS:
            self.assertTrue(np.allclose(mapping.project(*mapping.camera_pixel(x, y)), (x, y), atol=1e-8))
        self.assertGreater(mapping.mask[240, 74], 0)
        with self.assertRaises(ValueError):
            surface.SurfaceMap([corners[0], corners[2], corners[1], corners[3]], surface.TARGETS)

    def test_plane_rejects_outliers_and_flattens_3d_points(self):
        simulator = surface.SimulatedStream()
        image = simulator.background.copy()
        image[90:205, 95:290] -= 240
        mask = surface.SurfaceMap().mask
        plane = WallPlane.fit(image, mask, simulator.camera)
        self.assertIsNotNone(plane)
        self.assertGreater(plane.support, .75)
        self.assertAlmostEqual(plane.offset, 1150, places=0)
        ray = simulator.camera.ray(320, 240)
        point = ray * ((plane.offset - 70) / -plane.normal[2])
        self.assertAlmostEqual(plane.height(point), 70)
        self.assertAlmostEqual(plane.height(plane.flatten(point)), 0)

    def test_simulator_hover_contact_release_and_stall_cancel(self):
        stream = surface.SimulatedStream()
        depth_input = surface.SurfaceInput(None)
        depth_input.start_baseline(0)
        index = 0

        def frames(count):
            nonlocal index
            events = []
            for _ in range(count):
                stamp, image = stream.read(index * .04)
                index += 1
                events.extend(depth_input.update(image, stamp, stream.camera))
            return events

        self.assertFalse(frames(30))
        self.assertIsNotNone(depth_input.plane)
        stream.cursor = depth_input.mapping.camera_pixel(458, 655)
        self.assertFalse(frames(6))
        self.assertGreater(depth_input.pointer.height, 100)
        stream.pressed = True
        events = frames(6)
        self.assertEqual([e.kind for e in events].count("down"), 1)
        self.assertLess(abs(events[0].x - 458), 16)
        self.assertLess(abs(events[0].y - 655), 16)
        self.assertAlmostEqual(depth_input.pointer.height, 20, delta=2)
        stream.pressed = False
        self.assertEqual([e.kind for e in frames(8)].count("up"), 1)
        stream.pressed = True
        frames(8)
        self.assertEqual(depth_input.cancel_stale(index * .04 + .60).kind, "cancel")
        self.assertFalse(depth_input.touching)

    def test_rgb_support_rejects_ghosts_and_preserves_depth_fallback(self):
        stream = surface.SimulatedStream()
        pipeline = surface.SurfaceInput(None, rgb_assist=True)
        pipeline.start_baseline(0)
        width, height = surface.RGB_SIZE
        wall = np.full((height, width, 3), 140, dtype=np.uint8)
        hand = wall.copy()
        hand[57:64, 77:84] = (75, 100, 125)
        index = 0

        def feed(color, count=8):
            nonlocal index
            kinds = []
            for _ in range(count):
                stamp, depth = stream.read(index * .05)
                index += 1
                sample = None if color is None else (stamp, color)
                events = pipeline.update(depth, stamp, stream.camera, color=sample)
                kinds.extend(event.kind for event in events)
            return kinds

        feed(wall, 30)
        stream.cursor, stream.pressed = (320, 240), True
        self.assertEqual(feed(wall), [])
        self.assertEqual(pipeline.rgb_decision, "Rejected depth candidate")
        self.assertEqual(feed(hand).count("down"), 1)
        self.assertGreaterEqual(pipeline.confidence, 80)
        stream.cursor = None
        self.assertEqual(feed(hand).count("up"), 1)
        self.assertEqual(feed(hand), [])

        stream.cursor, stream.pressed = (320, 240), False
        self.assertEqual(feed(hand), [])
        self.assertEqual(pipeline.pointer_kind, "hover")
        matched = pipeline.confidence
        feed(None)
        depth_only = pipeline.confidence
        feed(wall)
        self.assertGreaterEqual(matched, 80)
        self.assertGreater(matched, depth_only)
        self.assertGreater(depth_only, pipeline.confidence)
        stream.pressed = True

        for color in (None, np.zeros_like(wall), wall + 35):
            stream.cursor = (320, 240)
            self.assertEqual(feed(color).count("down"), 1)
            stream.cursor = None
            self.assertEqual(feed(wall).count("up"), 1)

    def test_rgb_native_framing_reuses_color_between_samples(self):
        depth = np.full((480, 640), 1200, dtype=np.uint16)
        width, height = surface.RGB_SIZE
        color = np.full((height, width, 3), (15, 70, 130), dtype=np.uint8)
        process = MagicMock()
        header = f"KDP2 640 480 320 240 575 575 {width} {height}\n".encode("ascii")
        process.stdout = io.BytesIO(
            header + depth.tobytes() + b"\x01" + color.tobytes() + (depth + 1).tobytes() + b"\x00"
        )
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "depth_stream"
            helper.touch()
            with patch.object(surface, "HERE", helper.parent), patch.object(
                surface.subprocess, "Popen", return_value=process
            ) as spawn, patch.object(surface, "monotonic", return_value=1):
                live = surface.DepthStream(rgb_assist=True)
                live.reader.join(timeout=2)
                self.assertFalse(live.reader.is_alive())
                self.assertIsNone(live.error)
                np.testing.assert_array_equal(live.latest[1], depth + 1)
                np.testing.assert_array_equal(live.latest[2][1], color)
                spawn.assert_called_once_with(
                    [str(helper), "--parent-pipe", "--rgb-assist"],
                    stdin=surface.subprocess.PIPE, stdout=surface.subprocess.PIPE,
                )

    def test_camera_close_waits_for_cleanup_before_signaling(self):
        live = surface.DepthStream.__new__(surface.DepthStream)
        live.process = MagicMock()
        live.reader = MagicMock()
        live.process.poll.return_value = None
        live.close()
        live.process.stdin.close.assert_called_once_with()
        live.process.wait.assert_called_once_with(timeout=live.SHUTDOWN_SECONDS)
        live.process.terminate.assert_not_called()
        live.process.kill.assert_not_called()

    def test_camera_startup_retries_once_after_reaping_first_helper(self):
        first, second = MagicMock(), MagicMock()
        for process in (first, second):
            process.poll.return_value = surface.DepthStream.STARTUP_RETRY
            process.stdout = io.BytesIO()
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "depth_stream"
            helper.touch()
            with patch.object(surface, "HERE", helper.parent), patch.object(
                surface.subprocess, "Popen", side_effect=[first, second]
            ) as spawn, patch.object(surface, "monotonic", return_value=1):
                stream = surface.DepthStream()
                self.assertIsNone(stream.failure(1))
                first.stdin.close.assert_called_once_with()
                self.assertIsNone(stream.failure(1.5))
                self.assertEqual(spawn.call_count, 1)
                self.assertIsNone(stream.failure(2))
                self.assertEqual(spawn.call_count, 2)
                self.assertIn("camera found but could not start", stream.failure(3))
                stream.close()

    def test_replay_roundtrip_and_native_framing(self):
        image = np.full((480, 640), 1200, dtype=np.uint16)
        sensor = Intrinsics.from_values(320, 240, 569.2, 573.3)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.npz"
            surface.write_recording(path, [(0, image), (.04, image + 1)], sensor)
            replay = surface.ReplayStream(path)
            self.assertEqual(replay.camera, sensor)
            self.assertEqual(replay.read(1)[1][0, 0], 1200)
            self.assertIsNone(replay.read(1.01))
            self.assertEqual(replay.read(1.05)[1][0, 0], 1201)
            self.assertEqual(replay.read(1.65)[1][0, 0], 1200)

        process = MagicMock()
        process.stdout = io.BytesIO(b"KDP1 640 480 320 240 569.2 573.3\n" + image.tobytes())
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "depth_stream"
            helper.touch()
            with patch.object(surface, "HERE", helper.parent), patch.object(
                surface.subprocess, "Popen", return_value=process
            ):
                live = surface.DepthStream()
                live.reader.join(timeout=2)
                self.assertFalse(live.reader.is_alive())
                self.assertEqual(live.camera, sensor)
                self.assertTrue(np.array_equal(live.latest[1], image))


class CanvasControllerTests(unittest.TestCase):
    def test_air_swipe_uses_starting_binding_and_cancels_on_tracking_loss(self):
        canvas = MagicMock()
        canvas.winfo_width.return_value = canvas.winfo_height.return_value = 1000
        with TemporaryDirectory() as directory, patch.object(app.tk, "Canvas", return_value=canvas):
            wall = Wall(directory, blank=True)
            try:
                entry = wall.add(make_recipe("counter"), (130, 250))
                ui = app.Projection(MagicMock(), wall, offline=True, air_swipes=True)
                target = Target(entry.id, "main.face")
                ui.view.target = lambda x, y: target if x < 150 else Target(None, "canvas")
                ui.surface = SimpleNamespace(pointer_kind="hover", confidence=90)
                def frame(x, t, strength=2):
                    ui.surface.pointer = SimpleNamespace(x=x, y=200, rgb_strength=strength)
                    return ui.update_hover_swipe(t)
                for i in range(8):
                    frame(100, 1 + i*.05)
                self.assertTrue(ui.hover_swipes.armed)
                frame(120, 1.4, None)
                self.assertFalse(ui.hover_swipes.armed)
                for i, x in enumerate((140, 178, 212)):
                    self.assertFalse(frame(x, 1.45 + i*.05))
                for i in range(8):
                    frame(100, 2 + i*.05)
                self.assertFalse(frame(130, 2.4))
                self.assertFalse(frame(166, 2.45))
                self.assertEqual(frame(204, 2.5), "swipe_right")
                self.assertFalse(frame(218, 2.55))
                self.assertEqual(wall.get(entry.id).state["main"]["value"], 1)
            finally:
                wall.close()

    def test_synthetic_wall_tap_hold_and_drag_reach_saved_timer(self):
        root = MagicMock()
        canvas = MagicMock()
        canvas.winfo_width.return_value = 1000
        canvas.winfo_height.return_value = 1000
        with TemporaryDirectory() as directory, patch.object(app.tk, "Canvas", return_value=canvas):
            wall = Wall(directory, blank=True)
            try:
                timer = wall.add(make_recipe("timer", seconds=300), (130, 250))
                ui = app.Projection(root, wall, offline=True)
                camera = surface.SimulatedStream()
                pipeline = surface.SurfaceInput(None)
                pipeline.start_baseline(0)
                frame_index = 0

                def feed(x, y, contact, count):
                    nonlocal frame_index
                    camera.cursor = pipeline.mapping.camera_pixel(x, y) if x is not None else None
                    camera.pressed = contact
                    for _ in range(count):
                        stamp, frame = camera.read(frame_index * .04)
                        frame_index += 1
                        for event in pipeline.update(frame, stamp, camera.camera):
                            ui.handle_pointer(event)

                feed(None, None, False, 30)
                face = next(r for r, t in ui.view.regions if t.part == "main.face")
                x, y = face.x + face.w / 2, face.y + face.h / 2
                feed(x, y, True, 7)
                self.assertFalse(wall.get(timer.id).state)
                feed(x, y, False, 6)
                self.assertIn("since", wall.get(timer.id).state["main"])
                feed(x, y, True, 25)
                self.assertEqual(wall.get(timer.id).state["main"], {})
                feed(x, y, False, 7)
                self.assertEqual(wall.get(timer.id).state["main"], {})

                feed(210, 280, True, 6)
                for step in range(1, 14):
                    feed(210 + step * 11, 280, True, 1)
                feed(353, 280, False, 7)
                self.assertGreater(wall.get(timer.id).box.x, 255)
            finally:
                wall.close()

    def test_modeless_create_move_check_and_reload(self):
        root = MagicMock()
        canvas = MagicMock()
        canvas.winfo_width.return_value = 1000
        canvas.winfo_height.return_value = 1000
        with TemporaryDirectory() as directory, patch.object(app.tk, "Canvas", return_value=canvas):
            wall = Wall(directory, blank=True)
            try:
                ui = app.Projection(root, wall, offline=True)
                mouse = SimpleNamespace(x=75, y=140)
                ui.mouse_down(mouse)
                ui.mouse_up(mouse)
                self.assertEqual(ui.anchor, (75, 140))
                ui.results.put(("widget", make_recipe("checklist", items=["Bag", "Keys"]), ui.anchor))
                ui.consume_results()
                widget = wall.entries[0]
                self.assertEqual(widget.box.x, 75)
                ui.draw(0)
                ui.handle_pointer(PointerEvent("down", 110, 160, 1.0, "depth"))
                ui.handle_pointer(PointerEvent("move", 400, 205, 1.2, "depth"))
                self.assertIsNotNone(ui.preview)
                ui.handle_pointer(PointerEvent("up", 400, 205, 1.3, "depth"))
                self.assertEqual(wall.entries[0].box.x, 365)
                ui.draw(0)
                (region, target) = next((r, t) for r, t in ui.view.regions if t.part == "main.item_0")
                ui.handle_pointer(PointerEvent("down", region.x + 4, region.y + 4, 2.0, "depth"))
                ui.handle_pointer(PointerEvent("up", region.x + 4, region.y + 4, 2.1, "depth"))
                self.assertEqual(wall.entries[0].state["main"]["checked"], [0])
            finally:
                wall.close()
            wall = Wall(directory)
            try:
                self.assertEqual(wall.entries[0].state["main"]["checked"], [0])
                self.assertIsInstance(ui.view, WallView)
            finally:
                wall.close()


if __name__ == "__main__":
    unittest.main()
