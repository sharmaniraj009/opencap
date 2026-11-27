# Releases

How opencap is versioned, how a release is cut, and what is published where. The workflow is
[`.github/workflows/release.yml`](../.github/workflows/release.yml) (#34).

## Versioning

- **One source:** `version` under `[workspace.package]` in [`engine/Cargo.toml`](../engine/Cargo.toml).
  The engine binary (`opencap-engine --version`), the payload's `FirmVersion`, `/health`, the
  image and the installers all take it from there. The Rust engine is the only engine
  ([ADR-0018](adr/0018-rust-only-engine.md)): there is no wheel, sdist or PyPI package.
- **[SemVer](https://semver.org/).** `MAJOR.MINOR.PATCH`: MAJOR for an incompatible change to the
  config files, the MQTT payload or the admin API; MINOR for new drivers and features; PATCH for
  fixes. Before 1.0.0 a MINOR bump may still break things, and the release notes say so.
  Pre-releases use a SemVer suffix: `0.2.0-rc.1`; they are GitHub pre-releases and do not move `:latest`.
- **The tag is `v` + the version, exactly:** `v0.2.0`, `v0.2.0-rc.1`. The workflow refuses a
  tag that does not match.

## Cutting a release

1. Open a PR that sets the version in `engine/Cargo.toml` (and `Cargo.lock`) to the new one, and nothing else that needs review.
   Its CI is the last check before the tag. Merge it.
2. Tag the merge commit on `main` and push the tag:
   ```bash
   git switch main && git pull --ff-only
   git tag -a v0.2.0 -m "opencap 0.2.0"
   git push origin v0.2.0
   ```
3. The **Release** workflow runs: the Rust engine's checks (`rust-engine.yml`, the same jobs as on a
   PR), then builds and publishes. It takes about half an hour, mostly the arm64 image under emulation.
4. Check the release page and the image (below), then announce.

If a step fails after the image was pushed, fix the cause on `main` and cut a new PATCH
version. Do not move or re-push a published tag. To re-run publishing for the same tag (for
example after a GitHub outage), start the workflow by hand on the tag with *dry run* off.

## What is published where

| What | Where | Notes |
|---|---|---|
| Engine binaries | GitHub Release `vX.Y.Z` | `opencap-engine-X.Y.Z-linux-x86_64.tar.gz` and `opencap-engine-X.Y.Z-windows-x86_64.zip`: the binary, `LICENSE`, `NOTICE` and an example config |
| Container image | `ghcr.io/sharmaniraj009/opencap:X.Y.Z`, and `:latest` for a final (non-pre) release | one multi-arch tag: `linux/amd64` and `linux/arm64`, each smoke-tested before the push |
| Desktop installers | GitHub Release | `opencap-desktop_<v>_x64_en-US.msi`, `opencap-desktop_<v>_amd64.deb`, `opencap-desktop_<v>_amd64.AppImage`, with the Rust engine bundled ([desktop/README.md](../desktop/README.md), "Installers"); **unsigned** until the owner decides on code signing; `<v>` is the desktop app's own version (`desktop/src-tauri/tauri.conf.json`) |
| SBOMs (syft, SPDX JSON) | GitHub Release | `opencap-X.Y.Z-image-linux-amd64.spdx.json`, `...-arm64.spdx.json`: every OS package and the engine in each image |
| `SHA256SUMS` | GitHub Release | over every file attached to the release |
| Provenance attestations | GitHub attestations; the image's also in GHCR | for the engine binaries and the image digest (SLSA build provenance, signed with Sigstore) |
| Release notes | GitHub Release | generated from the merged PRs since the previous tag |

Only `GITHUB_TOKEN` is used. The workflow holds no other credential.

The [`Dockerfile`](../Dockerfile) builds the engine in `rust:1-slim-bookworm` and runs it on
`debian:bookworm-slim`. Neither tag is pinned by digest yet (`RUST_IMAGE`, `RUNTIME_IMAGE`): a PR that
pins them, with the release dry run as its check, is the next packaging step.

### Verifying a download

```bash
sha256sum -c SHA256SUMS
gh attestation verify opencap-engine-0.2.0-linux-x86_64.tar.gz --repo sharmaniraj009/opencap
gh attestation verify oci://ghcr.io/sharmaniraj009/opencap:0.2.0 --repo sharmaniraj009/opencap
```

## Dry runs

Every run that is not a release is a dry run: it builds the engine binaries and both images,
smoke-tests both images (arm64 under QEMU), writes the SBOMs and `SHA256SUMS`, and publishes
nothing (no push, no attestation, no release). The run's summary lists what a release would
attach. Dry runs happen:

- on every pull request that changes `release.yml`, the `Dockerfile` or `engine/Cargo.toml`
  (an arm64 build failure shows up there, not on release day);
- when the workflow is started by hand (Actions → Release → Run workflow), with *dry run* on,
  which is the default.

## Owner setup (one time)

These need the repository owner. Until they are done, the rest of the release still works.

1. **Image visibility.** GHCR creates the `opencap` package on the first push, linked to this
   repository. If it shows as private: package settings → *Change visibility* → Public.
