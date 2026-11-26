# Releases

How opencap is versioned, how a release is cut, and what is published where. The workflow is
[`.github/workflows/release.yml`](../.github/workflows/release.yml) (#34).

## Versioning

- **One source:** `__version__` in [`src/opencap/__init__.py`](../src/opencap/__init__.py).
  `pyproject.toml` reads it (`dynamic = ["version"]`), and the API's `/health` reports it, so
  the wheel, the sdist, the image and the running engine always agree.
- **[SemVer](https://semver.org/) meaning, [PEP 440](https://peps.python.org/pep-0440/) spelling.**
  `MAJOR.MINOR.PATCH`: MAJOR for an incompatible change to the config files, the MQTT payload
  or the admin API; MINOR for new drivers and features; PATCH for fixes. Before 1.0.0 a MINOR
  bump may still break things, and the release notes say so. Pre-releases use PEP 440 suffixes,
  which pip understands: `0.2.0rc1`, `0.2.0b1`, `0.2.0.dev1`.
- **The tag is `v` + `__version__`, exactly:** `v0.2.0`, `v0.2.0rc1`. The workflow refuses a
  tag that does not match.

## Cutting a release

1. Open a PR that sets `__version__` to the new version (and nothing else that needs review).
   Its CI is the last check before the tag. Merge it.
2. Tag the merge commit on `main` and push the tag:
   ```bash
   git switch main && git pull --ff-only
   git tag -a v0.2.0 -m "opencap 0.2.0"
   git push origin v0.2.0
   ```
3. The **Release** workflow runs: the CI checks (the same jobs as on a PR), then builds and
   publishes. It takes about half an hour, mostly the arm64 image under emulation.
4. Check the release page and the image (below), then announce.

If a step fails after the image was pushed, fix the cause on `main` and cut a new PATCH
version. Do not move or re-push a published tag. To re-run publishing for the same tag (for
example after a GitHub outage), start the workflow by hand on the tag with *dry run* off.

## What is published where

| What | Where | Notes |
|---|---|---|
| Wheel and sdist | GitHub Release `vX.Y.Z` | `opencap-X.Y.Z-py3-none-any.whl`, `opencap-X.Y.Z.tar.gz` |
| Container image | `ghcr.io/sharmaniraj009/opencap:X.Y.Z`, and `:latest` for a final (non-pre) release | one multi-arch tag: `linux/amd64` and `linux/arm64`, each smoke-tested before the push |
| Desktop installers | GitHub Release | `opencap-desktop_<v>_x64_en-US.msi`, `opencap-desktop_<v>_amd64.deb`, `opencap-desktop_<v>_amd64.AppImage`, with the Rust engine bundled ([desktop/README.md](../desktop/README.md), "Installers"); **unsigned** until the owner decides on code signing; `<v>` is the desktop app's own version (`desktop/src-tauri/tauri.conf.json`) |
| SBOMs (syft, SPDX JSON) | GitHub Release | `opencap-X.Y.Z-image-linux-amd64.spdx.json`, `...-arm64.spdx.json`: every OS and Python package in each image |
| `SHA256SUMS` | GitHub Release | over every file attached to the release |
| Provenance attestations | GitHub attestations; the image's also in GHCR | for the wheel, the sdist and the image digest (SLSA build provenance, signed with Sigstore) |
| Release notes | GitHub Release | generated from the merged PRs since the previous tag |
| PyPI | not yet: gated, see below | |

Only `GITHUB_TOKEN` is used. The workflow holds no other credential.

Base images are pinned by digest in the [`Dockerfile`](../Dockerfile) (`PYTHON_IMAGE`), so a
release rebuilds from the same base the PR tested. Moving the pin is a PR of its own.

### Verifying a download

```bash
sha256sum -c SHA256SUMS
gh attestation verify opencap-0.2.0-py3-none-any.whl --repo sharmaniraj009/opencap
gh attestation verify oci://ghcr.io/sharmaniraj009/opencap:0.2.0 --repo sharmaniraj009/opencap
```

## Dry runs

Every run that is not a release is a dry run: it builds the wheel, the sdist and both images,
smoke-tests both images (arm64 under QEMU), writes the SBOMs and `SHA256SUMS`, and publishes
nothing (no push, no attestation, no release). The run's summary lists what a release would
attach. Dry runs happen:

- on every pull request that changes `release.yml`, the `Dockerfile` or `pyproject.toml`
  (a dependency without arm64 wheels shows up there, not on release day);
- when the workflow is started by hand (Actions → Release → Run workflow), with *dry run* on,
  which is the default.

## Owner setup (one time)

These need the repository owner. Until they are done, the rest of the release still works.

1. **Image visibility.** GHCR creates the `opencap` package on the first push, linked to this
   repository. If it shows as private: package settings → *Change visibility* → Public.
2. **PyPI** (optional; the `pypi` job is skipped until step 4):
   1. On pypi.org, create the `opencap` project, or reserve it with a
      [pending trusted publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
   2. Add a trusted publisher: owner `sharmaniraj009`, repository `opencap`, workflow
      `release.yml`, environment `pypi`.
   3. In this repository: Settings → Environments → New environment `pypi` (optionally with
      required reviewers, so each upload waits for a click).
   4. Settings → Secrets and variables → Actions → Variables: `PYPI_PUBLISH` = `true`.

   PyPI trusted publishing uses a short-lived OIDC token per run: no API token is stored
   anywhere. The next release then also uploads the same wheel and sdist to PyPI.
