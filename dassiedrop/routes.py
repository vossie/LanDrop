import html
import hmac
import json
import shutil
import ssl
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth, config, state, storage, websocket


def get_share_base_url() -> str:
    return config.SHARE_BASE_URL.rstrip("/")


def get_app_version() -> str:
    return config.load_app_version()


def build_server(host: str, port: int, use_https: bool = False) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer((host, port), AppHandler)
    scheme = "http"
    server.is_https = use_https
    if use_https:
        cert_path, key_path = config.ensure_https_certificate()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    return server, scheme


def base_url_from_request(handler: BaseHTTPRequestHandler) -> str:
    configured = get_share_base_url()
    if configured:
        return configured

    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").strip()
    proto = forwarded_proto or (
        "https" if getattr(handler.server, "server_port", 0) == 443 else "http"
    )
    host = handler.headers.get("Host", "").strip()
    if host:
        return f"{proto}://{host}".rstrip("/")

    server_host, server_port = handler.server.server_address[:2]
    return f"http://{server_host}:{server_port}"


def share_payload(entry_type: str, entry: dict, base_url: str) -> dict:
    path = f"/s/{urllib.parse.quote(entry['short_code'])}"
    workspace_id = entry.get("workspace_id", config.DEFAULT_WORKSPACE_ID)
    workspace_name = config.DEFAULT_WORKSPACE_NAME
    workspace_slug_value = storage.workspace_slug(config.DEFAULT_WORKSPACE_NAME)
    with state.state_lock:
        workspace = storage.get_workspace_locked(workspace_id)
        if workspace is not None:
            workspace_name = workspace["name"]
            workspace_slug_value = storage.workspace_slug(workspace["name"])
    payload = {
        "type": entry_type,
        "id": entry["id"],
        "short_code": entry["short_code"],
        "share_path": path,
        "share_url": f"{base_url.rstrip('/')}{path}",
        "hidden": bool(entry.get("hidden", False)),
        "password_required": bool(entry.get("password_hash")),
        "created_at": entry["created_at"],
        "expires_at": entry["expires_at"],
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "workspace_slug": workspace_slug_value,
        "workspace_path": f"/w/{urllib.parse.quote(workspace_slug_value)}",
        "workspace_url": f"{base_url.rstrip('/')}/w/{urllib.parse.quote(workspace_slug_value)}",
    }
    if entry_type == "text":
        payload["content"] = entry["content"]
    else:
        payload["name"] = entry["name"]
        payload["size"] = entry["size"]
        payload["download_path"] = f"/download/{urllib.parse.quote(entry['id'])}"
        payload["download_url"] = f"{base_url.rstrip('/')}{payload['download_path']}"
    return payload


def render_template(name: str, replacements: dict[str, str] | None = None) -> str:
    template_path = config.TEMPLATES_DIR / name
    body = template_path.read_text(encoding="utf-8")
    for needle, value in (replacements or {}).items():
        body = body.replace(needle, value)
    return body


class AppHandler(BaseHTTPRequestHandler):
    server_version = "DassieDrop/1.2"

    def send_throttled(self, message: str, retry_after: int) -> None:
        data = message.encode("utf-8")
        self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.serve_asset("DassieDrop-dassie-icon.png")
            return

        if parsed.path.startswith("/assets/"):
            asset_name = parsed.path.removeprefix("/assets/")
            self.serve_asset(asset_name)
            return

        if parsed.path == "/":
            self.handle_root()
            return

        if parsed.path == "/workspaces":
            self.handle_workspaces_page()
            return

        if parsed.path.startswith("/w/"):
            workspace_slug_value = urllib.parse.unquote(parsed.path.removeprefix("/w/"))
            self.handle_workspace_shortcut(workspace_slug_value)
            return

        if parsed.path == "/api/workspaces":
            if config.ACCESS_CODE and not auth.is_authorized(self):
                self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
                return
            self.send_json(self.workspace_list_payload())
            return

        if config.ACCESS_CODE and not auth.is_authorized(self):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return

        if parsed.path == "/ws":
            self.handle_websocket()
            return

        if parsed.path == "/api/state":
            workspace_id = self.require_workspace_context()
            if workspace_id is None:
                return
            self.send_json(storage.get_snapshot(workspace_id))
            return

        if parsed.path == "/api/latest-text":
            self.handle_latest_text()
            return

        if parsed.path == "/api/latest-file":
            self.handle_latest_file()
            return

        if parsed.path == "/api/latest-file/content":
            self.handle_latest_file_content()
            return

        if parsed.path.startswith("/s/"):
            short_code = urllib.parse.unquote(parsed.path.removeprefix("/s/"))
            self.handle_short_link(short_code, auth.requested_entry_password(self))
            return

        if parsed.path.startswith("/download/"):
            file_id = urllib.parse.unquote(parsed.path.removeprefix("/download/"))
            self.serve_download(file_id, auth.requested_entry_password(self))
            return

        if parsed.path.startswith("/preview/"):
            file_id = urllib.parse.unquote(parsed.path.removeprefix("/preview/"))
            self.serve_preview(file_id, auth.requested_entry_password(self))
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            self.handle_login()
            return

        if config.ACCESS_CODE and not auth.is_authorized(self):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return

        if parsed.path == "/api/workspaces":
            self.handle_workspace_create()
            return

        if parsed.path.startswith("/api/workspaces/") and parsed.path.endswith("/enter"):
            workspace_id = urllib.parse.unquote(
                parsed.path.removeprefix("/api/workspaces/").removesuffix("/enter")
            )
            self.handle_workspace_enter(workspace_id)
            return

        if parsed.path == "/api/text":
            self.handle_text_update()
            return

        if parsed.path == "/api/share-text":
            self.handle_text_share()
            return

        if parsed.path.startswith("/api/text/") and parsed.path.endswith("/reveal"):
            entry_id = urllib.parse.unquote(
                parsed.path.removeprefix("/api/text/").removesuffix("/reveal")
            )
            self.handle_text_reveal(entry_id)
            return

        if parsed.path == "/api/upload":
            self.handle_file_upload()
            return

        if parsed.path == "/api/share-file":
            self.handle_file_share()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if config.ACCESS_CODE and not auth.is_authorized(self):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return

        if parsed.path.startswith("/api/workspaces/"):
            workspace_id = urllib.parse.unquote(parsed.path.removeprefix("/api/workspaces/"))
            self.handle_workspace_delete(workspace_id)
            return

        if parsed.path.startswith("/api/text/"):
            entry_id = urllib.parse.unquote(parsed.path.removeprefix("/api/text/"))
            self.handle_text_delete(entry_id)
            return

        if parsed.path.startswith("/api/file/"):
            file_id = urllib.parse.unquote(parsed.path.removeprefix("/api/file/"))
            self.handle_file_delete(file_id)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def current_session_workspace_id(self) -> str | None:
        _, session = auth.get_session(self)
        if session is None:
            return None
        workspace_id = session.get("workspace_id")
        if not workspace_id:
            return None
        with state.state_lock:
            if storage.get_workspace_locked(workspace_id) is None:
                return None
        return workspace_id

    def workspace_list_payload(self) -> dict:
        return {
            "workspaces": storage.list_workspaces(),
            "current_workspace_id": self.current_session_workspace_id(),
        }

    def handle_root(self) -> None:
        if config.ACCESS_CODE and not auth.is_authorized(self):
            self.send_html(render_template("login.html"))
            return

        session_id, session, cookie = auth.ensure_browser_session(self)
        if session is None or session_id is None:
            self.send_html(render_template("login.html"))
            return

        workspace_id = session.get("workspace_id")
        with state.state_lock:
            workspace = storage.get_workspace_locked(workspace_id) if workspace_id else None
        if workspace is None:
            self.redirect("/workspaces", cookie=cookie)
            return

        self.send_html(
            render_template(
                "index.html",
                {
                    "__SHARE_BASE_URL__": json.dumps(get_share_base_url()),
                    "__APP_VERSION__": html.escape(get_app_version()),
                    "__WORKSPACE_NAME__": html.escape(storage.compact_workspace_name(workspace["name"])),
                },
            ),
            cookie=cookie,
        )

    def handle_workspaces_page(self) -> None:
        if config.ACCESS_CODE and not auth.is_authorized(self):
            self.send_html(render_template("login.html"))
            return

        _, _, cookie = auth.ensure_browser_session(self)
        self.send_html(
            render_template(
                "workspaces.html",
                {"__APP_VERSION__": html.escape(get_app_version())},
            ),
            cookie=cookie,
        )

    def handle_workspace_shortcut(self, workspace_slug_value: str) -> None:
        if config.ACCESS_CODE and not auth.is_authorized(self):
            self.send_html(render_template("login.html"))
            return

        session_id, session, cookie = auth.ensure_browser_session(self)
        if session is None or session_id is None:
            self.send_html(render_template("login.html"))
            return

        with state.state_lock:
            workspace = storage.get_workspace_by_slug_locked(workspace_slug_value)
            if workspace is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Workspace not found")
                return
            if workspace.get("password_hash"):
                current_workspace_id = session.get("workspace_id")
                password = auth.requested_workspace_password(self)
                allowed, retry_after = auth.throttle_status(self, "workspace-shortcut", workspace["id"])
                if not allowed:
                    self.send_throttled("Too many password attempts", retry_after)
                    return
                if current_workspace_id != workspace["id"] and not storage.workspace_password_is_valid(
                    workspace, password
                ):
                    auth.record_throttle_failure(self, "workspace-shortcut", workspace["id"])
                    self.redirect(
                        f"/workspaces?workspace={urllib.parse.quote(workspace_slug_value)}",
                        cookie=cookie,
                    )
                    return
                auth.clear_throttle_failures(self, "workspace-shortcut", workspace["id"])
            storage.touch_workspace_locked(workspace, persist_interval=0.0)
            storage.persist_workspaces_locked()

        auth.set_session_workspace(session_id, workspace["id"])
        self.redirect("/", cookie=cookie)

    def handle_login(self) -> None:
        if not config.ACCESS_CODE:
            session_id = auth.create_authorized_session(config.DEFAULT_WORKSPACE_ID)
            self.send_json(
                {"ok": True},
                cookie=auth.session_cookie(session_id, secure=bool(getattr(self.server, "is_https", False))),
            )
            return

        allowed, retry_after = auth.throttle_status(self, "login")
        if not allowed:
            self.send_throttled("Too many access code attempts", retry_after)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        code = payload.get("code", "")
        if not isinstance(code, str) or not hmac.compare_digest(code, config.ACCESS_CODE):
            auth.record_throttle_failure(self, "login")
            self.send_error(HTTPStatus.UNAUTHORIZED, "Wrong access code")
            return

        auth.clear_throttle_failures(self, "login")
        session_id = auth.create_authorized_session(config.DEFAULT_WORKSPACE_ID)
        self.send_json(
            {"ok": True},
            cookie=auth.session_cookie(session_id, secure=bool(getattr(self.server, "is_https", False))),
        )

    def parse_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return None
        if not isinstance(payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return None
        return payload

    def handle_workspace_create(self) -> None:
        payload = self.parse_json_body()
        if payload is None:
            return

        name = payload.get("name", "")
        if not isinstance(name, str) or not name.strip():
            self.send_error(HTTPStatus.BAD_REQUEST, "Workspace name is required")
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return

        workspace = storage.create_workspace(name, password=password.strip())
        self.send_json(
            {
                "workspace": workspace,
                "workspaces": storage.list_workspaces(),
                "current_workspace_id": self.current_session_workspace_id(),
            }
        )

    def handle_workspace_enter(self, workspace_id: str) -> None:
        session_id, session = auth.get_session(self)
        if session_id is None or session is None:
            self.send_error(HTTPStatus.UNAUTHORIZED, "Session required")
            return
        payload = self.parse_json_body()
        if payload is None:
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return
        allowed, retry_after = auth.throttle_status(self, "workspace-enter", workspace_id)
        if not allowed:
            self.send_throttled("Too many workspace password attempts", retry_after)
            return
        ok, message = storage.enter_workspace(session_id, workspace_id, password=password)
        if not ok:
            status = HTTPStatus.NOT_FOUND if message == "Workspace not found" else HTTPStatus.FORBIDDEN
            if status == HTTPStatus.FORBIDDEN:
                auth.record_throttle_failure(self, "workspace-enter", workspace_id)
            self.send_error(status, message)
            return
        auth.clear_throttle_failures(self, "workspace-enter", workspace_id)
        self.send_json({"ok": True, "workspace_id": workspace_id})

    def handle_workspace_delete(self, workspace_id: str) -> None:
        payload = self.parse_json_body()
        if payload is None:
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return
        allowed, retry_after = auth.throttle_status(self, "workspace-delete", workspace_id)
        if not allowed:
            self.send_throttled("Too many workspace password attempts", retry_after)
            return
        ok, message = storage.delete_workspace(workspace_id, password=password)
        if not ok:
            status = HTTPStatus.NOT_FOUND if message == "Workspace not found" else HTTPStatus.FORBIDDEN
            if status == HTTPStatus.FORBIDDEN:
                auth.record_throttle_failure(self, "workspace-delete", workspace_id)
            self.send_error(status, message)
            return
        auth.clear_throttle_failures(self, "workspace-delete", workspace_id)
        self.send_json(self.workspace_list_payload())

    def require_workspace_context(self) -> str | None:
        explicit_workspace_selector = auth.requested_workspace_selector(self)
        if explicit_workspace_selector:
            with state.state_lock:
                workspace = storage.resolve_workspace_selector_locked(explicit_workspace_selector)
                if workspace is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Workspace not found")
                    return None
                allowed, retry_after = auth.throttle_status(self, "workspace-context", workspace["id"])
                if not allowed:
                    self.send_throttled("Too many workspace password attempts", retry_after)
                    return None
                if workspace.get("password_hash") and not storage.workspace_password_is_valid(
                    workspace, auth.requested_workspace_password(self)
                ):
                    auth.record_throttle_failure(self, "workspace-context", workspace["id"])
                    self.send_error(HTTPStatus.FORBIDDEN, "Wrong workspace password")
                    return None
                auth.clear_throttle_failures(self, "workspace-context", workspace["id"])
                return workspace["id"]

        session_id, session = auth.get_session(self)
        if session_id is not None and session is not None:
            workspace_id = session.get("workspace_id")
            if not workspace_id:
                self.send_error(HTTPStatus.CONFLICT, "Workspace not selected")
                return None
            with state.state_lock:
                workspace = storage.get_workspace_locked(workspace_id)
            if workspace is None:
                auth.set_session_workspace(session_id, None)
                self.send_error(HTTPStatus.CONFLICT, "Workspace not selected")
                return None
            return workspace_id

        if config.ACCESS_CODE:
            if self.headers.get("X-API-Key", "").strip() == config.ACCESS_CODE:
                return config.DEFAULT_WORKSPACE_ID
            self.send_error(HTTPStatus.UNAUTHORIZED, "Access code required")
            return None

        return config.DEFAULT_WORKSPACE_ID

    def handle_text_update(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        entry = self.parse_text_request()
        if entry is None:
            return

        storage.add_text_entry(
            entry["text"],
            hidden=entry["hidden"],
            password=entry["password"],
            sharer_name=entry["name"],
            sharer_ip=self.client_address[0],
            workspace_id=workspace_id,
        )
        snapshot = storage.get_snapshot(workspace_id)
        self.send_json(snapshot)
        websocket.broadcast_snapshot(workspace_id, snapshot)

    def handle_text_share(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        entry = self.parse_text_request()
        if entry is None:
            return

        storage.add_text_entry(
            entry["text"],
            hidden=entry["hidden"],
            password=entry["password"],
            sharer_name=entry["name"],
            sharer_ip=self.client_address[0],
            workspace_id=workspace_id,
        )
        created = storage.find_text_entry(storage.get_snapshot(workspace_id)["texts"][0]["id"], workspace_id=workspace_id)
        if created is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not create text entry")
            return
        snapshot = storage.get_snapshot(workspace_id)
        self.send_json(share_payload("text", created, base_url_from_request(self)))
        websocket.broadcast_snapshot(workspace_id, snapshot)

    def parse_text_request(self) -> dict | None:
        payload = self.parse_json_body()
        if payload is None:
            return None

        text = payload.get("text", "")
        if not isinstance(text, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Text must be a string")
            return None
        normalized_text = text.strip()
        if not normalized_text:
            self.send_error(HTTPStatus.BAD_REQUEST, "Text cannot be empty")
            return None
        hidden = payload.get("hidden", False)
        if not isinstance(hidden, bool):
            self.send_error(HTTPStatus.BAD_REQUEST, "Hidden must be a boolean")
            return None
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return None
        sharer_name = payload.get("name", "")
        if not isinstance(sharer_name, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Name must be a string")
            return None

        return {
            "text": normalized_text,
            "hidden": hidden,
            "password": password.strip(),
            "name": sharer_name.strip(),
        }

    def handle_text_reveal(self, entry_id: str) -> None:
        entry = storage.find_text_entry(entry_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Text entry not found")
            return

        payload = self.parse_json_body()
        if payload is None:
            return
        password = payload.get("password", "")
        if not isinstance(password, str):
            self.send_error(HTTPStatus.BAD_REQUEST, "Password must be a string")
            return
        allowed, retry_after = auth.throttle_status(self, "text-reveal", entry_id)
        if not allowed:
            self.send_throttled("Too many password attempts", retry_after)
            return
        if not storage.entry_password_is_valid(entry, password):
            auth.record_throttle_failure(self, "text-reveal", entry_id)
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return

        auth.clear_throttle_failures(self, "text-reveal", entry_id)
        self.send_json({"content": entry["content"]})

    def handle_latest_text(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        entry = storage.get_latest_text_entry(workspace_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No text entries found")
            return
        self.send_json(entry)

    def handle_latest_file(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        entry = storage.get_latest_file_entry(workspace_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No file entries found")
            return
        self.send_json(entry)

    def handle_latest_file_content(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        latest_entry = storage.get_latest_file_entry(workspace_id)
        if latest_entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No file entries found")
            return
        entry = storage.find_file_entry(latest_entry["id"], workspace_id=workspace_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No file entries found")
            return
        self.serve_file_entry(entry, as_attachment=True)

    def handle_short_link(self, short_code: str, password: str = "") -> None:
        entry = storage.find_entry_by_short_code(short_code)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Shared item not found")
            return

        entry_type, payload = entry
        if entry_type == "text":
            allowed, retry_after = auth.throttle_status(self, "short-link", payload["id"])
            if not allowed:
                self.send_throttled("Too many password attempts", retry_after)
                return
            if payload.get("password_hash") and not storage.entry_password_is_valid(payload, password):
                auth.record_throttle_failure(self, "short-link", payload["id"])
                self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
                return
            auth.clear_throttle_failures(self, "short-link", payload["id"])
            self.send_text(payload["content"])
            return

        allowed, retry_after = auth.throttle_status(self, "short-link", payload["id"])
        if not allowed:
            self.send_throttled("Too many password attempts", retry_after)
            return
        if payload.get("password_hash") and not storage.entry_password_is_valid(payload, password):
            auth.record_throttle_failure(self, "short-link", payload["id"])
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return
        auth.clear_throttle_failures(self, "short-link", payload["id"])
        self.serve_file_entry(payload, as_attachment=True)

    def handle_text_delete(self, entry_id: str) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        if not storage.delete_text_entry(entry_id, workspace_id=workspace_id):
            self.send_error(HTTPStatus.NOT_FOUND, "Text entry not found")
            return
        snapshot = storage.get_snapshot(workspace_id)
        self.send_json(snapshot)
        websocket.broadcast_snapshot(workspace_id, snapshot)

    def handle_file_delete(self, file_id: str) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        if not storage.delete_file_entry(file_id, workspace_id=workspace_id):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        snapshot = storage.get_snapshot(workspace_id)
        self.send_json(snapshot)
        websocket.broadcast_snapshot(workspace_id, snapshot)

    def handle_file_upload(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        parsed = self.parse_file_upload_request()
        if parsed is None:
            return

        self.store_file_upload(parsed, workspace_id)
        snapshot = storage.get_snapshot(workspace_id)
        self.send_json(snapshot)
        websocket.broadcast_snapshot(workspace_id, snapshot)

    def handle_file_share(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return
        parsed = self.parse_file_upload_request()
        if parsed is None:
            return

        created = self.store_file_upload(parsed, workspace_id)
        if created is None:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not create file entry")
            return
        snapshot = storage.get_snapshot(workspace_id)
        self.send_json(share_payload("file", created, base_url_from_request(self)))
        websocket.broadcast_snapshot(workspace_id, snapshot)

    def parse_file_upload_request(self) -> dict | None:
        content_type = self.headers.get("Content-Type", "")
        boundary = None
        for item in content_type.split(";"):
            item = item.strip()
            if item.startswith("boundary="):
                boundary = item.split("=", 1)[1].encode("utf-8")
                break

        if not boundary:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing multipart boundary")
            return None

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Empty upload")
            return None
        if length > config.MAX_FILE_SIZE:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File too large")
            return None

        body = self.rfile.read(length)
        filename, file_bytes, fields = self.parse_multipart_file(body, boundary)
        if filename is None or file_bytes is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Could not read uploaded file")
            return None
        hidden = fields.get("hidden", "false").lower() == "true"
        password = fields.get("password", "").strip()
        sharer_name = fields.get("name", "").strip()
        if hidden and not password:
            self.send_error(HTTPStatus.BAD_REQUEST, "Hidden files require a password")
            return None

        return {
            "filename": filename,
            "file_bytes": file_bytes,
            "hidden": hidden,
            "password": password,
            "name": sharer_name,
        }

    def store_file_upload(self, parsed: dict, workspace_id: str) -> dict | None:
        storage.ensure_upload_dir()
        stored_name = storage.unique_filename(parsed["filename"])
        target = config.UPLOAD_DIR / stored_name
        with target.open("wb") as handle:
            handle.write(parsed["file_bytes"])

        storage.add_file(
            parsed["filename"],
            stored_name,
            len(parsed["file_bytes"]),
            hidden=parsed["hidden"],
            password=parsed["password"],
            sharer_name=parsed["name"],
            sharer_ip=self.client_address[0],
            workspace_id=workspace_id,
        )
        return storage.find_file_entry(storage.get_snapshot(workspace_id)["files"][0]["id"], workspace_id=workspace_id)

    def parse_multipart_file(self, body: bytes, boundary: bytes):
        marker = b"--" + boundary
        parts = body.split(marker)
        fields = {}
        upload_name = None
        upload_payload = None
        for part in parts:
            if not part or part in (b"--\r\n", b"--"):
                continue
            part = part.lstrip(b"\r\n")
            headers_blob, separator, payload = part.partition(b"\r\n\r\n")
            if not separator:
                continue

            headers_text = headers_blob.decode("utf-8", errors="ignore")
            field_name = None
            filename = None
            for line in headers_text.split("\r\n"):
                lower = line.lower()
                if lower.startswith("content-disposition:"):
                    for piece in line.split(";"):
                        piece = piece.strip()
                        if piece.startswith("name="):
                            field_name = piece.split("=", 1)[1].strip("\"")
                        if piece.startswith("filename="):
                            filename = piece.split("=", 1)[1].strip("\"")

            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            if payload.endswith(b"--"):
                payload = payload[:-2]

            if field_name == "file":
                upload_name = storage.sanitize_filename(filename or "upload.bin")
                upload_payload = payload
            elif field_name:
                fields[field_name] = payload.decode("utf-8", errors="ignore")

        return upload_name, upload_payload, fields

    def handle_websocket(self) -> None:
        workspace_id = self.require_workspace_context()
        if workspace_id is None:
            return

        upgrade = self.headers.get("Upgrade", "")
        connection = self.headers.get("Connection", "")
        websocket_key = self.headers.get("Sec-WebSocket-Key", "")
        websocket_version = self.headers.get("Sec-WebSocket-Version", "")

        if upgrade.lower() != "websocket" or "upgrade" not in connection.lower():
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected WebSocket upgrade")
            return
        if not websocket_key or websocket_version != "13":
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid WebSocket headers")
            return

        accept_value = websocket.websocket_accept_value(websocket_key)
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_value)
        self.end_headers()

        client = websocket.WebSocketClient(self.connection, workspace_id)
        websocket.register_websocket_client(client)
        client.send_json(storage.get_snapshot(workspace_id))

        try:
            while True:
                opcode, payload = self.read_websocket_frame()
                if opcode is None:
                    break
                if opcode == 0x8:
                    client.send_frame(0x8, payload[:2] if payload else b"")
                    break
                if opcode == 0x9:
                    client.send_frame(0xA, payload)
        finally:
            websocket.unregister_websocket_client(client)

    def read_exact(self, length: int) -> bytes | None:
        remaining = length
        chunks = []
        while remaining > 0:
            try:
                chunk = self.rfile.read(remaining)
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_websocket_frame(self) -> tuple[int | None, bytes]:
        header = self.read_exact(2)
        if not header:
            return (None, b"")

        first_byte, second_byte = header
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        payload_length = second_byte & 0x7F

        if payload_length == 126:
            extended = self.read_exact(2)
            if extended is None:
                return (None, b"")
            payload_length = int.from_bytes(extended, "big")
        elif payload_length == 127:
            extended = self.read_exact(8)
            if extended is None:
                return (None, b"")
            payload_length = int.from_bytes(extended, "big")

        masking_key = self.read_exact(4) if masked else b""
        if masked and masking_key is None:
            return (None, b"")

        payload = self.read_exact(payload_length) if payload_length else b""
        if payload is None:
            return (None, b"")

        if masked:
            payload = bytes(
                byte ^ masking_key[index % 4] for index, byte in enumerate(payload)
            )
        return (opcode, payload)

    def serve_download(self, file_id: str, password: str = "") -> None:
        entry = storage.find_file_entry(file_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        allowed, retry_after = auth.throttle_status(self, "file-download", file_id)
        if not allowed:
            self.send_throttled("Too many password attempts", retry_after)
            return
        if entry.get("password_hash") and not storage.entry_password_is_valid(entry, password):
            auth.record_throttle_failure(self, "file-download", file_id)
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return
        auth.clear_throttle_failures(self, "file-download", file_id)
        self.serve_file_entry(entry, as_attachment=True)

    def serve_preview(self, file_id: str, password: str = "") -> None:
        entry = storage.find_file_entry(file_id)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        allowed, retry_after = auth.throttle_status(self, "file-preview", file_id)
        if not allowed:
            self.send_throttled("Too many password attempts", retry_after)
            return
        if entry.get("password_hash") and not storage.entry_password_is_valid(entry, password):
            auth.record_throttle_failure(self, "file-preview", file_id)
            self.send_error(HTTPStatus.FORBIDDEN, "Wrong password")
            return
        auth.clear_throttle_failures(self, "file-preview", file_id)
        self.serve_file_entry(entry, as_attachment=False)

    def serve_file_entry(self, entry: dict, as_attachment: bool) -> None:
        target = config.UPLOAD_DIR / entry["stored_name"]
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        content_type = storage.guess_content_type(entry["name"])
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        disposition = "attachment" if as_attachment else "inline"
        self.send_header(
            "Content-Disposition",
            f"{disposition}; filename*=UTF-8''{urllib.parse.quote(entry['name'])}",
        )
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def serve_asset(self, asset_name: str) -> None:
        safe_name = urllib.parse.unquote(asset_name)
        safe_name = safe_name.split("/")[-1]
        target = config.ASSETS_DIR / safe_name
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
            return

        content_type = storage.guess_content_type(target.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def send_html(self, body: str, cookie: str | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, cookie: str | None = None) -> None:
        data = storage.json_bytes(payload)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args
        print(f"[{self.log_date_time_string()}] {html.escape(message)}")
