# Developer Guide

## Overview

DassieDrop is a small Python application with:

- `app.py` for the HTTP server and API
- `templates/` for HTML
- `assets/` for browser JS and CSS
- `docs/` for operator and developer documentation
- `test_app.py` for the test suite

The project is intentionally simple and dependency-light. Keep new features consistent with that unless there is a strong reason to add complexity.

## Local Development

Run the app locally:

```bash
./.venv/bin/python app.py
```

Run tests:

```bash
./.venv/bin/python -m unittest -v test_app.py
```

## Versioning

The current build version is stored in the repo-root `VERSION` file.

Rules:

- Update `VERSION` when preparing a user-visible release change.
- The UI header reads from `VERSION` unless `APP_VERSION` is provided in the environment.
- The Ubuntu install script deploys the `VERSION` file and also supports `APP_VERSION` override in the service environment.

## Main Branch Release Rule

Versions roll up when committing to `main`.

That means:

- if you are committing a release-worthy change to `main`, bump `VERSION` in the same change
- do not leave release changes on `main` without updating the version
- keep version bumps intentional and monotonic

Example workflow:

1. Make the code or docs change.
2. Update `VERSION`.
3. Run `./.venv/bin/python -m unittest -v test_app.py`.
4. Commit the change that includes the version bump.

Record the user-visible change in `CHANGELOG.md` in the same release change.

## Releasing the Windows Portable Build

The Windows build is produced by the GitHub Actions workflow in `.github/workflows/build-windows.yml`. It triggers automatically when a version tag is pushed — there is no separate manual step.

To publish a new Windows release:

1. Update `VERSION` and `CHANGELOG.md` in the same commit (following the main branch release rule above).
2. Commit and push to `master`.
3. Push a version tag matching the new `VERSION` value:

```bash
git tag v$(cat VERSION)
git push origin v$(cat VERSION)
```

The workflow will:

- Build `dassiedrop.exe` on a Windows runner using PyInstaller
- Run the functional test suite against the binary
- Upload a zip artifact (`dassiedrop-windows-<version>`) to the Actions run for inspection
- Attach `dassiedrop.exe` and `dassiedrop.env.example` as downloadable assets to a GitHub Release named `DassieDrop <version>`

The release and its assets are visible at `https://github.com/vossie/DassieDrop/releases`.

## Documentation

When you add or change public behavior, update the relevant docs:

- `README.md` for product-facing usage and positioning
- `CHANGELOG.md` for release notes and user-visible changes
- `docs/bash-api.md` for shell and `curl` automation flows
- `docs/developer-guide.md` for contributor and release workflow notes
