"""Manifest Version=, pyproject and __version__ name the same release."""
from __future__ import annotations

from pathlib import Path

from blueferry_plugin_kit.testing import check_versions

from blueferry_immich_photos import __version__


def test_versions_match() -> None:
    assert check_versions(Path(__file__).resolve().parent.parent, __version__) == "0.3.0"
