# Bash API Help

DassieDrop exposes bash-friendly endpoints for automation, including workspace-aware sharing:

- `POST /api/share-text`
- `POST /api/share-file`
- `GET /api/workspaces`
- `POST /api/workspaces`

Share endpoints return a compact JSON payload with the generated short code, LAN share URL, and workspace metadata.

## Workspace Selection

Workspace-aware requests can target a workspace with any of these:

- `X-Workspace-ID: <workspace-id>`
- `X-Workspace-Name: <workspace-slug>`
- `?workspace=<workspace-id-or-slug>`
- `?workspace_name=<workspace-slug>`

Protected workspaces can also use:

- `X-Workspace-Password: <workspace-password>`
- `?workspace_password=<workspace-password>`

List workspaces:

```bash
curl -sS http://127.0.0.1:8000/api/workspaces
```

Create a workspace:

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  -X POST \
  -d '{"name":"Ops Desk","password":"vault"}' \
  http://127.0.0.1:8000/api/workspaces
```

Read state for a workspace by slug:

```bash
curl -sS \
  -H 'X-Workspace-Name: ops-desk' \
  http://127.0.0.1:8000/api/state
```

## Share Text

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  -X POST \
  -d '{
    "text": "hello from bash",
    "name": "CLI"
  }' \
  http://127.0.0.1:8000/api/share-text
```

Example response:

```json
{
  "type": "text",
  "id": "4b6a6d7c8e9f0123",
  "short_code": "ABCD",
  "share_path": "/s/ABCD",
  "share_url": "http://127.0.0.1:8000/s/ABCD",
  "hidden": false,
  "password_required": false,
  "created_at": 1714672800.0,
  "expires_at": 1714759200.0,
  "workspace_id": "default",
  "workspace_name": "Default",
  "workspace_slug": "default",
  "workspace_path": "/w/default",
  "workspace_url": "http://127.0.0.1:8000/w/default",
  "content": "hello from bash"
}
```

Hidden text example:

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  -X POST \
  -d '{
    "text": "secret note",
    "name": "CLI",
    "hidden": true,
    "password": "vault"
  }' \
  http://127.0.0.1:8000/api/share-text
```

Send text to a specific workspace:

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  -H 'X-Workspace-Name: ops-desk' \
  -X POST \
  -d '{
    "text": "hello from ops",
    "name": "CLI"
  }' \
  http://127.0.0.1:8000/api/share-text
```

## Share File

```bash
curl -sS \
  -X POST \
  -F 'file=@./example.txt' \
  -F 'name=CLI' \
  http://127.0.0.1:8000/api/share-file
```

Example response:

```json
{
  "type": "file",
  "id": "18f7d6c5b4a39281",
  "short_code": "WXYZ",
  "share_path": "/s/WXYZ",
  "share_url": "http://127.0.0.1:8000/s/WXYZ",
  "hidden": false,
  "password_required": false,
  "created_at": 1714672800.0,
  "expires_at": 1714759200.0,
  "workspace_id": "default",
  "workspace_name": "Default",
  "workspace_slug": "default",
  "workspace_path": "/w/default",
  "workspace_url": "http://127.0.0.1:8000/w/default",
  "name": "example.txt",
  "size": 42,
  "download_path": "/download/18f7d6c5b4a39281",
  "download_url": "http://127.0.0.1:8000/download/18f7d6c5b4a39281"
}
```

Hidden file example:

```bash
curl -sS \
  -X POST \
  -F 'file=@./secret.pdf' \
  -F 'name=CLI' \
  -F 'hidden=true' \
  -F 'password=vault' \
  http://127.0.0.1:8000/api/share-file
```

Upload a file into a specific workspace:

```bash
curl -sS \
  -H 'X-Workspace-Name: ops-desk' \
  -X POST \
  -F 'file=@./example.txt' \
  -F 'name=CLI' \
  http://127.0.0.1:8000/api/share-file
```

## Access Code

If DassieDrop is protected by an access code, the simplest bash option is to send it as `X-API-Key`:

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: your-access-code' \
  -X POST \
  -d '{"text":"hello again"}' \
  http://127.0.0.1:8000/api/share-text
```

You can do the same for file uploads:

```bash
curl -sS \
  -H 'X-API-Key: your-access-code' \
  -X POST \
  -F 'file=@./example.txt' \
  http://127.0.0.1:8000/api/share-file
```

And combine access code plus workspace targeting:

```bash
curl -sS \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: your-access-code' \
  -H 'X-Workspace-Name: ops-desk' \
  -X POST \
  -d '{"text":"hello again"}' \
  http://127.0.0.1:8000/api/share-text
```

If you still want browser-style session auth from bash, you can log in first and reuse the session cookie:

```bash
curl -sS -c cookies.txt \
  -H 'Content-Type: application/json' \
  -X POST \
  -d '{"code":"your-access-code"}' \
  http://127.0.0.1:8000/login
```

Then pass `-b cookies.txt` on later requests:

```bash
curl -sS -b cookies.txt \
  -H 'Content-Type: application/json' \
  -X POST \
  -d '{"text":"hello again"}' \
  http://127.0.0.1:8000/api/share-text
```
