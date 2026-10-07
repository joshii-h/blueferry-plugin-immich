"""Keep tests away from the user's configuration, cache and session bus."""
from __future__ import annotations

from blueferry_plugin_kit.testing import isolate_environment

# No test may reach a real bus: the plugin is exercised in-process.
isolate_environment("blueferry-immich-tests-", bus_name="blueferry-immich-tests")
