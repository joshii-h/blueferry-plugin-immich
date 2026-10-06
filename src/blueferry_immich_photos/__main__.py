"""``blueferry-immich-photos serve|setup|status|forget``."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import shlex
import shutil
import sys
from pathlib import Path

from blueferry.plugin_api.manifest import ManifestError, default_directories, parse_manifest
from blueferry.plugin_api.service import run
from blueferry_immich_photos import PLUGIN_ID, manifest_text
from blueferry_immich_photos.immich import ImmichClient, ImmichError, normalize_url
from blueferry_immich_photos.service import _ERROR_TEXT, ImmichPhotosService
from blueferry_immich_photos.settings import Settings, SettingsError, SettingsStore

ENTRY_POINT = "blueferry-immich-photos"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _command() -> list[str]:
    """How the bus and BlueFerry should start this plugin."""
    installed = shutil.which(ENTRY_POINT)
    if installed:
        return [installed]
    # Source checkout: carry the import path of this package and of
    # blueferry.plugin_api, which may live in different trees.
    import blueferry.plugin_api as api

    roots = [
        str(Path(__file__).resolve().parents[1]),
        str(Path(api.__file__).resolve().parents[2]),
    ]
    return [
        "/usr/bin/env", "PYTHONPATH=" + ":".join(dict.fromkeys(roots)),
        sys.executable, "-m", "blueferry_immich_photos",
    ]


def install_activation(data_home: Path | None = None) -> list[Path]:
    """Write the user manifest and D-Bus service file unless the system has them."""
    data_home = data_home or _data_home()
    template = parse_manifest(manifest_text())
    written: list[Path] = []
    system = [d for d in default_directories() if not str(d).startswith(str(data_home))]
    if not any((directory / f"{PLUGIN_ID}.plugin").exists() for directory in system):
        command = _command()
        text = manifest_text().replace(
            "Exec=blueferry-immich-photos serve", "Exec=" + shlex.join([*command, "serve"]),
        ).replace("Cli=blueferry-immich-photos", "Cli=" + shlex.join(command))
        parse_manifest(text)  # never install something clients would ignore
        target = data_home / "blueferry" / "plugins" / f"{PLUGIN_ID}.plugin"
        _write(target, text)
        written.append(target)
        service = data_home / "dbus-1" / "services" / f"{template.bus_name}.service"
        _write(service, "[D-BUS Service]\nName={}\nExec={}\n".format(
            template.bus_name, shlex.join([*command, "serve"]),
        ))
        written.append(service)
    return written


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, path)


def setup(args: argparse.Namespace, store: SettingsStore | None = None) -> int:
    store = store or SettingsStore()
    try:
        url = normalize_url(args.url)
    except ImmichError:
        print("The URL must start with https:// (http only for localhost).", file=sys.stderr)
        return 2
    if args.api_key_stdin:
        key = sys.stdin.readline().strip()
    else:
        print("Create an API key in Immich: Account Settings > API Keys, with")
        print("asset.read, asset.view and asset.download.")
        key = getpass.getpass("Immich API key (input hidden): ").strip()
    if not key or len(key) > 512 or any(ch.isspace() for ch in key):
        print("That does not look like an API key.", file=sys.stderr)
        return 2
    if not args.no_verify:
        try:
            ImmichClient(url, key).recent(1, camera_model=args.camera_model or "")
        except ImmichError as error:
            print(f"Check failed: {_ERROR_TEXT.get(error.token, error.token)}.", file=sys.stderr)
            return 1
    try:
        where = store.save(
            Settings(url=url, key_store="keyring", camera_model=args.camera_model or ""),
            key, prefer_keyring=not args.key_file,
        )
    except (SettingsError, OSError) as error:
        print(f"Could not store the settings: {error}", file=sys.stderr)
        return 1
    print("API key stored in the desktop keyring." if where == "keyring"
          else f"API key stored in {store.key_path} (owner-only).")
    for path in install_activation():
        print(f"Installed {path}")
    print("Done. Open the Photos tab in BlueFerry or run: blueferry photos recent")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=ENTRY_POINT, description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve on the session bus (started by D-Bus)")
    set_up = commands.add_parser("setup", help="store the server URL and API key")
    set_up.add_argument("--url", required=True)
    set_up.add_argument("--camera-model", default="",
                        help="only list assets from this camera model, e.g. 'iPhone 16 Pro'")
    set_up.add_argument("--key-file", action="store_true",
                        help="store the key in an owner-only file instead of the keyring")
    set_up.add_argument("--api-key-stdin", action="store_true",
                        help="read the key from standard input (for scripts)")
    set_up.add_argument("--no-verify", action="store_true",
                        help="do not test the key against the server")
    commands.add_parser("status", help="show whether the plugin is configured")
    commands.add_parser("forget", help="remove the stored key and settings")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "setup":
        return setup(args)
    if args.command == "forget":
        SettingsStore().forget()
        print("Removed the stored Immich URL and API key.")
        return 0
    manifest = parse_manifest(manifest_text())
    if args.command == "status":
        try:
            settings = SettingsStore().load()
        except SettingsError as error:
            print(error)
            return 1
        print(f"Configured for {settings.url}" if settings else "Not configured.")
        return 0
    try:
        return run(lambda bus: ImmichPhotosService(manifest, bus))
    except ManifestError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
