# LanDrop

Small Python web app for sharing pasted text and uploaded files between browsers on the same network.

## Run

```bash
./.venv/bin/python app.py
```

## Test

```bash
./.venv/bin/python -m unittest -v test_app.py
```

To require a shared access code:

```bash
ACCESS_CODE=my-secret-code ./.venv/bin/python app.py
```

The app listens on `0.0.0.0:8000` by default, so other machines on the same network can open:

```text
http://<this-machine-ip>:8000
```

## Install As Ubuntu Service

Run the installer as `root` on the target Ubuntu server:

```bash
sudo bash ./install-ubuntu-service.sh
```

It will:

- create a system user and group named `landrop`
- install the app into `/opt/landrop`
- store uploads in `/var/lib/landrop/uploads`
- write config to `/etc/landrop/landrop.env`
- create and enable a `systemd` service that starts on boot

You can override defaults when installing:

```bash
sudo ACCESS_CODE=my-secret-code PORT=8080 bash ./install-ubuntu-service.sh
```

Useful service commands:

```bash
sudo systemctl status landrop
sudo systemctl restart landrop
sudo journalctl -u landrop -f
```

To uninstall the service but keep uploaded data and the service user:

```bash
sudo bash ./uninstall-ubuntu-service.sh
```

To also remove persisted uploads and the service account:

```bash
sudo REMOVE_DATA=1 REMOVE_USER=1 bash ./uninstall-ubuntu-service.sh
```

## What it does

- Text history with copy and delete actions
- File history with drag-and-drop upload, download, and delete actions
- Automatic expiry for text and files after 24 hours
- Optional access code gate for all browsers
- No external dependencies

## API

- `GET /api/state`: full current history snapshot
- `GET /api/latest-text`: newest text entry as JSON
- `GET /api/latest-file`: newest file entry metadata as JSON
- `GET /api/latest-file/content`: newest file content as a file download

## Notes

- Text history is kept in memory while the app is running.
- Uploaded files are stored in `uploads/`.
- Browsers poll every 2 seconds for updates.
- Maximum upload size is 1 GB.
- Expired files are removed automatically when the app is used.
