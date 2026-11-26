"""Regression tests for provider settings, credential routing and response formats."""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import commands
from model_settings import DEFAULT_URL, ModelConfig, ModelPreferences, normalize_url
from widgets import make_recipe


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.prefs = ModelPreferences(Path(folder.name) / "model.json")
        self.url = "https://example.test/openai/v1"
        self.config = ModelConfig("provider/model", self.url, False)
        self.key = "example-test-credential"

    def response(self, *, fenced=False):
        definition = make_recipe("timer", "Tea timer", seconds=60).definition()
        text = json.dumps(definition)
        if fenced:
            text = f"```json\n{text}\n```"
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())

    def test_normalize_full_endpoint_and_reject_unsafe_url(self):
        self.assertEqual(normalize_url("https://EXAMPLE.TEST:443/openai/v1/chat/completions/"), self.url)
        self.assertEqual(normalize_url("https://api.openai.com"), DEFAULT_URL)
        self.assertEqual(normalize_url("http://[::1]:11434/v1"), "http://[::1]:11434/v1")
        for url in ("http://example.test/v1", "https://user@example.test/v1", self.url + "?key=example"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_save_metadata_separately_from_key_and_blank_leaves_existing_key(self):
        with patch("model_settings.Keychain") as store:
            self.prefs.save(self.config, self.key)
            store.return_value.save.assert_called_once_with(self.url, self.key)
            self.prefs.save(ModelConfig("provider/next-model", self.url, False))
            store.return_value.save.assert_called_once()
        raw = self.prefs.path.read_text()
        self.assertNotIn(self.key, raw)
        self.assertEqual(json.loads(raw)["model"], "provider/next-model")

    def test_settings_used_for_creation_with_keychain_key_and_without_json_mode(self):
        self.prefs.save(self.config)
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("model_settings.sys.platform", "darwin"),
            patch("model_settings.Keychain") as store,
            patch.object(commands, "urlopen") as transport,
        ):
            store.return_value.read.return_value = self.key
            transport.return_value.__enter__.return_value = self.response(fenced=True)
            self.assertEqual(commands.generate("Make tea timer", preferences=self.prefs).title, "Tea timer")
        store.return_value.read.assert_called_once_with(self.url)
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, self.url + "/chat/completions")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {self.key}")
        self.assertNotIn("Authorization", request.headers)
        fields = json.loads(request.data)
        self.assertEqual(fields["model"], self.config.model)
        self.assertNotIn("response_format", fields)

    def test_terminal_precedence_and_url_scoped_keys(self):
        self.prefs.save(self.config)
        environment = {"WALL_LLM_MODEL": "another-model", "WALL_LLM_URL": self.url + "/chat/completions",
                       "WALL_LLM_KEY": self.key, "WALL_LLM_JSON_MODE": "0"}
        with patch.dict("os.environ", environment, clear=True), patch("model_settings.Keychain") as store:
            active = ModelConfig().from_environment()
            self.assertEqual(active.model, "another-model")
            self.assertFalse(active.json_mode)
            self.assertEqual(self.prefs.credential(active), self.key)
            self.assertEqual(ModelConfig("other", "https://other.test/v1").environment_key(), "")
            store.assert_not_called()

    def test_connection_does_not_save_and_401_has_actionable_error(self):
        with patch.object(commands, "urlopen") as transport:
            transport.return_value.__enter__.return_value = self.response()
            self.assertEqual(commands.test_connection(self.config, self.key, self.prefs).title, "Tea timer")
            self.assertFalse(self.prefs.path.exists())
            transport.side_effect = HTTPError(self.url, 401, self.key, None, None)
            with self.assertRaisesRegex(ValueError, r"HTTP 401.*Check the API key") as error:
                commands.test_connection(self.config, self.key, self.prefs)
            self.assertNotIn(self.key, str(error.exception))

    def test_offline_bypasses_config_and_credential_lookup(self):
        self.prefs.path.write_text("broken settings")
        with patch.object(commands, "urlopen") as transport:
            self.assertTrue(commands.generate("One-minute timer", offline=True, preferences=self.prefs).components)
        transport.assert_not_called()
