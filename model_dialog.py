"""The desktop form for configuring and testing a widget model."""

import os
from queue import Empty, SimpleQueue
from threading import Thread
import tkinter as tk

from commands import test_connection
from keychain import Keychain
from model_settings import ModelConfig


def show_model_settings(window, preferences, on_saved):
    error = ""
    try:
        saved = preferences.load()
    except (OSError, ValueError) as exc:
        saved, error = ModelConfig(), str(exc)
    status = tk.StringVar(master=window, value=error)
    endpoint = tk.StringVar(master=window, value=saved.url)
    model = tk.StringVar(master=window, value=saved.model)
    key = tk.StringVar(master=window)
    json_mode = tk.BooleanVar(master=window, value=saved.json_mode)

    form = tk.Frame(window, padx=16, pady=14)
    form.pack(fill="both", expand=True)
    form.columnconfigure(1, weight=1)
    for row, (name, value, masked) in enumerate((
        ("API base URL", endpoint, False),
        ("Model ID", model, False),
        ("API key", key, True),
    )):
        tk.Label(form, text=name, anchor="w").grid(row=row, column=0, sticky="w", pady=4, padx=(0, 13))
        entry = tk.Entry(form, textvariable=value, width=51, show="•" if masked else "")
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        if row == 1:
            model_entry = entry

    notes = "A blank key field uses the saved or environment key. A blank model ID uses built-in templates."
    if os.environ.get("WALL_LLM_MODEL", "").strip():
        notes += "\nThe terminal's model settings currently take precedence when creating widgets."
    tk.Label(form, text=notes, justify="left", anchor="w", wraplength=475).grid(
        row=3, column=0, columnspan=2, sticky="w", pady=(4, 5))
    tk.Checkbutton(form, text="Request JSON mode", variable=json_mode).grid(row=4, column=1, sticky="w")
    tk.Label(form, textvariable=status, justify="left", anchor="w", wraplength=475).grid(
        row=5, column=0, columnspan=2, sticky="w", pady=(8, 4))
    actions = tk.Frame(form)
    actions.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def run(operation):
        if test_button.cget("state") == "disabled":
            return
        try:
            config = ModelConfig(model.get(), endpoint.get(), json_mode.get()).validated()
        except ValueError as exc:
            status.set(str(exc))
            return
        token = key.get()
        for button in buttons:
            button.configure(state="disabled")
        status.set({"test": "Testing with a sample widget…", "save": "Saving settings…",
                    "forget": "Removing the saved key…"}[operation])
        results = SimpleQueue()

        def work():
            try:
                if operation == "test":
                    test_connection(config, token, preferences)
                elif operation == "save":
                    preferences.save(config, token)
                else:
                    Keychain().forget(config.url)
            except (OSError, ValueError) as exc:
                results.put(str(exc))
            else:
                results.put(None)

        def poll():
            if not window.winfo_exists():
                return
            try:
                failure = results.get_nowait()
            except Empty:
                window.master.after(80, poll)
                return
            for button in buttons:
                button.configure(state="normal")
            if failure:
                status.set(failure)
            elif operation == "save":
                key.set("")
                on_saved()
            elif operation == "test":
                status.set("Connected. The model returned a valid widget.")
            else:
                key.set("")
                status.set("Saved key removed for this API URL.")

        Thread(target=work, daemon=operation == "test").start()
        window.master.after(80, poll)

    test_button = tk.Button(actions, text="Test connection", command=lambda: run("test"))
    save_button = tk.Button(actions, text="Save", command=lambda: run("save"))
    forget_button = tk.Button(actions, text="Forget saved key", command=lambda: run("forget"))
    buttons = (test_button, save_button, forget_button)
    forget_button.pack(side="left")
    save_button.pack(side="right")
    test_button.pack(side="right")
    model_entry.focus_force()
