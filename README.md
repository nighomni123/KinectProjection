# Kinect Projection

A free-form projected wall canvas inspired by [The Lightbulb Computer](https://lightbulbcomputer.com/). Create widgets by command, put them anywhere in the workspace, and use them with the mouse or an Xbox 360 Kinect v1. The Mac renders the canvas onto a projector connected as an extended display.

## Quick start

Use Python 3.10 or later with Tkinter on macOS. The mouse and simulator modes run without a Kinect, projector, or model API key.

```bash
git clone https://github.com/nighomni123/KinectProjection.git
cd KinectProjection
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py --offline
```

## Try it without hardware

The first launch includes a clock and timer. Click empty wall space to choose a location, then click **New** or press **N**. Type a command and press Return. The command field also accepts text from macOS Dictation if it is enabled on your Mac. Commands you can try with the built-in templates include

- `Five-minute tea timer`
- `Checklist: pack bag, charge laptop, bring keys`
- `Water tracker goal 8 glasses`
- `Note: Leave by 6:30`
- `Clock + stopwatch`
- `Pick: tea, coffee, water`
- `Counter for reps`

Drag a widget’s header to move it. In **Edit** mode, select a widget to show its resize handle and **Remove** control. Removing it clears that instance from the wall. Use **U** to restore the most recently removed instance during the current session, or **Library** to place any saved widget again. A new copy has fresh widget state. Select a widget and press **G** to read its gesture instructions. **H** hides the canvas controls, **F** toggles fullscreen, and **Q** quits.

A tilted-wall simulator exercises the same Kinect depth and gesture processing used for a live stream. Move the mouse to hover, hold the mouse button down briefly to make contact, and release to lift your finger. It takes about a second to capture the simulated empty wall. **D** shows or hides the depth view.

```bash
python3 app.py --simulate --offline
```

Depth modes use NumPy, OpenCV, and the [KinectTouch detector](https://github.com/nighomni123/KinectTouch). The requirements file installs these dependencies. Use `--data-dir ./another-wall` to keep a separate canvas and `--blank` to omit the starter widgets when creating it.

## Connect an OpenAI-compatible model

1. Launch `python3 app.py` and press **M**, or open **New** → **Model settings**.
2. Enter your provider’s **API base URL**, exact **Model ID**, and **API key**. For OpenAI, use `https://api.openai.com/v1`. For another provider, use its Chat Completions API base URL. You can also paste its full `/chat/completions` URL. Local servers can use HTTP on localhost, such as `http://localhost:11434/v1`. Leave the key blank if your server accepts unauthenticated requests. [OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) · [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility)
3. Click **Test connection**. This sends a sample widget request and checks that the response can be loaded by the app. If your provider rejects the `response_format` field, turn off **Request JSON mode** and try again.
4. Click **Save**, then open **New** and try `Make a kitchen tile with a five-minute timer and a checklist for tea, cups, and biscuits`.

The API key is saved in macOS Keychain for that URL. Walls on the same Mac that use the same URL share the saved key. The URL, model ID, and JSON mode are saved in `workspace/model.json`, or under the selected data directory. Leave the key field blank on later edits to reuse the saved key. Paste a new key to replace it, or use **Forget saved key** to remove it for the URL shown. macOS may ask for access to the Keychain entry.

For terminal-based configuration, set the URL and model, then enter the key at the hidden prompt.

```zsh
export WALL_LLM_URL='https://api.openai.com/v1'
export WALL_LLM_MODEL='YOUR_MODEL_ID'
read -rs 'WALL_LLM_KEY?API key: '
printf '\n'
export WALL_LLM_KEY
python3 app.py
```

A terminal profile with `WALL_LLM_MODEL` takes precedence over the saved model settings. `WALL_LLM_KEY` applies to the URL in `WALL_LLM_URL`. With no URL, the app uses the official OpenAI endpoint and also accepts `OPENAI_API_KEY`. Set `WALL_LLM_JSON_MODE=0` to turn off JSON mode for the terminal profile.

A configured model can compose up to four supported components and choose their settings, colors, and gesture bindings. The app sends the command text and widget format. New widgets are validated before saving. **Use built-in templates** or `--offline` lets you create widgets without an API request. Saved widgets run locally.

The wall layout and running state are saved in `workspace/wall.json`. Reusable widget bundles contain

- `widgets/<id>/widget.json` for the controls and settings.
- `widgets/<id>/gestures.yaml` for gesture-to-action mappings and a short instruction for each mapping. The app regenerates the display text from the mapping when it loads the widget. Changes to the mappings take effect after relaunch.

Timed widgets use saved start timestamps so a timer or stopwatch can continue across relaunch. The library, state, and recordings live under the selected data directory.

## Connect Kinect and the projector

```bash
sh build-depth.sh
python3 app.py --kinect
```

The native helper requires a C compiler and libfreenect with Kinect v1 support. The build script defaults to `~/opt`. If libfreenect is installed elsewhere, run `FREENECT_PREFIX=/path/to/prefix sh build-depth.sh`. The helper reads the device’s depth intrinsics and streams 640×480 depth in millimetres. The development setup used an Intel Mac with macOS Monterey and a patched libfreenect build.

1. Connect the Kinect to USB and its 12 V adapter. Close other Kinect apps first. Aim it at a flat surface and check the depth window. Around a metre away is a starting point. With no projector, press **B** and use the outlined camera region to try touches.
2. Connect the projector as an **extended display**. Drag the main window onto it, leave the depth view on the Mac, and press **F** with the main window focused. Frame the entire projected area within the Kinect depth view and keep both devices fixed.
3. Press **C** for alignment. Place a fingertip or a small raised marker against the surface at the center of the highlighted target. Press **Space** to freeze the depth view and click the tip at the corresponding spot in that view. Repeat for all four targets. **Esc** cancels alignment.
4. Remove the marker, clear the surface, and press **B**. After 30 usable frames the app fits the dominant wall plane. The depth view shows fit quality, camera-space X/Y/Z and the detected point’s distance above the captured surface. It uses ray-relative distance when a plane fit cannot be made.
5. Start with a large timer. Try a short tap, a hold to reset, and a drag on the header. A hollow ring shows hover and a filled ring shows contact. On a counter or progress widget, swipe horizontally across the number to change the count. Swipe bindings are available in four directions for supported controls. The current tracker follows one near-wall contact at a time.

The projector calibration is saved in `calibration.json`. Re-align after moving either device or changing projection geometry. Recapture the empty surface whenever the wall or tabletop changes. The detector uses near-surface depth and cannot infer a pointing direction from across the room.

### RGB assist

```bash
sh build-depth.sh
python3 app.py --kinect --rgb-assist --offline
```

Press **B** with an empty wall and wait for **Color ready** in the depth window. The Kinect sends 640×480 RGB to the camera helper, which aligns it to depth and sends 160×120 color samples to OpenCV at up to about 15 fps. A new depth contact must have a nearby color change when color is reliable. Depth still determines the position, hold, and release. In darkness, during broad lighting changes, or when the color stream is unavailable, detection falls back to depth. A hand that looks very similar to the wall may need more contrast for RGB assist to confirm it.

The depth window shows when color matches or rejects a candidate. **R** saves those decisions with the usual depth diagnostics and leaves color images out of the recording. To compare with the depth only detector, launch without `--rgb-assist`.

Hover and contact markers show a confidence score from 0 to 100. The score combines the size of the depth patch, wall fit, continuity between frames and local RGB agreement. It is a heuristic for comparing observations. The recordings include the displayed score and any air swipe action.

### Swipe in the air

```bash
python3 app.py --kinect --air-swipes --offline
```

This enables RGB assist. Press **B** with the wall clear. Aim the hollow hover ring at a counter's number and pause for 0.3 seconds. When the depth window says **Air swipe ready**, move roughly one tenth of the canvas width to the right within 0.7 seconds to increment, or left to decrement. The starting control owns the swipe even if the hand crosses its edge. An air swipe requires a color match and hover confidence of at least 80 throughout the movement. Other widgets follow the swipe directions in their saved gesture bindings.

### Closing and reopening the Kinect

Close the main window or press **Q**, and wait for the Terminal prompt before starting the app again. The helper stops its streams and closes the camera. If startup stalls or an enumerated camera cannot be opened, the app retries once with a fresh helper. Set `KINECT_USB_DEBUG=1` before the launch command to see the USB shutdown stages in Terminal.

## Record and replay

Press **R** in depth mode to save up to 150 frames to `workspace/recordings/`. Press **R** again to finish early. To catch a false touch, record while it is happening so the file includes the current wall baseline, camera alignment, marker state, and touch events. The depth window labels the marker **Hover**, **Candidate**, or **Contact**. A candidate has yet to trigger a press.

To replay a gesture in the app, start the recording with a clear view for roughly two seconds so playback can capture a fresh baseline. Playback also accepts `KinectTouch` recordings with `depth` and `ts_ms` arrays. It loops and can use the live alignment file without changing it.

```bash
python3 app.py --replay workspace/recordings/depth-EXAMPLE.npz
python3 -m unittest discover -s tests -v
```

For the first hardware session, check the fit and cursor at all four corners, then exercise tap, hold, drag and swipe. Save a short recording of any misses so the same frames can be replayed while tuning the detector. The native helper build, synthetic depth processing, saved widget behavior, and mocked model responses were tested without Kinect or projector hardware.

## Home Assistant integration plan

Use the projected wall as a Home Assistant dashboard. The first milestone is one lamp tile that shows the lamp’s state, turns it on or off with a tap, and updates when someone changes it from the Home Assistant app.

### Build in phases

1. **Connect and read state.** Add a Home Assistant settings dialog for the dashboard base URL and a long-lived access token created under **Profile → Security**. Store the token in a separate macOS Keychain entry. Start with the authenticated REST API. Verify the connection with `GET /api/` and poll `GET /api/states/<entity_id>` for explicitly selected entities. Show `Unavailable` or `Disconnected` when appropriate. Run network work outside the Tkinter and Kinect frame loops. [Home Assistant authentication](https://www.home-assistant.io/docs/authentication/) · [REST API](https://contributors.home-assistant.io/core/internals/rest-api/)
2. **Control one lamp.** Route a completed tap from `Projection.apply_gesture()` to a Home Assistant adapter. Resolve the lamp’s current state and send either `light.turn_on` or `light.turn_off` with its entity ID through the REST actions endpoint. Show **Pending** while waiting and refresh the lamp from Home Assistant. On a timeout, refresh the state before accepting another command. Give dimmable lights left and right swipes for stepped brightness once the tap works reliably on the wall. [REST actions](https://contributors.home-assistant.io/core/internals/rest-api/#actions) · [Light action options](https://www.home-assistant.io/actions/light.turn_on/)
3. **Add scenes and live updates.** Add tiles for selected scenes and scripts, with a deliberate hold to run routines such as **Movie night** or **Good night**. Show **Started** when Home Assistant accepts a script and use the script entity’s state to track whether it is running. As the wall grows, add a WebSocket `subscribe_entities` subscription for the displayed entities. Rebuild the state snapshot after reconnecting. [Home Assistant scenes](https://www.home-assistant.io/docs/scene/) · [Scripts](https://www.home-assistant.io/integrations/script/) · [WebSocket subscriptions](https://contributors.home-assistant.io/core/internals/websocket-api/#subscribe-to-entities)
4. **Let commands create smart-home tiles.** Keep an editable mapping from friendly aliases such as `desk_lamp` to Home Assistant entities and allowed actions. Give the model those aliases and capabilities when it creates a tile. A saved widget carries the alias and typed gesture bindings in its existing `widget.json` and `gestures.yaml` files. The local adapter resolves the alias, validates the action and parameters, and sends it when you use the tile. Keep the token in Keychain and show **Needs setup** if a copied widget’s alias is missing on another wall.

### Tile ideas

| Widget | Wall interaction |
| --- | --- |
| **Room lighting** | Place controls near the areas they affect. Tap to switch a light on or off, swipe to dim it, and show the reported brightness. |
| **Routine launcher** | Hold **Movie night** to start a Home Assistant scene, or hold **Leaving home** to run a script with steps configured in Home Assistant. |
| **Kitchen board** | Place a local cooking timer beside a shared Home Assistant shopping list. Tap an item on the wall to mark it done and see the update on your phone. [Home Assistant to-do lists](https://www.home-assistant.io/integrations/todo/) |
| **Room comfort** | Show live temperature and air quality. Add a thermostat tile with large plus and minus targets and a configured setpoint range. |
| **At-a-glance home status** | Put read-only tiles by the door for open windows, energy use, and whether the washing machine has finished. |

### First milestone checks

- Use a fake Home Assistant adapter in simulation and recording replay. Test one action per completed tap, a cancelled contact producing zero actions, and pending, unavailable, and reconnect states.
- With a real Home Assistant instance, verify one visible lamp by mouse first. Change the lamp from the Home Assistant app and check that the wall updates. After projector calibration, repeat the same sequence by Kinect tap. Add a scene or a second room after that loop works.
