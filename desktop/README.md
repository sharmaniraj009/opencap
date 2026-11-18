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

The Linux bundle is `opencap-desktop_<version>_amd64.deb` (about 2 MB). It installs
`/usr/bin/opencap-desktop`, icons and a menu entry, and depends on `libwebkit2gtk-4.1-0` and
`libgtk-3-0`. The package is named `opencap-desktop` so it cannot clash with an engine package.

Point a profile at a running engine (`opencap run`), or start one for the UI:
`OPENCAP_TOKEN_ALICE=… opencap run` with `api.tokens` in `poller.yaml`.

## Licences (ADR-0007)

Direct dependencies: `tauri` 2 and `tauri-build` 2 (Apache-2.0 OR MIT), `keyring` 3, `ureq` 2,
`url` 2, `serde`, `serde_json`, `thiserror` and, for tests only, `tempfile` (all MIT OR
Apache-2.0). The whole tree (`cargo metadata`, all targets) is MIT, Apache-2.0, BSD, ISC, Zlib,
Unicode-3.0 or Unlicense, except:

- **MPL-2.0**, unmodified, pulled in by Tauri's HTML handling: `cssparser`, `cssparser-macros`,
  `selectors`, `dtoa-short`, `option-ext`. MPL-2.0 is file-level copyleft: fine to ship
  unmodified, and changes to those files would have to be published. `engine/deny.toml` does not
  allow MPL-2.0 yet; the desktop workspace has no `cargo deny` gate of its own yet.
- **CDLA-Permissive-2.0**: `webpki-roots` (Mozilla's root certificates, via `ureq`).
- `r-efi` is MIT OR Apache-2.0 OR LGPL-2.1-or-later; MIT applies.

## Not yet

Starting a bundled engine as a sidecar for single-box installs, signing, auto-update, macOS
bundles.
