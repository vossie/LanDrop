# Developer Guide

## Overview

LanDrop is a small Python application with:

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

## Documentation

When you add or change public behavior, update the relevant docs:

- `README.md` for product-facing usage and positioning
- `docs/bash-api.md` for shell and `curl` automation flows
- `docs/developer-guide.md` for contributor and release workflow notes
