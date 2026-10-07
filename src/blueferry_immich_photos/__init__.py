"""BlueFerry plugin: recent photos from a self-hosted Immich server.

Imports only ``blueferry.plugin_api`` from BlueFerry, so this directory can
move into its own repository unchanged.
"""
from __future__ import annotations

from importlib import resources

PLUGIN_ID = "io.weirdware.blueferry.immich_photos"
__version__ = "0.3.1"


def manifest_text() -> str:
    return (
        resources.files(__name__).joinpath(f"{PLUGIN_ID}.plugin").read_text(encoding="utf-8")
    )
