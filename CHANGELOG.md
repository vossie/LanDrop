# Changelog

## 1.0.39 - 2026-05-07

- Tightened workspace identifiers to the canonical `a-z`, `0-9`, `-`, `_`, and `.` character set.
- Changed workspace entry to resolve `POST /api/workspaces/{workspace}/enter` by selector string instead of a raw workspace id.

## 1.0.38 - 2026-05-07

- Simplified the public workspace selector contract to one opaque, case-sensitive string via `X-Workspace` or `workspace`.
- The OpenAPI schema and Markdown docs now treat `X-Workspace-ID`, `X-Workspace-Slug`, `X-Workspace-Name`, `workspace_slug`, and `workspace_name` as compatibility aliases instead of first-class API inputs.

## 1.0.37 - 2026-05-07

- Breaking API change: share payloads now use `workspace_display_name` instead of `workspace_name` to make the display-name field explicit.
- API selector terminology now prefers `X-Workspace-Slug` and `workspace_slug`, while the older `X-Workspace-Name` and `workspace_name` request aliases remain supported for compatibility.
- OpenAPI and Markdown docs were updated to reflect the current API behavior and authenticated upload examples.
