# SteamOS Remote

SteamOS Remote is a Decky Loader host plugin for controlling and monitoring a
Steam Deck from the companion [Omarchy client](https://github.com/tuthan/omarchy-steam-remote)
over a local network.

This repository contains the Decky host in `host/` and the shared protocol
contract in `protocol/`.

## Install

Decky Loader must already be installed.

### Decky UI

1. Download `steamos-remote-decky-<version>.zip` from the
   [Releases](https://github.com/tuthan/decky-steam-remote/releases) page.
2. Install the ZIP with Decky’s plugin installer.
3. If needed, use Decky’s **Reload Plugins** action.

No reboot or Decky service restart is required.

### SSH

From a local checkout, install the latest release with:

```sh
ssh deck@steamdeck.local 'bash -s' < install.sh
```

To install a specific release:

```sh
ssh deck@steamdeck.local 'bash -s -- v0.4.0' < install.sh
```

The installer copies the plugin to `~/homebrew/plugins/steamos-remote` and
asks Decky to reload its plugins. It does not require root or `sudo`. If the
reload endpoint is unavailable, use Decky’s **Reload Plugins** action.

The installer can also be run directly on the Deck:

```sh
curl -fsSL https://raw.githubusercontent.com/tuthan/decky-steam-remote/main/install.sh | bash -s -- v0.4.0
```

## Pairing

Open the plugin settings on the Deck, then pair from the Omarchy client and
approve the pending request in Decky. The host creates its identity and TLS
certificate automatically on first use. The listener accepts IPv4 connections
on all interfaces by default; set the host/IP override only when needed.

Sunshine monitoring is disabled by default and can only be enabled locally in
Decky settings.

## Releases

Update the version in `host/package.json`, commit the change, and push a
matching tag:

```sh
git tag v0.4.1
git push origin v0.4.1
```

The `Release` workflow runs the checks, builds the ZIP and SHA256 file, and
attaches both to the GitHub release. The tag must match the package version
without the leading `v`.

## Development

Run the checks and build locally:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q host protocol
node --check host/frontend/index.js
python3 host/build.py
```

The build writes `artifacts/steamos-remote-decky-<version>.zip` and its
`SHA256` file.

## License

[MIT](LICENSE)
