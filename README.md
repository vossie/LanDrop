# DassieDrop
![dassiedrop_logo.png](brand/images/dassiedrop_logo.png)
**Move files and text between your devices, instantly, privately, and without the cloud.**

DassieDrop is a lightweight, self-hosted LAN drop zone that lets you share files and clipboard-style text between:

- iPhone / iPad
- Android
- Linux
- Windows
- macOS

No accounts. No syncing. No third-party services. Just open a browser and drop.

---

## 🚀 Why DassieDrop?

If you’ve ever:

- emailed yourself files just to move them
- used WhatsApp/Slack as a “clipboard”
- struggled getting files from iPhone → Linux
- avoided cloud tools for privacy/work reasons

This is for you.

---

## 🔥 What makes it different

- 🌐 Works on anything with a browser
- 🔒 No cloud / no external services
- ⚡ Instant sharing over your local network
- 📄 Supports both files and text snippets
- 🧠 Dead simple, no setup, no accounts
- 🧰 Scriptable via curl API

---

## ⚖️ Why not just use something else?

| Tool | Limitation |
|------|-----------|
| AirDrop | Apple-only |
| Dropbox / Drive / iCloud | Sends your data through the cloud |
| Email / WhatsApp / Slack | Awkward for quick transfers |
| USB sticks | Manual and slow |
| SCP / rsync | Not usable from phones |
| DassieDrop | Works everywhere via browser on your LAN |

---

## 🧪 Try it in 30 seconds

```bash
git clone https://github.com/vossie/DassieDrop.git
cd DassieDrop
python3 app.py
```

Open in your browser:

http://127.0.0.1:8000

From another device on the same network:

http://YOUR-IP:8000

---

## 🔐 Privacy & Security

DassieDrop is local-first by design:

- No internet required
- No third-party servers
- Files stay on your machine
- Auto-expiry (24h cleanup)
- Optional access code
- Optional hidden/password-protected items

⚠️ Not intended to be exposed directly to the public internet without proper setup (reverse proxy + TLS).

---

## 🧰 API (for automation)

Upload text:

```bash
curl -X POST http://127.0.0.1:8000/upload_text \
     -d "text=Hello world"
```

Upload a file:

```bash
curl -F "file=@example.txt" http://127.0.0.1:8000/upload_file
```

List items:

```bash
curl http://127.0.0.1:8000/list
```

Delete item:

```bash
curl -X DELETE http://127.0.0.1:8000/delete/<id>
```

---

## 🧠 Use cases

- Move screenshots from iPhone → Linux instantly
- Send logs from a server → your phone
- Share config snippets between work and home machines
- Quick “clipboard sync” across devices
- Replace email/Slack as a file bridge

---

## ⚙️ Requirements

- Python 3.x
- No external dependencies

---

## 🛠️ Installation

See full instructions in docs/installation.md

---

## 🤝 Contributing

PRs welcome. Keep it simple, local-first, and dependency-free.

---

## 🦡 Why “Dassie”?

Because it’s small, local, and surprisingly effective, like the rock hyrax.
