# DassieDrop API Usage

DassieDrop includes a simple HTTP API for automation, shell scripts, and local tooling.

Base URL examples:

- `http://127.0.0.1:8000`
- `http://YOUR-IP:8000`

## Share Text

```bash
curl -X POST http://127.0.0.1:8000/upload_text \
     -d "text=Hello world"
```

## Share a File

```bash
curl -F "file=@example.txt" http://127.0.0.1:8000/upload_file
```

## List Items

```bash
curl http://127.0.0.1:8000/list
```

## Delete an Item

```bash
curl -X DELETE http://127.0.0.1:8000/delete/<id>
```

## Notes

- The API is intended for local-network use.
- If you expose DassieDrop externally, put it behind proper TLS and access controls.
- For broader endpoint details, see [openapi.yaml](openapi.yaml).
