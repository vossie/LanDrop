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
