# SteamOS Remote

SteamOS Remote is split into two independently installable projects:

- `host/` is the Decky host plugin. Its Python backend owns the v1 HTTPS API,
  pairing approval, scopes, private state, operation journal, display/power
  bridge, and optional narrow Sunshine provider adapter. Its JavaScript
  frontend is the only Steam API caller.
- `/home/hvo/Projects/omarchy-steam-remote/` is the native Omarchy bar widget
  and panel. It owns pinned HTTPS, private client state, visible polling, and
  local UDP Wake-on-LAN.

The disposable evidence-driven spike remains under `spike/` and is preserved.
The shared v1 schema and fixtures are under `protocol/`; the Omarchy project
contains a pinned copy and does not import the sibling checkout at runtime.

## Decky host

Run the dependency-free checks and build from this directory:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q host protocol
node --check host/frontend/index.js
python3 host/build.py
```

The build produces:

`/home/hvo/Projects/decky-steam-remote/artifacts/steamos-remote-decky-0.4.0.zip`

Install the archive through Decky's normal plugin installer. On first use, the
host creates a persistent random identity and self-signed certificate/key in
the Decky-provided private settings directory. The identity and certificate
are reused across restarts and upgrades, and Decky displays the certificate
fingerprint. The Omarchy client discovers the host, generates a random 16-byte
`verification_nonce`, and sends only that nonce. Each side then *derives* the
8-digit comparison code from the nonce and the host certificate fingerprint
with scrypt; the code itself is never transmitted. The client derives it from
the fingerprint it pinned, the host from its own certificate, so a
TLS-terminating LAN relay — which presents a different certificate — produces
different digits and the owner sees the mismatch. scrypt makes searching for a
nonce that forges a matching code infeasible inside the 120-second window.
Decky displays its derived code beside the pending client, with the remaining
120-second lifetime, so the owner can compare both screens before approving
it. The older client-chosen verification code is refused with
`400 pairing_method_unsupported`; hosts from 0.4.0 require an updated client.
Once approved, the client automatically stores the endpoint and TLS pin,
and the host sends its active-route NIC MAC as the wake target. Omarchy also
selects its own active sender interface; no MAC/interface save step is needed.
When Decky's toaster API is available, a background watcher also raises a
notification for a newly received request while the settings page is closed.
An advanced full pairing payload remains available for manual/offline bootstrap.

For troubleshooting, the backend writes startup and RPC failures through
Decky's plugin logger. The settings view displays the resolved `DECKY_PLUGIN_LOG`
path once `get_settings` responds; the frontend also emits rate-limited RPC
errors in the Decky browser console without logging pairing secrets.

The listener binds to `0.0.0.0` by default, so it accepts connections on all
IPv4 interfaces. A blank `Pairing host/IP override` is intentional: each
pairing payload advertises the IPv4 address selected by the OS for its active
route. Set the override only when the host is multi-homed or the client must
use a particular interface/address.

The normal pairing request also carries a binding for the exact TLS channel
that the client opened. The host compares it with its own channel before it
creates a pending request, so a relay cannot forward the client's own TLS
channel. Python exposes this binding for TLS 1.2, so only the short bootstrap
exchange is limited to TLS 1.2; regular authenticated requests may negotiate
TLS 1.3. Channel binding alone does not stop a relay that opens its *own*
connection to the real host; the certificate-bound code derivation above is
what closes that case. Once the host accepts the request, it returns a
short-lived high-entropy pairing session handle for approval polling instead
of repeatedly treating the eight-digit code as a credential.

The authenticated status response reports whether the host wake target is
available. Steam power actions are explicit and confirmed in Omarchy: Suspend,
Restart (reboot), and Shut down are dispatched through the fixed
`SteamClient.System` methods and remain “requested” until a later observation
can confirm what happened. The host status also reads a labelled CPU hwmon
sensor when available; Omarchy formats uptime as `d:h:m`.

The HTTPS listener uses a bounded worker pool with TLS-handshake and request
deadlines, and pairing state is pruned after expiry with a hard record bound.
Approval polling uses a high-entropy session handle and a separate poll limit;
new pairing attempts remain rate-limited. The Decky settings editor preserves
unsaved listener drafts while background status refreshes.

Sunshine is controlled only by Decky's **Monitor Sunshine** setting. The
frontend probes the installed Decky Sunshine owner through Decky Loader's
guarded cross-plugin method bridge and exposes only passive status plus the
owner's restart method. A normal paired client can see the cached status and
request a restart after the owner freshly confirms Sunshine stopped; there is
no separate Sunshine permission control in the pairing UI. Older credentials
may retain the legacy `sunshine.control` label, but it is no longer needed for
this action. If the owner bridge is unavailable, the UI says so and no restart
control is advertised.

## Omarchy client

The separate project documents its build, native plugin path, update/removal
flow, design adoption, and provider compatibility:

[Omarchy project README](/home/hvo/Projects/omarchy-steam-remote/README.md)

Install source:

`/home/hvo/Projects/omarchy-steam-remote/`

The Omarchy plugin is installed from its reviewed directory and does not
require a generated ZIP artifact.

## Contract and safety boundaries

Only enumerated routes and operations are accepted. Mutations carry a client
`request_id`; retries with the same body are idempotent, and changed bodies
conflict. Display preview intent is persisted before dispatch and the host
owns the 15-second restore watchdog. A Steam method return remains distinct
from visible-picture confirmation or a physical power transition.

Sunshine monitoring is off by default. Enabling it is a local Decky action;
the remote client cannot enable it. The provider seam accepts only passive
status and owner-mediated `ensure_running`; it never accepts a PID, path,
service name, shell command, or raw Steam payload.

## Validation boundary

Automated host/client/contract tests, manifest validation, QML panel linting,
JavaScript syntax checking, and the Decky package build pass in this workspace.
The actual SteamOS/Decky/Sunshine hardware journeys (blind no-signal recovery,
suspend/wake cycles, and provider recovery) still require the reference
SteamOS host and a second Omarchy machine; this workspace does not contain
that host runtime.
