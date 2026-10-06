# BlueFerry plugin: Immich photos

Shows the newest photos and videos from a self-hosted
[Immich](https://immich.app) server in BlueFerry's clients (Qt "Photos" tab,
terminal client, `blueferry photos`). It runs as its own process on the
session bus and imports only `blueferry.plugin_api`; see `PLUGINS.md` in the
BlueFerry repository for the contract.

## Setup

1. In Immich, create an API key (Account Settings > API Keys) with the
   permissions `asset.read`, `asset.view` and `asset.download`.
2. Run `blueferry plugins immich setup --url https://photos.example.org`
   (or `blueferry-immich-photos setup …` / `python -m blueferry_immich_photos setup …`)
   and paste the key; it is not echoed.

The key goes to the desktop keyring (Secret Service); without one, to an
owner-only file (`--key-file` forces that). Setup also installs the plugin
manifest and a D-Bus service file below `~/.local/share` unless a system
package already provides them. `--camera-model "iPhone 16 Pro"` limits the
list to one camera; Immich 3.x no longer reports the uploading device id, so
the camera model is the closest filter.

Thumbnails (up to 200 MB) and opened originals (up to 2 GB) are cached in
`~/.cache/blueferry/immich` (owner-only) and pruned least recently used first.
`blueferry-immich-photos forget` removes the stored URL and key.

Tested against the Immich OpenAPI description (3.3) with fake HTTP only; not
yet against a live server.
