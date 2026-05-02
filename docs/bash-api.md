# Bash API Help

LanDrop exposes two bash-friendly endpoints for automation:

- `POST /api/share-text`
- `POST /api/share-file`

Both endpoints return a compact JSON payload with the generated short code and LAN share URL.

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

## Access Code

If LanDrop is protected by an access code, the simplest bash option is to send it as `X-API-Key`:

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
