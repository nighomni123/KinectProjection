"""Create a widget from a command using a configured model or built-in templates."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from model_settings import CHAT_PATH, ModelConfig, validate_key
from widgets import ACCENTS, KINDS, make_recipe, validate_recipe


EXAMPLES = (
    "Five-minute tea timer",
    "Checklist: pack bag, charge laptop, bring keys",
    "Water tracker goal 8 glasses",
    "Note: Leave by 6:30",
    "Clock + stopwatch",
    "Pick: tea, coffee, water",
    "Counter for reps",
)
WORDS = {word: i for i, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve fifteen twenty thirty forty sixty".split()
)}
WORDS.update(fifteen=15, twenty=20, thirty=30, forty=40, sixty=60)
DURATION = re.compile(
    r"\b(\d{1,5}|" + "|".join(WORDS) + r")[\s-]*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)


def examples():
    return "\n".join(f"• {line}" for line in EXAMPLES[:5])


def _offline(command):
    parts = []
    for phrase in re.split(r"\s+\+\s+", command):
        phrase = re.sub(r"^(?:please\s+)?(?:(?:make|add|create|put|give me)\s+)?(?:an?\s+)?",
                        "", phrase.strip(), flags=re.IGNORECASE)
        if not re.match(r"^(?:note|text|reminder|checklist|to-do|todo|list)\b", phrase, re.IGNORECASE):
            phrase = re.split(r"[.!;,]\s+(?=(?:tap|hold|swipe|drag)\b)", phrase, maxsplit=1,
                              flags=re.IGNORECASE)[0]
        phrase = re.sub(r"\s+(?:over\s+)?here[.!]?$", "", phrase, flags=re.IGNORECASE)
        lower = phrase.lower()
        name, delimiter, value = phrase.partition(":")
        options = [item.strip() for item in re.split(r"[,\n]", value) if item.strip()]
        if re.match(r"^(?:note|text|reminder)\b", lower):
            parts.append(make_recipe("note", name.strip().capitalize() if delimiter else "Note",
                                     text=value.strip() if delimiter else phrase.partition(" ")[2]))
        elif re.match(r"^(?:checklist|to-do|todo|list)\b", lower):
            parts.append(make_recipe("checklist", "Checklist", items=options or ["First item"]))
        elif re.match(r"^(?:pick|picker|choose|random choice)\b", lower):
            parts.append(make_recipe("picker", "Pick an option", items=options or ["Option A", "Option B"]))
        elif "stopwatch" in lower:
            parts.append(make_recipe("stopwatch"))
        elif "timer" in lower or "countdown" in lower:
            duration = DURATION.search(phrase)
            seconds = 60
            if duration:
                amount, units = duration.group(1).lower(), duration.group(2).lower()
                amount = int(amount) if amount.isdecimal() else WORDS[amount]
                seconds = amount * {"s": 1, "m": 60, "h": 3600}[units[0]]
            title = DURATION.sub("", phrase).strip(" -").capitalize()
            parts.append(make_recipe("timer", title or "Timer", seconds=seconds))
        elif "clock" in lower:
            parts.append(make_recipe("clock", "Local time"))
        elif re.search(r"\b(progress|tracker|goal)\b", lower):
            goal = re.search(r"\b(?:goal\s+)?(\d{1,5})(?:\s+([\w -]{1,24}))?", phrase)
            title = phrase[:goal.start()].rstrip() if goal else phrase
            title = re.sub(r"\bgoal\s*$", "", title, flags=re.IGNORECASE).strip()
            parts.append(make_recipe("progress", title.capitalize() or "Progress",
                                     goal=int(goal[1]) if goal else 8,
                                     unit=goal[2].strip() if goal and goal[2] else "done"))
        elif "counter" in lower:
            start = re.search(r"\bstart(?:ing)?\s+(?:at\s+)?(-?\d{1,7})\b", lower)
            step = re.search(r"\b(?:by|step)\s+(\d{1,5})\b", lower)
            subject = re.search(r"\bcounter for ([\w -]+)", phrase, flags=re.IGNORECASE)
            title = subject[1].capitalize() if subject else "Counter"
            parts.append(make_recipe("counter", title, start=int(start[1]) if start else 0,
                                     step=int(step[1]) if step else 1))
        else:
            raise ValueError("Choose a timer, stopwatch, counter, checklist, note, clock, progress tracker, or picker.")

    if len(parts) == 1:
        return parts[0]
    entries = [dict(recipe.definition()["components"][0], id=f"part_{i + 1}")
               for i, recipe in enumerate(parts)]
    title = " + ".join(recipe.title for recipe in parts)
    return validate_recipe({"version": 1, "title": title[:42].rstrip(" +"), "components": entries})


def _prompt():
    return (
        "Design a widget for a projected wall. Return only one JSON object. Its fields are version (1), "
        "title (up to 42 characters), optional accent (" + ", ".join(ACCENTS) + "), and components "
        "(one to four objects with a unique lowercase id, kind, label up to 48 characters, and settings). "
        "Supported component kinds and example settings follow: " + json.dumps(KINDS) + ". "
        "Timer seconds are 1–86400. Lists contain one to eight short strings. Notes are up to 340 characters. "
        "Use only these components. The app handles placement. Built-in tap, hold and drag controls are "
        "provided. Add an optional bindings array only for an explicit relevant request. Each object has "
        "target (component id plus .face, .reset, .plus, .minus or .item_0 etc.), gesture (tap, hold, "
        "swipe_left, swipe_right, swipe_up or swipe_down), and action. Timers and stopwatches expose "
        "toggle and reset on .face and reset on .reset. Counters and progress expose increment or decrement "
        "on .plus or .minus respectively, and increment, decrement or reset on .face. A checklist item "
        "exposes check, and a picker .face exposes pick and reset. Do not add code or external integrations. "
        'Example: {"version":1,"title":"Tea","components":[{"id":"tea","kind":"timer","label":"Tea timer",'
        '"settings":{"seconds":300}}]}.'
    )


def _model(command, config, token):
    fields = {
        "model": config.model,
        "messages": [{"role": "system", "content": _prompt()}, {"role": "user", "content": command}],
    }
    if config.json_mode:
        fields["response_format"] = {"type": "json_object"}
    request = Request(config.url + CHAT_PATH, data=json.dumps(fields).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=35) as response:
            payload = response.read(131073)
        if len(payload) > 131072:
            raise ValueError("Model response was too large.")
        text = json.loads(payload)["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise TypeError("Expected a JSON text reply")
        fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced[1]
        definition = json.loads(text)
    except HTTPError as error:
        tips = {
            401: "Check the API key in Model settings.",
            403: "Check that the API key can access this model.",
            404: "Check the base URL and exact model ID.",
            429: "The provider rate limit or quota was reached.",
        }
        if config.json_mode:
            tips[400] = "Check the model ID. Turn off Request JSON mode if the provider does not support it."
        raise ValueError(f"Model request failed (HTTP {error.code}). "
                         + tips.get(error.code, "Check the provider and model settings.")) from error
    except (URLError, TimeoutError) as error:
        raise ValueError("Could not reach the model endpoint.") from error
    except (UnicodeError, KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("The model returned a reply the widget builder could not read.") from error
    return validate_recipe(definition)


def test_connection(config, key, preferences):
    config = config.validated()
    if not config.model:
        raise ValueError("Enter the provider's model ID first.")
    token = validate_key(key) or preferences.credential(config)
    return _model("Create a one-minute timer as a test widget.", config, token)


def generate(command, *, offline=False, preferences=None):
    command = command.strip()
    if not 1 <= len(command) <= 600:
        raise ValueError("Enter a widget command up to 600 characters.")
    if offline:
        return _offline(command)
    config = ModelConfig().from_environment()
    if preferences is not None and not config.model:
        config = preferences.load()
    if not config.model:
        return _offline(command)
    token = preferences.credential(config) if preferences is not None else config.environment_key()
    return _model(command, config, token)
