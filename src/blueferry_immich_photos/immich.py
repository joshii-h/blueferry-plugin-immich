"""Minimal Immich REST client (API 1.1xx, 2.x and 3.x).

Endpoints, all with the ``x-api-key`` header:

* ``POST /api/search/metadata`` with ``{"size": n, "order": "desc"}`` for the
  newest assets (``assets.items``). ``order``/``model`` are deprecated since
  3.2 in favour of ``orderBy``/``filter`` but still accepted; older servers
  know only these. Needs the key permission ``asset.read``.
* ``GET /api/assets/{id}/thumbnail?size=thumbnail`` (``asset.view``).
* ``GET /api/assets/{id}/original`` (``asset.download``).
* For "Test connection": ``GET /api/server/version`` (public) and
  ``GET /api/users/me`` (``user.read``, optional: without it the test
  names no user).

Every request has a timeout, redirects are refused (they would carry the API
key to another host), and every response body has a size limit. Errors are
reduced to short tokens; nothing from the server is logged.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any

from blueferry_immich_photos import __version__

TIMEOUT_SEC = 20.0
DOWNLOAD_TIMEOUT_SEC = 60.0
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 4 * 1024 * 1024
MAX_ORIGINAL_BYTES = 8 * 1024 * 1024 * 1024
_CHUNK = 256 * 1024
ASSET_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class ImmichError(Exception):
    """``token`` is one of: unauthorized, forbidden, not-found, server-error,
    network, too-large, bad-response, redirect, invalid-url."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


def normalize_url(raw: str) -> str:
    """The server's base URL: https (http only on loopback), no credentials."""
    value = raw.strip().rstrip("/")
    if value.endswith("/api"):
        value = value[:-4]
    parts = urllib.parse.urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host:
        raise ImmichError("invalid-url")
    if parts.scheme == "http" and host not in _LOOPBACK:
        raise ImmichError("invalid-url")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ImmichError("invalid-url")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise ImmichError("redirect")


_opener = urllib.request.build_opener(_NoRedirect)
Open = Callable[[urllib.request.Request, float], Any]


def _open(request: urllib.request.Request, timeout: float) -> Any:
    return _opener.open(request, timeout=timeout)  # nosec B310


@dataclass(frozen=True, slots=True)
class Asset:
    id: str
    type: str          # image, video or other
    taken_at: str      # ISO 8601 as sent by the server
    file_name: str     # the server's originalFileName, untrusted


class ImmichClient:
    def __init__(self, base_url: str, api_key: str, *, open_url: Open = _open) -> None:
        self.base_url = normalize_url(base_url)
        self._key = api_key
        self._open = open_url

    def _request(self, path: str, *, body: object = None, accept: str) -> urllib.request.Request:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # nosec B310 - scheme checked in normalize_url
            self.base_url + path, data=data, method="POST" if data else "GET",
        )
        request.add_header("x-api-key", self._key)
        request.add_header("Accept", accept)
        request.add_header("User-Agent", f"blueferry-immich-photos/{__version__}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return request

    def _call(self, request: urllib.request.Request, timeout: float) -> Any:
        try:
            return self._open(request, timeout)
        except ImmichError:
            raise
        except urllib.error.HTTPError as error:
            raise ImmichError(_status_token(error.code)) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise ImmichError("network") from None

    def recent(self, limit: int, *, camera_model: str = "") -> list[Asset]:
        body: dict[str, object] = {
            "size": max(1, min(int(limit), 1000)), "order": "desc", "withExif": False,
        }
        if camera_model:
            body["model"] = camera_model
        with self._call(
            self._request("/api/search/metadata", body=body, accept="application/json"),
            TIMEOUT_SEC,
        ) as response:
            raw = _read_limited(response, MAX_JSON_BYTES)
        try:
            items = json.loads(raw)["assets"]["items"]
        except (ValueError, KeyError, TypeError):
            raise ImmichError("bad-response") from None
        if not isinstance(items, list):
            raise ImmichError("bad-response")
        return [asset for asset in map(_asset, items) if asset is not None]

    def _json(self, path: str) -> object:
        with self._call(self._request(path, accept="application/json"), TIMEOUT_SEC) as response:
            raw = _read_limited(response, MAX_JSON_BYTES)
        try:
            return json.loads(raw)
        except ValueError:
            raise ImmichError("bad-response") from None

    def server_version(self) -> str:
        """``1.135.3`` from ``GET /api/server/version``."""
        value = self._json("/api/server/version")
        if not isinstance(value, dict):
            raise ImmichError("bad-response")
        parts = [value.get(key) for key in ("major", "minor", "patch")]
        if not all(isinstance(part, int) and not isinstance(part, bool) for part in parts):
            raise ImmichError("bad-response")
        return ".".join(str(part) for part in parts)

    def user_name(self) -> str:
        """The key owner's name, else e-mail (``GET /api/users/me``)."""
        value = self._json("/api/users/me")
        if not isinstance(value, dict):
            raise ImmichError("bad-response")
        for key in ("name", "email"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
        return ""

    def asset(self, asset_id: str) -> Asset:
        """One asset's metadata (``GET /api/assets/{id}``, ``asset.read``)."""
        _check_id(asset_id)
        with self._call(
            self._request(f"/api/assets/{asset_id}", accept="application/json"), TIMEOUT_SEC,
        ) as response:
            raw = _read_limited(response, MAX_JSON_BYTES)
        try:
            found = _asset(json.loads(raw))
        except ValueError:
            raise ImmichError("bad-response") from None
        if found is None or found.id != asset_id:
            raise ImmichError("not-found")
        return found

    def thumbnail(self, asset_id: str) -> bytes:
        _check_id(asset_id)
        request = self._request(
            f"/api/assets/{asset_id}/thumbnail?size=thumbnail", accept="image/*",
        )
        with self._call(request, TIMEOUT_SEC) as response:
            return _read_limited(response, MAX_THUMBNAIL_BYTES)

    def download_original(self, asset_id: str, target: IO[bytes]) -> int:
        _check_id(asset_id)
        request = self._request(
            f"/api/assets/{asset_id}/original", accept="application/octet-stream",
        )
        written = 0
        with self._call(request, DOWNLOAD_TIMEOUT_SEC) as response:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    return written
                written += len(chunk)
                if written > MAX_ORIGINAL_BYTES:
                    raise ImmichError("too-large")
                target.write(chunk)


def _check_id(asset_id: str) -> None:
    if not ASSET_ID.fullmatch(asset_id):
        raise ImmichError("not-found")


def _status_token(code: int) -> str:
    if code == 401:
        return "unauthorized"
    if code == 403:
        return "forbidden"
    if code == 404:
        return "not-found"
    return "server-error"


def _read_limited(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ImmichError("too-large")
    return bytes(data)


_TYPES = {"IMAGE": "image", "VIDEO": "video"}


def _asset(item: object) -> Asset | None:
    if not isinstance(item, dict):
        return None
    asset_id = item.get("id")
    if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
        return None
    if item.get("isTrashed") is True:
        return None
    taken = item.get("fileCreatedAt") or item.get("localDateTime") or ""
    name = item.get("originalFileName")
    return Asset(
        id=asset_id,
        type=_TYPES.get(str(item.get("type")), "other"),
        taken_at=taken if isinstance(taken, str) else "",
        file_name=name if isinstance(name, str) else "",
    )
