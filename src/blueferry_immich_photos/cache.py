"""Owner-only file cache below ``$XDG_CACHE_HOME/blueferry/immich``.

Thumbnails and originals live in separate directories with separate size
budgets; the least recently used files go first. Access refreshes a file's
mtime, which is what the pruning sorts by.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

THUMBNAIL_BUDGET_BYTES = 200 * 1024 * 1024
ORIGINAL_BUDGET_BYTES = 2 * 1024 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ ()+-]")
_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def default_root() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(cache_home) / "blueferry" / "immich"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("cache directory has the wrong owner or type")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return path


# Originals are opened with the desktop's default handler, so the extension
# decides which program runs. Only media extensions are kept.
_EXTENSIONS = {
    "image": frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
                        ".tif", ".tiff", ".dng", ".avif", ".jxl", ".bmp"}),
    "video": frozenset({".mov", ".mp4", ".m4v", ".3gp", ".webm", ".mkv", ".avi"}),
}
_DEFAULT_EXTENSION = {"image": ".jpg", "video": ".mov"}


def safe_file_name(name: str, asset_id: str, kind: str = "image") -> str:
    """The server's file name, reduced to something harmless to open."""
    base = os.path.basename(name.replace("\\", "/")).strip().lstrip(".")
    stem, extension = os.path.splitext(_SAFE_NAME.sub("_", base))
    if extension.lower() not in _EXTENSIONS.get(kind, frozenset()):
        extension = _DEFAULT_EXTENSION.get(kind, ".bin")
    return (stem[:100] or asset_id) + extension.lower()


class PhotoCache:
    def __init__(
        self,
        root: Path | None = None,
        *,
        thumbnail_budget: int = THUMBNAIL_BUDGET_BYTES,
        original_budget: int = ORIGINAL_BUDGET_BYTES,
    ) -> None:
        self.root = root or default_root()
        self.thumbnail_budget = thumbnail_budget
        self.original_budget = original_budget
        self._lock = threading.Lock()

    def _dir(self, kind: str) -> Path:
        _private_dir(self.root.parent)
        _private_dir(self.root)
        return _private_dir(self.root / kind)

    @staticmethod
    def _check(asset_id: str) -> None:
        if not _ID.fullmatch(asset_id):
            raise ValueError("invalid asset id")

    # ---- thumbnails ----------------------------------------------------------

    def thumbnail(self, asset_id: str) -> Path | None:
        self._check(asset_id)
        return self._touch(self._dir("thumbnails") / asset_id)

    def store_thumbnail(self, asset_id: str, data: bytes) -> Path:
        self._check(asset_id)
        directory = self._dir("thumbnails")
        path = directory / asset_id
        self._write(directory, path, lambda stream: stream.write(data))
        self.prune("thumbnails", self.thumbnail_budget, keep=path)
        return path

    # ---- originals -----------------------------------------------------------

    def original(self, asset_id: str) -> Path | None:
        self._check(asset_id)
        folder = self._dir("originals") / asset_id
        try:
            names = [entry for entry in os.scandir(folder) if entry.is_file(follow_symlinks=False)
                     and not entry.name.startswith(".")]
        except OSError:
            return None
        return self._touch(Path(names[0].path)) if names else None

    def store_original(
        self, asset_id: str, file_name: str, writer: Callable[[IO[bytes]], object],
        *, kind: str = "image",
    ) -> Path:
        self._check(asset_id)
        folder = _private_dir(self._dir("originals") / asset_id)
        path = folder / safe_file_name(file_name, asset_id, kind)
        self._write(folder, path, writer)
        self.prune("originals", self.original_budget, keep=path)
        return path

    # ---- housekeeping --------------------------------------------------------

    def _touch(self, path: Path) -> Path | None:
        try:
            info = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        try:
            os.utime(path)
        except OSError:
            pass
        return path

    def _write(self, directory: Path, path: Path, writer: Callable[[IO[bytes]], object]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".part-", dir=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                writer(stream)
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            Path(temporary).unlink(missing_ok=True)

    def prune(self, kind: str, budget: int, *, keep: Path | None = None) -> None:
        """Delete least recently used files until ``kind`` fits ``budget``."""
        with self._lock:
            files: list[tuple[float, int, Path]] = []
            for directory, _subdirs, names in os.walk(self._dir(kind)):
                for name in names:
                    path = Path(directory) / name
                    try:
                        info = os.lstat(path)
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        files.append((info.st_mtime, info.st_size, path))
            total = sum(size for _mtime, size, _path in files)
            for _mtime, size, path in sorted(files):
                if total <= budget:
                    break
                if path == keep:
                    continue
                path.unlink(missing_ok=True)
                total -= size
                if kind == "originals" and path.parent != self._dir(kind):
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass
