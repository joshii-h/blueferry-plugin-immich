"""The plugin process: Photos1 backed by Immich."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from blueferry.plugin_api.config import ConfigError
from blueferry.plugin_api.manifest import PluginManifest
from blueferry.plugin_api.service import PhotosService, PluginCallError

from blueferry_immich_photos.cache import PhotoCache
from blueferry_immich_photos.immich import Asset, ImmichClient, ImmichError, normalize_url
from blueferry_immich_photos.settings import Settings, SettingsError, SettingsStore

log = logging.getLogger(__name__)

SETUP_HINT = (
    "set the server URL and API key in BlueFerry's settings (Plugins), or run: "
    "blueferry plugins immich setup --url https://your-immich-server"
)
_ERROR_TEXT = {
    "unauthorized": "the API key was rejected",
    "forbidden": "the API key lacks asset.read, asset.view or asset.download",
    "not-found": "not found on the server",
    "server-error": "the Immich server reported an error",
    "network": "the Immich server is not reachable",
    "too-large": "the server sent more data than allowed",
    "bad-response": "the server's answer was not understood",
    "redirect": "the server redirected; check the URL",
    "invalid-url": "the configured URL is not https",
}


class ImmichPhotosService(PhotosService):
    def __init__(
        self,
        manifest: PluginManifest,
        bus: Any = None,
        *,
        settings: SettingsStore | None = None,
        cache: PhotoCache | None = None,
        client_factory: Callable[[str, str], ImmichClient] = ImmichClient,
        **kwargs: Any,
    ) -> None:
        super().__init__(manifest, bus, **kwargs)
        self._settings = settings or SettingsStore()
        self._cache = cache or PhotoCache()
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._assets: dict[str, Asset] = {}
        self._newest: str | None = None
        self._last_error = ""

    def _client(self) -> tuple[ImmichClient, str]:
        settings = self._settings.load()
        if settings is None:
            raise PluginCallError("not configured; " + SETUP_HINT)
        try:
            key = self._settings.api_key(settings)
            return self._client_factory(settings.url, key), settings.camera_model
        except SettingsError as error:
            raise PluginCallError(str(error)) from None
        except ImmichError as error:
            raise PluginCallError(_ERROR_TEXT.get(error.token, error.token)) from None

    def _failed(self, error: ImmichError) -> PluginCallError:
        self._last_error = error.token
        log.info("Immich request failed: %s", error.token)
        return PluginCallError(_ERROR_TEXT.get(error.token, error.token))

    def status(self) -> dict[str, object]:
        try:
            settings = self._settings.load()
        except SettingsError as error:
            return {"state": "error", "detail": str(error)}
        if settings is None:
            return {"state": "unconfigured", "detail": SETUP_HINT}
        server = settings.url.split("://", 1)[-1].split("/", 1)[0]
        if self._last_error:
            return {
                "state": "error", "server": server,
                "detail": _ERROR_TEXT.get(self._last_error, self._last_error),
            }
        return {"state": "ok", "server": server}

    # ---- settings (Plugin1.GetConfig/SetConfig) ------------------------------

    def config_values(self) -> dict[str, object]:
        """Worker thread. The stored URL and camera model; the key only as set/unset."""
        try:
            settings = self._settings.load()
        except SettingsError as error:
            raise PluginCallError(str(error)) from None
        if settings is None:
            return {}
        try:
            stored = bool(self._settings.api_key(settings))
        except SettingsError:
            stored = False
        return {"url": settings.url, "api_key": stored, "camera_model": settings.camera_model}

    def apply_config(self, values: dict[str, object]) -> None:
        """Worker thread. Check the key against the server, then store it.

        The keyring entry stays keyed by the server URL (the same entry
        ``setup`` writes), so a configuration made with ``setup`` keeps
        working. Without a new key, the stored one moves with a changed URL.
        """
        try:
            url = normalize_url(str(values.get("url") or ""))
        except ImmichError:
            raise ConfigError("url", "must start with https:// (http only for localhost)") from None
        model = str(values.get("camera_model") or "")
        try:
            current = self._settings.load()
        except SettingsError:
            current = None
        key = str(values.get("api_key") or "")
        if not key and current is not None:
            try:
                key = self._settings.api_key(current)
            except SettingsError:
                key = ""
        if not key:
            raise ConfigError("api_key", "is required")
        if any(ch.isspace() for ch in key) or len(key) > 512:
            raise ConfigError("api_key", "does not look like an API key")
        changed = (
            current is None or current.url != url or current.camera_model != model
            or "api_key" in values
        )
        if not changed:
            return
        try:
            self._client_factory(url, key).recent(1, camera_model=model)
        except ImmichError as error:
            field = "api_key" if error.token in ("unauthorized", "forbidden") else "url"
            raise ConfigError(field, _ERROR_TEXT.get(error.token, error.token)) from None
        prefer_keyring = current is None or current.key_store != "file"
        try:
            self._settings.save(
                Settings(url=url, key_store="keyring", camera_model=model), key,
                prefer_keyring=prefer_keyring,
            )
        except (SettingsError, OSError) as error:
            raise ConfigError("", f"could not store the settings: {error}") from None
        if current is not None and current.url != url and current.key_store == "keyring":
            self._settings.forget_keyring(current.url)
        with self._lock:
            self._last_error = ""
            self._assets.clear()
            self._newest = None
        log.info("settings saved through the settings form")

    def list_recent(self, limit: int) -> list[dict[str, object]]:
        client, model = self._client()
        # Network and disk work stay outside the lock: a long download must
        # not hold up a listing. The cache writes atomically on its own.
        try:
            assets = client.recent(limit, camera_model=model)[:limit]
        except ImmichError as error:
            raise self._failed(error) from None
        with self._lock:
            self._last_error = ""
            self._assets.update((asset.id, asset) for asset in assets)
            newest = assets[0].id if assets else ""
            changed = self._newest is not None and newest != self._newest
            self._newest = newest
        entries = [self._entry(client, asset) for asset in assets]
        if changed:
            self._to_main(self.Changed)
        return entries

    def _entry(self, client: ImmichClient, asset: Asset) -> dict[str, object]:
        thumbnail = self._cache.thumbnail(asset.id)
        if thumbnail is None:
            try:
                thumbnail = self._cache.store_thumbnail(asset.id, client.thumbnail(asset.id))
            except ImmichError as error:
                # One missing thumbnail must not hide the whole list.
                log.debug("thumbnail unavailable: %s", error.token)
        original = self._cache.original(asset.id)
        return {
            "id": asset.id,
            "taken_at": asset.taken_at,
            "type": asset.type,
            "thumbnail": str(thumbnail or ""),
            "original": str(original or ""),
        }

    def fetch_original(self, photo_id: str) -> str:
        try:
            cached = self._cache.original(photo_id)
        except ValueError:
            raise PluginCallError("not a photo id") from None
        if cached is not None:
            return str(cached)
        client, _model = self._client()
        with self._lock:
            asset = self._assets.get(photo_id)
        try:
            if asset is None:
                # Not listed by this process (it may have idled out since).
                asset = client.asset(photo_id)
            path = self._cache.store_original(
                photo_id, asset.file_name,
                lambda stream: client.download_original(photo_id, stream),
                kind=asset.type,
            )
        except ImmichError as error:
            raise self._failed(error) from None
        return str(path)
