# DassieDrop

![dassiedrop_logo.png](brand/images/dassiedrop_logo.png)

## Your persistent local drop zone

No cloud. No accounts. No syncing.  
Just open a browser and drop.

DassieDrop is a lightweight local-first drop zone for your home network.  
It lets you move files and text between devices using nothing but a browser.

Unlike live-transfer tools, DassieDrop is persistent:
- drop files now
- pick them up later
- access them from any device on your LAN

It bridges the gap between:
- iPhone / iPad
- Android
- Linux
- Windows
- macOS

![dassiedrop_default.png](docs/dassiedrop-default.png)

---

## Why DassieDrop?

You have:
- a screenshot on your iPhone
- a PDF on your tablet
- logs on a server
- a command on your desktop

...and you need it somewhere else quickly.

Most people end up:
- emailing themselves files
- using Slack or WhatsApp as a clipboard
- uploading to cloud storage
- fighting with AirDrop limitations
- debugging unreliable WebRTC connections

DassieDrop solves this with a simple idea:

> A permanent local drop zone for your network.

Upload from one device.  
Download later from another.

No pairing. No accounts. No cloud dependency.

---

## Features

- **Persistent drop zone**  
  Upload now and retrieve later.

- **Universal compatibility**  
  If it has a browser, it works.

- **100% local-first**  
  Your data stays on your network.

- **File + text sharing**  
  Share documents, screenshots, URLs, notes, logs, and commands.

- **Simple and lightweight**  
  Built using the Python standard library with minimal dependencies.

- **Automation-friendly**  
  API-ready with `curl` support.

---

## How it compares

| Feature | DassieDrop | PairDrop / Snapdrop | Cloud Storage |
| :--- | :--- | :--- | :--- |
| **Workflow** | Persistent drop zone | Live device handoff | Sync + storage |
| **Connectivity** | Standard HTTP | WebRTC | Internet required |
| **Privacy** | Local-only | Uses signaling infrastructure | Third-party cloud |
| **Automation** | API / `curl` support | Browser-only | Vendor APIs |
| **Setup** | Lightweight Python runtime | Node.js + WebRTC stack | Account required |
| **Availability** | Drop now, collect later | Both devices must be active | Always online |

---

## Common workflows

### Mobile to Desktop

Send screenshots or photos from iPhone or Android directly to Linux, Windows, or macOS.

### Shared Clipboard

Drop:
- URLs
- terminal commands
- notes
- code snippets

...then open them instantly on another device.

### Server Logs

Pipe logs or output directly into DassieDrop using `curl`.

```bash
curl -H "X-API-Key: YOUR_API_KEY" \
  -F "file=@-;filename=server.log" \
  http://SERVER:8000/api/upload < server.log
```

### The Home Hub

Run DassieDrop on:
- a Raspberry Pi
- a NAS
- a mini PC
- a homelab server

...and always have a personal LAN inbox available.

---

## Quick Start

### Option 1 - Run directly with Python

Perfect for quick use or locked-down machines.

```bash
git clone https://github.com/vossie/DassieDrop.git
cd DassieDrop
python3 app.py
```

Access locally:

```text
http://localhost:8000
```

Or from another device on your network:

```text
http://YOUR-IP:8000
```

---

### Option 2 - Docker

Ideal for homelabs and always-on systems.

```bash
git clone https://github.com/vossie/DassieDrop.git
cd DassieDrop
docker build -t dassiedrop .
docker run -d \
  --name dassiedrop \
  -p 8000:8000 \
  -v dassiedrop-data:/data \
  dassiedrop
```

---

## Privacy by design

### Local-only

No external signaling servers.  
No STUN/TURN infrastructure.  
No cloud relay services.

### Lightweight and auditable

Small, clean codebase using the Python standard library.

### Auto-cleanup

Optional expiry policies for uploaded files and text.

### Secure

Optional access codes and HTTPS support.

---

## Why "Dassie"?

The dassie, or rock hyrax, is a small social mammal native to Southern Africa.

Like this project, it relies on simple shared spaces to stay connected efficiently.

---

## Roadmap ideas

- QR code device onboarding
- Drag-and-drop multi-upload
- Clipboard sync mode
- File previews
- Optional authentication
- Temporary share links
- Mobile-friendly PWA support

---

## Documentation

- [Installation Guide](docs/installation.md)
- [API Documentation](docs/api-usage.md)
- [Bash and curl API](docs/bash-api.md)
- [Developer Guide](docs/developer-guide.md)
- [License](LICENSE)

---

## Screenshots

### Login

![Screenshot-Login.png](docs/images/Screenshot-Login.png)

### Workspace selector

![Screenshot-Workspace-Selector.png](docs/images/Screenshot-Workspace-Selector.png)

### File drop zone

![Screenshot-File-Drop-Zone.png](docs/images/Screenshot-File-Drop-Zone.png)

### Text clipboard

![Screenshot-Text-Clipboard.png](docs/images/Screenshot-Text-Clipboard.png)

---

## Philosophy

DassieDrop is intentionally simple.

It is not:
- cloud storage
- device syncing
- a social platform
- a heavyweight collaboration tool

It is a fast, reliable, local-first drop zone that works everywhere a browser works.
