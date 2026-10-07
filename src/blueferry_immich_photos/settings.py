"""Server URL in a config file, API key in the keyring (or a 0600 file).

The key goes to the desktop Secret Service through libsecret, like
BlueFerry's own storage key. Without a usable keyring it falls back to an
owner-only file next to the config. The key never appears in logs, the
manifest, D-Bus replies or command lines.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blueferry_plugin_kit.secrets import (
    KeyringStore,
    SecretsError,
    read_private_text,
    write_private,
)
from blueferry_plugin_kit.secrets import config_dir as _kit_config_dir

from blueferry_immich_photos import PLUGIN_ID

SCHEMA = "io.weirdware.blueferry.immich_photos.ApiKey"

SettingsError = SecretsError


def config_dir() -> Path:
    return _kit_config_dir(PLUGIN_ID)


@dataclass(frozen=True, slots=True)
class Settings:
    url: str
    key_store: str          # "keyring" or "file"
    camera_model: str = ""  # optional: only assets from this camera model


class SettingsStore(KeyringStore):
    SECRET_SCHEMA = SCHEMA
    SECRET_ATTRIBUTES = ("server",)
    SECRET_LABEL = "BlueFerry Immich API key"

    def __init__(self, directory: Path | None = None, *, secret: Any = None) -> None:
        # secret: gi.repository.Secret, injectable for tests
        super().__init__(directory or config_dir(), secret=secret)

    @property
    def config_path(self) -> Path:
        return self.directory / "config.json"

    @property
    def key_path(self) -> Path:
        return self.directory / "api-key"

    def load(self) -> Settings | None:
        try:
            raw = json.loads(read_private_text(self.config_path))
        except FileNotFoundError:
            return None
        except ValueError:
            raise SettingsError("config.json is not valid JSON") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("url"), str):
            raise SettingsError("config.json has no server URL")
        model = raw.get("camera_model", "")
        return Settings(
            url=raw["url"],
            key_store="file" if raw.get("key_store") == "file" else "keyring",
            camera_model=model if isinstance(model, str) else "",
        )

    def save(self, settings: Settings, api_key: str, *, prefer_keyring: bool = True) -> str:
        """Store the key (keyring first) and the config; return the key store."""
        store = self.save_secret(
            {"server": settings.url}, api_key, prefer_keyring=prefer_keyring,
        )
        write_private(self.config_path, json.dumps({
            "url": settings.url, "key_store": store, "camera_model": settings.camera_model,
        }, indent=2) + "\n")
        return store

    def api_key(self, settings: Settings) -> str:
        return self.load_secret(
            settings.key_store, {"server": settings.url},
            missing="the API key file is missing; run setup again",
            empty="no API key stored; run setup again",
            strip=True,
        )

    def forget(self) -> None:
        settings = None
        try:
            settings = self.load()
        except SettingsError:
            pass
        if settings is not None and settings.key_store == "keyring":
            self.clear_keyring({"server": settings.url})
        self.key_path.unlink(missing_ok=True)
        self.config_path.unlink(missing_ok=True)

    def forget_keyring(self, url: str) -> None:
        """Drop the keyring entry of a server URL that is no longer used."""
        self.clear_keyring({"server": url})
