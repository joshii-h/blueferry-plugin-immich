"""Keep tests away from the user's configuration, cache and session bus."""
from __future__ import annotations

import os
import tempfile

_scratch = tempfile.mkdtemp(prefix="blueferry-immich-tests-")
for _variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
    os.environ[_variable] = os.path.join(_scratch, _variable.lower())
    os.makedirs(os.environ[_variable], mode=0o700, exist_ok=True)
os.environ["XDG_DATA_DIRS"] = os.path.join(_scratch, "system")
# No test may reach a real bus: the plugin is exercised in-process.
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/blueferry-immich-tests"
