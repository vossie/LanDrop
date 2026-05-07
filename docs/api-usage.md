# DassieDrop API Usage

DassieDrop exposes a simple HTTP API for automation, shell scripts, and local tooling.

Base URL examples:

- `http://127.0.0.1:8000`
- `http://YOUR-IP:8000`

For richer bash-oriented examples, see [bash-api.md](bash-api.md). For the full schema, see [openapi.yaml](openapi.yaml).

## Get Workspace State

Read the current workspace snapshot:

```bash
curl http://127.0.0.1:8000/api/state
```

Target a workspace by slug:

```bash
curl -H 'X-Workspace-Name: default' \
  http://127.0.0.1:8000/api/state
```

## Share Text

Create a text entry and get a compact share payload back:

```bash
curl -X POST \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello world","name":"CLI"}' \
  http://127.0.0.1:8000/api/share-text
```

If you want the full updated workspace snapshot instead, use:

```bash
curl -X POST \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello world","name":"CLI"}' \
  http://127.0.0.1:8000/api/text
```

## Share a File

Upload a file and get a compact share payload back:

```bash
curl -F "file=@example.txt" \
  -F "name=CLI" \
  http://127.0.0.1:8000/api/share-file
```

If you want the full updated workspace snapshot instead, use:

```bash
curl -F "file=@example.txt" \
  -F "name=CLI" \
  http://127.0.0.1:8000/api/upload
```

Upload from stdin with an explicit filename:

```bash
curl -F "file=@-;filename=server.log" \
  http://127.0.0.1:8000/api/upload < server.log
```

If DassieDrop is protected, include `X-API-Key`:

```bash
curl -H 'X-API-Key: your-api-key-or-access-code' \
  -F "file=@-;filename=server.log" \
  http://127.0.0.1:8000/api/upload < server.log
```

## Delete an Item

Delete a text entry:

```bash
curl -X DELETE http://127.0.0.1:8000/api/text/<text-id>
```

Delete a file entry:

```bash
curl -X DELETE http://127.0.0.1:8000/api/file/<file-id>
```

## Access Control

If DassieDrop is protected, send `X-API-Key` for automation:

```bash
curl -X POST \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: your-api-key-or-access-code' \
  -d '{"text":"Hello world"}' \
  http://127.0.0.1:8000/api/share-text
```

## Notes

- The API is intended for local-network use.
- Use `X-Workspace-Name`, `X-Workspace-ID`, or the workspace query parameters when targeting a non-default workspace.
- If you expose DassieDrop externally, put it behind proper TLS and access controls.
