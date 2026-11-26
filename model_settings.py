"""Model preferences and credentials scoped to the configured provider URL."""

from dataclasses import dataclass, replace
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

from keychain import Keychain
from wall import write_json


DEFAULT_URL = "https://api.openai.com/v1"
CHAT_PATH = "/chat/completions"


def normalize_url(value):
    value = value.strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Enter a model API base URL, such as https://api.openai.com/v1.")
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError as error:
        raise ValueError("The model API URL is invalid.") from error
    local = host in {"localhost", "127.0.0.1", "::1"}
    if not host or parts.username is not None or parts.password is not None or not (
        parts.scheme == "https" or (parts.scheme == "http" and local)
    ):
        raise ValueError("Use an HTTPS base URL or HTTP on localhost, with the key in its own field.")
    if parts.query or parts.fragment:
        raise ValueError("Remove the query or fragment from the model URL.")
    origin = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        origin += f":{port}"
    path = parts.path.rstrip("/").removesuffix(CHAT_PATH).rstrip("/")
    if parts.scheme == "https" and origin == "api.openai.com" and not path:
        path = "/v1"
    return urlunsplit((parts.scheme, origin, path, "", ""))


def validate_key(key):
    key = key.strip()
    if len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("The API key must be a single token without spaces.")
    return key


@dataclass(frozen=True)
class ModelConfig:
    model: str = ""
    url: str = DEFAULT_URL
    json_mode: bool = True

    def validated(self):
        model = self.model.strip()
        if len(model) > 180 or any(ord(char) < 32 for char in model):
            raise ValueError("Enter the provider's model ID in a single line.")
        return replace(self, model=model, url=normalize_url(self.url))

    def from_environment(self):
        model = os.environ.get("WALL_LLM_MODEL", "").strip()
        if not model:
            return self
        url = os.environ.get("WALL_LLM_URL", DEFAULT_URL) or DEFAULT_URL
        mode = os.environ.get("WALL_LLM_JSON_MODE")
        if mode is not None and mode not in {"0", "1"}:
            raise ValueError("Set WALL_LLM_JSON_MODE to 1 or 0.")
        return ModelConfig(model, url, mode != "0").validated()

    def environment_key(self):
        token = os.environ.get("WALL_LLM_KEY", "")
        url = os.environ.get("WALL_LLM_URL", DEFAULT_URL) or DEFAULT_URL
        if token and normalize_url(url) == self.url:
            return validate_key(token)
        parts = urlsplit(self.url)
        if parts.scheme == "https" and parts.netloc == "api.openai.com":
            return validate_key(os.environ.get("OPENAI_API_KEY", ""))
        return ""


class ModelPreferences:
    def __init__(self, path):
        self.path = path

    def load(self):
        if not self.path.exists():
            return ModelConfig()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
                    or not isinstance(data.get("model"), str) or not isinstance(data.get("url"), str)
                    or type(data.get("json_mode")) is not bool):
                raise ValueError("Unsupported settings format")
            return ModelConfig(data["model"], data["url"], data["json_mode"]).validated()
        except ValueError as error:
            raise ValueError("Model settings could not be read. Open Model settings and save them again.") from error

    def save(self, config, key=""):
        config, key = config.validated(), validate_key(key)
        if key:
            Keychain().save(config.url, key)
        write_json(self.path, {"version": 1, "model": config.model, "url": config.url,
                               "json_mode": config.json_mode})

    def credential(self, config):
        if token := config.environment_key():
            return token
        if sys.platform == "darwin":
            return validate_key(Keychain().read(config.url))
        return ""
