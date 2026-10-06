# blueferry-plugin-immich

Recent iPhone photos from your Immich server in BlueFerry.

A plugin for [BlueFerry](https://github.com/joshii-h/blueferry) that shows the
newest photos and videos from a self-hosted [Immich](https://immich.app)
server in BlueFerry's clients: the Photos tab of the Qt app, the terminal
client (`g`) and `blueferry photos`. It runs as its own process on the
session bus and talks to BlueFerry only through `blueferry.plugin_api`
(see `PLUGINS.md` in the BlueFerry repository).

## Install

With a BlueFerry that has plugin management (`blueferry plugins install`):

```sh
blueferry plugins install https://github.com/joshii-h/blueferry-plugin-immich
```

or open BlueFerry's settings, Plugins, and pick "Immich photos" from the
list. BlueFerry shows the source, the version tag and commit, the
capabilities and the command it will run, and installs only after you
confirm. The plugin gets its own virtual environment below
`~/.local/share/blueferry/plugins/`. Update with
`blueferry plugins update io.weirdware.blueferry.immich_photos`, remove with
`blueferry plugins remove io.weirdware.blueferry.immich_photos`.

## Configure

1. In Immich, open Account Settings > API Keys and create a key with the
   permissions **asset.read**, **asset.view** and **asset.download**
   (nothing else is needed).
2. In BlueFerry's settings, Plugins > Immich photos > Settings, enter the
   server URL and the key. Or on the command line:

   ```sh
   blueferry plugins config io.weirdware.blueferry.immich_photos \
       --set url=https://photos.example.org --secret api_key
   ```

   The older `blueferry plugins immich setup --url https://photos.example.org`
   still works.

| Setting | Meaning |
| --- | --- |
| `url` | Your Immich address; `https://` (plain `http://` only for localhost). |
| `api_key` | The API key; checked against the server before it is stored. |
| `camera_model` | Optional: only list assets from this camera, e.g. `iPhone 16 Pro`. Immich 3.x no longer reports the uploading device, so the camera model is the closest filter. |

The key goes to the desktop keyring (Secret Service); without one, to an
owner-only file. It never appears in logs, the manifest, D-Bus replies or
command lines, and the settings form only shows whether one is stored.
The URL lives in `~/.config/blueferry/plugins/io.weirdware.blueferry.immich_photos/`.
`blueferry-immich-photos forget` removes the URL and the key.

Thumbnails (up to 200 MB) and opened originals (up to 2 GB) are cached in
`~/.cache/blueferry/immich` (owner-only) and pruned least recently used
first.

## Develop

```sh
python3 -m venv --system-site-packages .venv   # dbus-python, PyGObject, libsecret from the system
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

`blueferry-plugin-api` comes from the `plugin-api` directory of the
BlueFerry repository. Tests use fake HTTP, keyring and cache; the plugin has
not been tested against a live Immich server yet.

## License

GPL-2.0-or-later, like BlueFerry. See `LICENSE`.
