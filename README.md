# SteamOS Remote

SteamOS Remote is a Decky Loader plugin for controlling and monitoring another
Steam Deck from the companion [Omarchy client](https://github.com/tuthan/omarchy-steam-remote)
over a local network. The same install can run as a Client, Server, or Both.

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
ssh deck@steamdeck.local 'bash -s -- v0.5.0' < install.sh
```

The installer copies the plugin to `~/homebrew/plugins/steamos-remote` and
asks Decky to reload its plugins. Run it as the `deck` user; `sudo` is used to
create, replace, and update the Decky plugin directory. If the reload endpoint
is unavailable, use Decky’s **Reload Plugins** action.

The installer can also be run directly on the Deck:

```sh
curl -fsSL https://raw.githubusercontent.com/tuthan/decky-steam-remote/main/install.sh | bash -s -- v0.5.0
```

## Device modes

On first launch, choose the role that matches the device:

- **Client** controls one saved remote device. Discovery is an explicit,
  bounded local-network scan; manual HTTPS entry is available when discovery
  is not suitable.
- **Server** accepts authenticated requests from paired clients and keeps the
  Decky display bridge alive while the settings panel is closed.
- **Both** enables both roles independently.

Changing roles is an explicit saved setting. Existing host identities,
listener settings, incoming clients, and recovery profiles are preserved when
Client mode is added. Pending display recovery is allowed to finish (or can be
cancelled from Settings) before the Server role is stopped. A saved outgoing
pairing and unresolved operation journal survive Client mode being disabled.

The Client destination has Remote, Display settings, Power options, and
Connection details flows. Display previews always offer Revert first, power
actions require a confirmation, and an ambiguous mutation is never replayed
automatically; the user must acknowledge an explicit resend.

## Pairing

Enable Server or Both on the target device, then discover it from the Client
screen or enter its address manually. The client pins the target certificate,
shows a certificate-bound comparison code, and keeps a pending request across
panel close and plugin reload. Approve the request on the target only when both
codes match. The host creates its identity and TLS certificate automatically on
first use. The listener accepts IPv4 connections on all interfaces by default;
set the host/IP override only when needed.

The target's incoming pairing card places Reject before Approve. Removing the
local pairing does not silently revoke server access; the Connection details
flow explains that distinction.

Sunshine monitoring is disabled by default and can only be enabled locally in
Decky settings.

The installable frontend is dependency-free: it uses the native Decky
component surface when the loader exposes it and retains semantic, accessible
fallback controls for older API-v0 loaders and the local test harness. No
runtime package is downloaded during build or install.

## Releases

Update the version in `host/package.json`, commit the change, and push a
matching tag:

```sh
git tag v0.5.1
git push origin v0.5.1
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
