# 0010. One web UI, served by the engine and wrapped by Tauri

- Status: Accepted
- Date: 2026-11-17
- Deciders: technical lead

## Context
KEPServerEX ships a Windows configuration tool and a Quick Client. opencap needs a web UI
(`web-ui` PR in flight) and desktop apps for Linux and Windows.

## Decision
- The UI is a **static single-page application** that talks only to the public
  admin/config API (REST + WebSocket, [03 §3.6](../architecture/03-deep-dive.md#36-configuration-api-contract-for-the-web-ui-pr-and-both-engines)).
  No server-side rendering, no engine-internal calls. Built assets are served by the engine
  under `/ui/` and work from any origin the API allows.
- The **desktop app** (`desktop/`) is a Tauri 2 shell that loads the same bundle. The engine
  normally runs as a **system service** (systemd / Windows service), so polling continues when
  the app is closed (amended after RFC 0001 §5). The app can (a) start a bundled engine as a
  sidecar only for portable or demo use, (b) connect to one or more
  remote nodes (KEPServerEX remote configuration), (c) store connection profiles and tokens
  in the OS keychain.
- Linux: AppImage + deb; Windows: MSI (WebView2 is present on Windows 10/11; the installer
  bootstraps it otherwise).

## Consequences
The `web-ui` PR's choices are reviewed against "static bundle, API only". UI features are
built once.

## Alternatives considered
Native GUI (Qt, egui): two codebases with the web UI; Electron: much larger footprint.

## Revisit when
Users need offline-only features the browser sandbox cannot offer (they move into the Tauri
side as commands, still sharing the UI).
