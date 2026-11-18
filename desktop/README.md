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

## Security model

- Capabilities (`src-tauri/capabilities/default.json`) cover the launcher window only, with
  `core:default` plus the app's own commands: no shell, no filesystem and no HTTP plugin.
- Engine windows load remote pages and get no Tauri IPC.
- The launcher page has a strict CSP (`script-src 'self'`) and builds every element with
  `textContent`.

## Develop

Rust 1.85+. On Linux the WebKitGTK stack:

```bash
sudo apt-get install -y libwebkit2gtk-4.1-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev libdbus-1-dev build-essential pkg-config file
```

```bash
cd desktop
cargo test                      # profile storage (in-memory keychain), engine checks against a fake server
cargo run                       # the launcher window (needs a display)
cargo install tauri-cli --version "^2" --locked
cargo tauri build               # bundles: AppImage/deb on Linux, MSI on Windows
```

Point a profile at a running engine (`opencap run`), or start one for the UI:
`OPENCAP_TOKEN_ALICE=… opencap run` with `api.tokens` in `poller.yaml`.

## Not yet

Starting a bundled engine as a sidecar for single-box installs, signing, auto-update, macOS
bundles.
