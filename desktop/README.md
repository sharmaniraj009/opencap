# opencap desktop

A Tauri 2 shell around the engine's web UI ([ADR-0010](../docs/adr/0010-one-ui-web-and-tauri.md), #27).
It keeps connection profiles for local and remote opencap engines, shows each engine's health,
and opens the engine's own web UI (`<engine>/ui/`) in a native window. Linux (AppImage, deb)
and Windows (MSI). Read-only towards PLCs, like the engine.

## What it does

- **Profiles:** a name and an engine address (default `http://127.0.0.1:8080`). Stored in
  `profiles.json` in the app's config directory (Linux `~/.config/org.opencap.desktop/`,
  Windows `%APPDATA%\org.opencap.desktop\`), mode 0600 on Unix.
- **Tokens:** kept in the system keychain (Secret Service / GNOME Keyring or KWallet on Linux,
  Credential Manager on Windows, Keychain on macOS) under the service `org.opencap.desktop`,
  never in a file.
- **Health:** the launcher asks each engine for `/health` (version, instance, devices
  ok/offline/demoted, MQTT link) and tests the stored token (`/api/v1/whoami`, or `/channels`
  on an engine without the configuration API). It says plainly when the engine is unreachable,
  needs a token, or refused the token. These requests come from Rust, so the engine needs no
  CORS settings.
- **Open:** shows `<engine>/ui/` in its own window. The token is written to that page's
  `sessionStorage` by an initialization script that checks the page's origin first, and the
  window cannot navigate to another origin.

## Tray, start at login, start-up behaviour

Everything here is the user's own switch, off on a new install, and lives in the launcher's
**Preferences** (`settings.json` next to `profiles.json`: switches and the id of the engine
opened last, never a secret):

- **Tray icon** (Linux status area, Windows notification area), always there when the desktop has
  one. Menu: *Show opencap*, *Open the engine opened last*, *Start* and *Stop the bundled engine*
  (only with an installer), *Quit* (which stops the bundled engine, like closing the app). A left
  click shows the launcher.
- **Keep running in the tray when the window is closed**: closing the launcher hides it instead of
  quitting. Honoured only while the tray icon exists, otherwise the user would have no way back to
  the window (the checkbox is disabled, with the reason, on a desktop without a status area).
- **Start opencap when I log in**: a `.desktop` file in `~/.config/autostart/` on Linux, a value
  under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` on Windows (per user, set with
  `reg.exe`), both removed again when the switch goes off. It starts `opencap-desktop --minimized`:
  the launcher window stays hidden when there is a tray icon to bring it back from. From an
  AppImage the entry points at the AppImage, not at its temporary mount.
- **Start the bundled engine when the app starts**, and **Open the engine opened last when the app
  starts** (skipped with `--minimized`: nobody is at the screen). The *Open <name>* button at the
  top of the launcher and the tray entry do the same on demand.

## Security model

- **No new Tauri capability.** The tray, the settings and the autostart entry are Rust-side: the
  tray is built and handled in Rust, and the launcher reaches the settings through three more of
  the app's own commands (`get_settings`, `set_settings`, `set_autostart`). `capabilities/default.json`
  is unchanged (`core:default`, launcher window only), and engine windows still get no IPC.
  The `tray-icon` and `image-png` features of `tauri` are compile-time features, not permissions.
- Capabilities (`src-tauri/capabilities/default.json`) cover the launcher window only, with
  `core:default` plus the app's own commands: no shell, no filesystem and no HTTP plugin.
- Engine windows load remote pages and get no Tauri IPC.
- The launcher page has a strict CSP (`script-src 'self'`) and builds every element with
  `textContent`.
- A token is bound to the address it was saved for (`token_origin` in `profiles.json`). Change
  a profile's address without typing a new token and the old token is deleted, and the launcher
  says so. A token saved for another address is never sent, even if the keychain could not
  delete it or `profiles.json` was edited by hand.
- A profile that sends its token over `http://` to another computer is flagged in the launcher:
  put TLS (https) in front of the engine.
- `tauri` and `tauri-build` are pinned to the reviewed minor versions, and CI builds with
  `--locked`. The "engine windows get no IPC" guarantee depends on Tauri's remote-origin checks.
- Windows: the MSI carries Microsoft's WebView2 bootstrapper (`embedBootstrapper`), so it
  installs without internet access when WebView2 is present, as on Windows 10 and 11. A PC
  with no WebView2 at all still needs Microsoft's offline WebView2 installer first.
- The bundled engine (below) listens on 127.0.0.1 only and always requires its token.

## Develop

Rust 1.85+. On Linux the WebKitGTK stack:

```bash
sudo apt-get install -y libwebkit2gtk-4.1-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev libdbus-1-dev build-essential pkg-config file
```

```bash
cd desktop
cargo test                      # profile storage (in-memory keychain), settings, autostart entries, engine checks against a fake server
cargo run                       # the launcher window (needs a display)
cargo install tauri-cli --version "^2" --locked
cargo tauri build               # bundles: AppImage/deb on Linux, MSI on Windows
```

`cargo tauri build` alone bundles the app without an engine. The installers CI builds bundle
the engine as well; see [Installers](#installers-with-the-engine-bundled).

## Installers, with the engine bundled

Single-box install (ADR-0010 (a), #58): one installer puts the app and the Rust engine
(`opencap-engine`) side by side. [`desktop.yml`](../.github/workflows/desktop.yml) builds them
on every PR that touches `desktop/` or `engine/`, and the release workflow attaches them to each
release ([docs/release.md](../docs/release.md)).

| Installer | Installs | Notes |
|---|---|---|
| `opencap-desktop_<v>_x64_en-US.msi` | `C:\Program Files\opencap-desktop\` (app + `opencap-engine.exe`), Start menu entry | per machine; WebView2 bootstrapper embedded, no internet needed when WebView2 is present |
| `opencap-desktop_<v>_amd64.deb` | `/usr/bin/opencap-desktop`, `/usr/bin/opencap-engine`, menu entry | depends on `libwebkit2gtk-4.1-0`, `libgtk-3-0`; named so it cannot clash with an engine package |
| `opencap-desktop_<v>_amd64.AppImage` | nothing: runs in place | same two binaries inside |

The launcher shows **Engine bundled with this app** with *Start* and *Stop*:

- **Config and outbox** are in the user's local app data, not in the install directory:
  Linux `~/.local/share/org.opencap.desktop/engine/`, Windows
  `%LOCALAPPDATA%\org.opencap.desktop\engine\` (`config/`, `data/saf.sqlite`, `logs/engine.log`).
  The first start creates them; nothing overwrites them afterwards. Upgrading, reinstalling or
  uninstalling the app leaves them in place (CI checks the uninstall case on both platforms).
- **Port:** 127.0.0.1 and a free port, chosen on the first start and kept while it is free.
  If another program took it, the next start moves to a free port and says so. The app manages
  only the `poller.yaml` line marked `# port managed by opencap desktop`; edit the rest freely
  (devices go in `devices.json`, as for any engine; `opencap import-kep` writes them).
- **Token:** every start generates a new 256-bit token and passes it to the engine in
  `OPENCAP_API_TOKEN`. The launcher's "Engine on this computer" profile keeps it in the system
  keychain, bound to the engine's address, and *Open* signs the web UI in with it. Without a
  keychain the token stays in memory until the app closes. It is never written to a file.
- **Stop** asks the engine to shut down cleanly (SIGTERM, then a kill after 10 s) on Linux. On
  Windows it terminates the process, because a windowless child has no console to receive
  Ctrl+C; the outbox is SQLite, so queued messages are kept. Closing the app stops the engine.
- **Install as a service:** not yet. Once the engine can install itself as a service (#57),
  the launcher will hand over to that instead of running the engine as its own child.
- `opencap-desktop --check-bundled-engine [report-file]` starts the engine next to the app on a
  throwaway config, checks `/health` with its token, stops it and exits 0 or 1: the installers'
  smoke test, also handy on a target PC.

**Unsigned.** The MSI, deb and AppImage are not code-signed: signing needs a certificate and is
an owner decision (board #1, "Decisions for the owner"). Windows SmartScreen therefore warns on
first run ("More info" → "Run anyway"). Check downloads against the release's `SHA256SUMS` and
provenance attestation instead ([docs/release.md](../docs/release.md), "Verifying a download").

Build the installers locally like CI does:

```bash
(cd ../engine && cargo build --locked --release -p opencap-engine)
mkdir -p src-tauri/binaries
cp ../engine/target/release/opencap-engine "src-tauri/binaries/opencap-engine-$(rustc -vV | sed -n 's/host: //p')"
cargo tauri build --config src-tauri/tauri.engine.conf.json
```

`src-tauri/tauri.engine.conf.json` adds the engine as Tauri's `externalBin`. It is a separate
file so that `cargo build`, `cargo test` and `cargo tauri dev` work without an engine binary.
The MSI's upgrade code is Tauri's default, derived from the product name: keep `productName`
as it is, or set `bundle.windows.wix.upgradeCode` to the old value before renaming, so that a
new MSI replaces the old one instead of installing beside it.

Point a profile at a running engine (`opencap run`), or start one for the UI:
`OPENCAP_TOKEN_ALICE=… opencap run` with `api.tokens` in `poller.yaml`.

## Licences (ADR-0007)

Direct dependencies: `tauri` 2 (with its `tray-icon` and `image-png` features) and `tauri-build` 2 (Apache-2.0 OR MIT), `keyring` 3, `ureq` 2,
`url` 2, `serde`, `serde_json`, `thiserror`, `getrandom` 0.3 (the bundled engine's token), on
Unix `libc` (its clean stop) and, for tests only, `tempfile` (all MIT OR Apache-2.0). The tray icon's PNG decoding adds four crates: `image` (MIT OR Apache-2.0), `byteorder-lite` (Unlicense OR MIT), `moxcms` and `pxfm` (BSD-3-Clause OR Apache-2.0). The whole tree (`cargo metadata`, all targets) is MIT, Apache-2.0, BSD, ISC, Zlib,
Unicode-3.0 or Unlicense, except:

- **MPL-2.0**, unmodified, pulled in by Tauri's HTML handling: `cssparser`, `cssparser-macros`,
  `selectors`, `dtoa-short`, `option-ext`. MPL-2.0 is file-level copyleft: fine to ship
  unmodified, and changes to those files would have to be published. `desktop/deny.toml`
  allows MPL-2.0 for these five crates only.
- **CDLA-Permissive-2.0**: `webpki-roots` (Mozilla's root certificates, via `ureq`).
- `r-efi` is MIT OR Apache-2.0 OR LGPL-2.1-or-later; MIT applies.

CI enforces this with cargo-deny (`desktop/deny.toml`; job "Desktop licence and advisory gate"
in `.github/workflows/licenses.yml`) on pull requests that touch `desktop/` and nightly.

## Not yet

Signing (owner decision), auto-update, macOS bundles, the "install as service" hand-over (#57).
