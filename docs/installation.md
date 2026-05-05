# DassieDrop Installation

Use this guide for local startup, Docker, and server installs.

## Quick Start

```bash
./.venv/bin/python app.py
```

Then open:

```text
http://127.0.0.1:8000
```

From other devices on the same network:

```text
http://<this-machine-ip>:8000
```

## Protect The App With An Access Code

```bash
ACCESS_CODE=my-secret-code ./.venv/bin/python app.py
```

## Run With HTTPS

DassieDrop can generate a local self-signed certificate automatically when you enable HTTPS.
With HTTPS enabled, DassieDrop keeps plain HTTP on port `8000` and adds HTTPS on port `8443` by default.

```bash
HTTPS=1 ./.venv/bin/python app.py
```

The first start creates:

- `certs/dassiedrop-selfsigned.crt`
- `certs/dassiedrop-selfsigned.key`

Then open:

```text
http://localhost:8000
https://localhost:8443
```

Notes:

- Browser clipboard read works much more reliably over `https://` than plain LAN `http://`.
- Browsers will warn that the certificate is self-signed until you explicitly trust it.
- `localhost` is the easiest hostname for clipboard support. A raw LAN IP may still work for HTTPS, but certificate trust and browser behavior are stricter there.
- Override ports with `HTTP_PORT` and `HTTPS_PORT` if you do not want `8000` and `8443`.

Optional overrides:

```bash
HTTPS=1 \
HTTP_PORT=8000 \
HTTPS_PORT=8443 \
HTTPS_CERT_FILE=/path/to/dassiedrop.crt \
HTTPS_KEY_FILE=/path/to/dassiedrop.key \
HTTPS_SELF_SIGNED_HOST=localhost \
HTTPS_SELF_SIGNED_SANS=DNS:localhost,IP:127.0.0.1 \
./.venv/bin/python app.py
```

## Use Your Own SSL Certificate

If you already have a certificate and private key, point DassieDrop at those files instead of using the generated self-signed pair:

```bash
HTTPS=1 \
HTTP_PORT=8000 \
HTTPS_PORT=8443 \
HTTPS_CERT_FILE=/etc/ssl/certs/dassiedrop.crt \
HTTPS_KEY_FILE=/etc/ssl/private/dassiedrop.key \
./.venv/bin/python app.py
```

Use a certificate whose hostname or IP matches the address you open in the browser. For example, if you browse to `https://files.example.lan:8443`, that hostname must be covered by the certificate.

## Run With Docker

DassieDrop ships with:

- a `Dockerfile` for local image builds
- a `docker-compose.yml` for a persistent container setup
- a writable `/data/uploads` path for uploaded files

```bash
docker build -t dassiedrop .
```

```bash
docker run -d \
  --name dassiedrop \
  -p 8000:8000 \
  -e ACCESS_CODE=my-secret-code \
  -e SHARE_BASE_URL=http://192.168.1.24:8000 \
  -v dassiedrop-data:/data \
  dassiedrop
```

Open `http://127.0.0.1:8000`.

The container stores uploads in `/data/uploads`.

Run with Compose:

```bash
ACCESS_CODE=my-secret-code SHARE_BASE_URL=http://192.168.1.24:8000 docker compose up -d
```

The included [docker-compose.yml](/home/carel/IdeaProjects/bronzegate/DassieDrop/docker-compose.yml) maps port `8000`, keeps uploads in a named volume, and restarts automatically.

## Configure The LAN Link Address

By default, DassieDrop uses the browser's current origin for share links. To force a fixed LAN address, set `SHARE_BASE_URL`.

```bash
SHARE_BASE_URL=http://192.168.1.24:8000 ./.venv/bin/python app.py
```

Use this when:

- all devices should see the same LAN address
- DassieDrop is behind a reverse proxy
- you do not want links generated from `127.0.0.1`

## Test

```bash
./.venv/bin/python -m unittest -v test_app.py
```

## Install As An Ubuntu Service

Run on the target Ubuntu server as `root`:

```bash
sudo bash ./install-ubuntu-service.sh
```

Quick install:

```bash
curl -fsSLo github-ubuntu-install-upgrade.sh https://raw.githubusercontent.com/vossie/DassieDrop/master/github-ubuntu-install-upgrade.sh
chmod +x github-ubuntu-install-upgrade.sh
sudo ./github-ubuntu-install-upgrade.sh
```

Or install or upgrade directly from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/vossie/DassieDrop/master/github-ubuntu-install-upgrade.sh | sudo bash
```

It will:

- upgrade the runtime to `python3.11`
- create a system user and group named `dassiedrop`
- install the app into `/opt/dassiedrop`
- store uploads in `/var/lib/dassiedrop/uploads`
- write config to `/etc/dassiedrop/dassiedrop.env`
- create and enable a `systemd` service

Override defaults:

```bash
sudo ACCESS_CODE=my-secret-code PORT=8080 bash ./install-ubuntu-service.sh
```

Or use `--port`:

```bash
sudo bash ./install-ubuntu-service.sh --port 8080
```

Set the share link base address during install:

```bash
sudo SHARE_BASE_URL=http://192.168.1.24:8000 bash ./install-ubuntu-service.sh
```

The GitHub helper also supports overrides. On upgrade it reuses values from `/etc/dassiedrop/dassiedrop.env` unless you override them:

```bash
curl -fsSL https://raw.githubusercontent.com/vossie/DassieDrop/master/github-ubuntu-install-upgrade.sh | sudo ACCESS_CODE=my-secret-code PORT=8080 bash
```

Use the Ubuntu service install for a native `systemd` deployment. Use Docker for a portable container runtime.

## Install On CentOS Stream From GitHub

Install or upgrade on a CentOS Stream host:

```bash
curl -fsSL https://raw.githubusercontent.com/vossie/DassieDrop/master/github-centos-stream-install-upgrade.sh | sudo bash
```

The CentOS Stream helper installs required packages with `dnf`, upgrades to `python3.11`, creates the same `dassiedrop` system user and `systemd` service, and reuses values from `/etc/dassiedrop/dassiedrop.env` on upgrade unless you override them.

Override defaults:

```bash
curl -fsSL https://raw.githubusercontent.com/vossie/DassieDrop/master/github-centos-stream-install-upgrade.sh | sudo ACCESS_CODE=my-secret-code PORT=8080 bash
```
