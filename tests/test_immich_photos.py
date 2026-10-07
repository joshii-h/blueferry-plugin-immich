"""The bundled Immich plugin with fake HTTP, keyring and cache; no network."""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import stat
import urllib.error
from pathlib import Path

import pytest
from blueferry.plugin_api.client import PluginClient, PluginError
from blueferry.plugin_api.manifest import parse_manifest
from blueferry.plugin_api.testing import ServiceTransport, inline_service

from blueferry_immich_photos import PLUGIN_ID, manifest_text
from blueferry_immich_photos import __main__ as cli
from blueferry_immich_photos.cache import PhotoCache, safe_file_name
from blueferry_immich_photos.immich import (
    ImmichClient,
    ImmichError,
    _NoRedirect,
    normalize_url,
)
from blueferry_immich_photos.service import ImmichPhotosService
from blueferry_immich_photos.settings import Settings, SettingsError, SettingsStore

ID_A = "3f1c2d4e-0000-4000-8000-00000000000a"
ID_B = "3f1c2d4e-0000-4000-8000-00000000000b"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _Server:
    """Answers urllib requests like an Immich server would."""

    def __init__(self, items: list[dict] | None = None, status: int = 200) -> None:
        self.items = items if items is not None else [
            {"id": ID_A, "type": "IMAGE", "fileCreatedAt": "2026-10-06T16:21:00.000Z",
             "originalFileName": "IMG_0001.HEIC"},
            {"id": ID_B, "type": "VIDEO", "fileCreatedAt": "2026-10-05T10:00:00.000Z",
             "originalFileName": "../../evil\x1b.MOV"},
            {"id": "bad id", "type": "IMAGE"},
        ]
        self.status = status
        self.requests: list = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        assert timeout <= 60
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "x", {}, None)
        url = request.full_url
        if url.endswith("/api/search/metadata"):
            return _Response(json.dumps({"assets": {"items": self.items}}).encode())
        if "/thumbnail" in url:
            return _Response(b"thumb-" + url.split("/")[-2].encode())
        if url.endswith("/original"):
            return _Response(b"original-bytes" * 10)
        raise AssertionError(url)


@pytest.mark.parametrize("raw,expected", [
    ("https://photos.example.org/", "https://photos.example.org"),
    ("https://photos.example.org/api", "https://photos.example.org"),
    ("http://localhost:2283", "http://localhost:2283"),
])
def test_urls_are_normalized(raw, expected) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "http://photos.example.org", "ftp://x", "https://user:pw@photos.example.org",
    "https://photos.example.org/?a=1", "photos.example.org",
])
def test_unsafe_urls_are_refused(raw) -> None:
    with pytest.raises(ImmichError):
        normalize_url(raw)


def test_recent_assets_use_the_metadata_search_with_the_api_key() -> None:
    server = _Server()
    client = ImmichClient("https://photos.example.org", "secret-key", open_url=server)
    assets = client.recent(5, camera_model="iPhone 16 Pro")
    assert [asset.id for asset in assets] == [ID_A, ID_B]
    assert assets[1].type == "video" and assets[0].taken_at.startswith("2026-10-06")
    request = server.requests[0]
    assert request.get_header("X-api-key") == "secret-key"
    assert json.loads(request.data) == {
        "size": 5, "order": "desc", "withExif": False, "model": "iPhone 16 Pro",
    }
    assert client.thumbnail(ID_A) == b"thumb-" + ID_A.encode()
    assert server.requests[-1].full_url.endswith(f"/api/assets/{ID_A}/thumbnail?size=thumbnail")


@pytest.mark.parametrize("status,token", [(401, "unauthorized"), (403, "forbidden"),
                                          (500, "server-error")])
def test_http_errors_become_tokens(status, token) -> None:
    client = ImmichClient("https://p.example.org", "k", open_url=_Server(status=status))
    with pytest.raises(ImmichError) as caught:
        client.recent(1)
    assert caught.value.token == token


def test_redirects_are_refused_so_the_key_stays_on_the_server() -> None:
    with pytest.raises(ImmichError, match="redirect"):
        _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example")


def test_oversized_answers_are_refused(monkeypatch) -> None:
    from blueferry_immich_photos import immich

    monkeypatch.setattr(immich, "MAX_THUMBNAIL_BYTES", 4)
    client = ImmichClient("https://p.example.org", "k", open_url=_Server())
    with pytest.raises(ImmichError, match="too-large"):
        client.thumbnail(ID_A)


def test_cache_is_private_and_prunes_least_recently_used(tmp_path) -> None:
    cache = PhotoCache(tmp_path / "blueferry" / "immich", thumbnail_budget=10)
    first = cache.store_thumbnail("a", b"123456")
    os.utime(first, (1, 1))
    second = cache.store_thumbnail("b", b"123456")
    assert not first.exists() and second.exists()
    assert stat.S_IMODE(second.stat().st_mode) == 0o600
    assert stat.S_IMODE(second.parent.stat().st_mode) == 0o700
    assert cache.thumbnail("a") is None and cache.thumbnail("b") == second
    with pytest.raises(ValueError):
        cache.thumbnail("../x")
    assert safe_file_name("../../evil\x1b.MOV", "id", "video") == "evil_.mov"
    assert safe_file_name("IMG_1.HEIC", "id") == "IMG_1.heic"
    # The extension picks the program that opens it: only media survive.
    assert safe_file_name("x.desktop", "id") == "x.jpg"
    assert safe_file_name("page.html", "id", "video") == "page.mov"
    assert safe_file_name("...", "id") == "id.jpg"


def test_settings_fall_back_to_an_owner_only_key_file(tmp_path) -> None:
    store = SettingsStore(tmp_path / "conf", secret=None)
    store._module = lambda: None  # no libsecret
    where = store.save(Settings("https://p.example.org", "keyring"), "k3y")
    assert where == "file"
    assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o600
    settings = store.load()
    assert settings is not None and store.api_key(settings) == "k3y"
    store.key_path.chmod(0o644)
    with pytest.raises(SettingsError, match="readable by other users"):
        store.api_key(settings)
    store.forget()
    assert store.load() is None


class _FakeSecret:
    COLLECTION_DEFAULT = "default"

    class SchemaFlags:
        NONE = 0

    class SchemaAttributeType:
        STRING = 0

    class Schema:
        @staticmethod
        def new(name, _flags, _attributes):
            return name

    def __init__(self) -> None:
        self.items: dict[tuple, str] = {}

    def password_store_sync(self, schema, attributes, _collection, _label, value, _c):
        self.items[(schema, tuple(attributes.items()))] = value
        return True

    def password_lookup_sync(self, schema, attributes, _c):
        return self.items.get((schema, tuple(attributes.items())))

    def password_clear_sync(self, schema, attributes, _c):
        return self.items.pop((schema, tuple(attributes.items())), None) is not None


def test_settings_prefer_the_keyring(tmp_path) -> None:
    secret = _FakeSecret()
    store = SettingsStore(tmp_path / "conf", secret=secret)
    assert store.save(Settings("https://p.example.org", "keyring"), "k3y") == "keyring"
    assert not store.key_path.exists()
    assert "k3y" not in store.config_path.read_text()
    assert store.api_key(store.load()) == "k3y"
    store.forget()
    assert secret.items == {}


@pytest.fixture
def plugin(tmp_path):
    secret = _FakeSecret()
    store = SettingsStore(tmp_path / "conf", secret=secret)
    server = _Server()
    cache_root = tmp_path / "cache" / "blueferry"
    manifest = parse_manifest(manifest_text())
    service = inline_service(
        ImmichPhotosService, manifest,
        settings=store,
        cache=PhotoCache(cache_root / "immich"),
        client_factory=lambda url, key: ImmichClient(url, key, open_url=server),
    )
    client = PluginClient(manifest, transport=ServiceTransport(service),
                          cache_root=lambda: cache_root)
    return store, server, service, client


def test_unconfigured_plugin_explains_the_setup(plugin) -> None:
    _store, _server, _service, client = plugin
    status = client.status()
    assert status.state == "unconfigured"
    assert "blueferry plugins immich setup --url" in status.detail
    with pytest.raises(PluginError, match="not configured"):
        client.list_recent(5)


def test_recent_photos_and_originals_end_to_end(plugin) -> None:
    store, server, service, client = plugin
    store.save(Settings("https://photos.example.org", "keyring"), "k3y")
    changed: list[bool] = []
    service.Changed = lambda: changed.append(True)
    photos = client.list_recent(10)
    assert [(p.id, p.type) for p in photos] == [(ID_A, "image"), (ID_B, "video")]
    assert photos[0].thumbnail is not None and photos[0].thumbnail.read_bytes().startswith(b"thumb")
    assert photos[0].original is None
    path = client.fetch_original(ID_B)
    assert path.name == "evil_.mov" and path.read_bytes().startswith(b"original")
    assert client.status().server == "photos.example.org"
    # Cached: listing again neither refetches thumbnails nor the original.
    before = len(server.requests)
    photos = client.list_recent(10)
    assert len(server.requests) == before + 1
    assert photos[1].original == path
    assert changed == []
    server.items.insert(0, {"id": "3f1c2d4e-0000-4000-8000-00000000000c", "type": "IMAGE"})
    client.list_recent(10)
    assert changed == [True]


def test_original_of_an_unlisted_asset_asks_the_server_for_its_name(plugin) -> None:
    store, server, service, client = plugin
    store.save(Settings("https://photos.example.org", "keyring"), "k3y")
    original_call = server.__call__

    def answer(request, timeout):
        if request.full_url.endswith(f"/api/assets/{ID_A}"):
            return _Response(json.dumps({"id": ID_A, "type": "IMAGE",
                                         "originalFileName": "IMG_9.PNG"}).encode())
        return original_call(request, timeout)

    service._client_factory = lambda url, key: ImmichClient(url, key, open_url=answer)
    assert client.fetch_original(ID_A).name == "IMG_9.png"
    assert service.in_flight == 0


def test_server_errors_reach_the_client_as_text(plugin) -> None:
    store, server, _service, client = plugin
    store.save(Settings("https://photos.example.org", "keyring"), "k3y")
    server.status = 401
    with pytest.raises(PluginError, match="API key was rejected"):
        client.list_recent(3)
    assert client.status().state == "error"


def test_setup_stores_the_key_and_installs_activation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "system"))
    monkeypatch.setattr(cli, "_command", lambda: ["/opt/bin/blueferry-immich-photos"])
    monkeypatch.setattr("sys.stdin", io.StringIO("k3y\n"))
    store = SettingsStore(tmp_path / "conf", secret=_FakeSecret())
    args = argparse.Namespace(url="https://photos.example.org/", api_key_stdin=True,
                              no_verify=True, camera_model="", key_file=False)
    assert cli.setup(args, store) == 0
    plugin_file = tmp_path / "data" / "blueferry" / "plugins" / f"{PLUGIN_ID}.plugin"
    installed = parse_manifest(plugin_file.read_text())
    assert installed.exec == ("/opt/bin/blueferry-immich-photos", "serve")
    service_file = next((tmp_path / "data" / "dbus-1" / "services").iterdir())
    assert service_file.name == f"{installed.bus_name}.service"
    assert "Exec=/opt/bin/blueferry-immich-photos serve" in service_file.read_text()
    assert "k3y" not in plugin_file.read_text() + service_file.read_text()
    args.url = "http://photos.example.org"
    assert cli.setup(args, store) == 2


def test_plugin_imports_only_the_plugin_api_from_blueferry() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                if module == "blueferry" or module.startswith("blueferry."):
                    assert module.startswith("blueferry.plugin_api"), (path.name, module)


def test_manifest_describes_the_settings_form() -> None:
    manifest = parse_manifest(manifest_text())
    assert manifest.api_minor == 1 and manifest.version == "0.2.1"
    fields = {field.key: field for field in manifest.config}
    assert list(fields) == ["url", "api_key", "camera_model"]
    assert fields["url"].type == "url" and fields["url"].required
    assert fields["api_key"].secret and fields["api_key"].required
    assert not fields["camera_model"].required


def test_settings_form_checks_the_key_and_never_reveals_it(plugin) -> None:
    from blueferry.plugin_api.config import SECRET_MASK

    store, server, _service, client = plugin
    result = client.set_config({"url": "https://photos.example.org"})
    assert not result.ok and result.errors == {"api_key": "is required"}
    server.status = 401
    result = client.set_config({"url": "https://photos.example.org", "api_key": "bad"})
    assert result.errors == {"api_key": "the API key was rejected"}
    assert store.load() is None
    server.status = 200
    result = client.set_config({"url": "https://photos.example.org/", "api_key": "k3y",
                                "camera_model": "iPhone 16 Pro"})
    assert result.ok, result.errors
    settings = store.load()
    assert settings.url == "https://photos.example.org"
    assert settings.camera_model == "iPhone 16 Pro" and store.api_key(settings) == "k3y"
    shown = client.get_config()
    assert shown == {"url": "https://photos.example.org", "api_key": SECRET_MASK,
                     "camera_model": "iPhone 16 Pro"}
    result = client.set_config({"url": "http://photos.example.org"})
    assert list(result.errors) == ["url"] and "https://" in result.errors["url"]


def test_a_new_url_keeps_the_stored_key_in_the_same_keyring_entry(plugin) -> None:
    """A configuration made with `setup` (0.1) keeps working after the move."""
    store, _server, _service, client = plugin
    store.save(Settings("https://old.example.org", "keyring"), "k3y")
    secret = store._secret
    assert ("io.weirdware.blueferry.immich_photos.ApiKey",
            (("server", "https://old.example.org"),)) in secret.items
    assert client.get_config()["api_key"] == "********"
    result = client.set_config({"url": "https://new.example.org", "api_key": "********"})
    assert result.ok, result.errors
    settings = store.load()
    assert settings.url == "https://new.example.org" and store.api_key(settings) == "k3y"
    assert [attributes for _schema, attributes in secret.items] == [
        (("server", "https://new.example.org"),)]


def test_activation_prefers_the_running_environment(tmp_path, monkeypatch) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    script = venv_bin / "blueferry-immich-photos"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr("sys.executable", str(venv_bin / "python"))
    monkeypatch.setattr("shutil.which", lambda _name: "/home/me/.local/bin/blueferry-immich-photos")
    assert cli._command() == [str(script)]
    script.unlink()
    assert cli._command() == ["/home/me/.local/bin/blueferry-immich-photos"]
