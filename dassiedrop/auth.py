import hmac
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler

from . import config, state, storage


def parse_cookies(cookie_header: str) -> dict:
    cookies = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def make_session_id() -> str:
    return secrets.token_urlsafe(24)


def session_cookie(session_id: str, secure: bool = False) -> str:
    secure_suffix = "; Secure" if secure else ""
    return f"session={session_id}; Path=/; HttpOnly; SameSite=Lax{secure_suffix}"


def get_session(handler: BaseHTTPRequestHandler) -> tuple[str | None, dict | None]:
    cookies = parse_cookies(handler.headers.get("Cookie", ""))
    session_id = cookies.get("session")
    if not session_id:
        return (None, None)
    with state.session_lock:
        session = state.authorized_sessions.get(session_id)
        if session is None:
            return (None, None)
        return (session_id, session)


def is_authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not config.ACCESS_CODE:
        return True

    api_key = handler.headers.get("X-API-Key", "").strip()
    if api_key and hmac.compare_digest(api_key, config.ACCESS_CODE):
        return True

    session_id, _ = get_session(handler)
    return session_id is not None


def create_authorized_session(workspace_id: str | None = None) -> str:
    session_id = make_session_id()
    with state.session_lock:
        state.authorized_sessions[session_id] = {"workspace_id": workspace_id}
    return session_id


def ensure_browser_session(handler: BaseHTTPRequestHandler) -> tuple[str | None, dict | None, str | None]:
    session_id, session = get_session(handler)
    if session_id is not None and session is not None:
        return (session_id, session, None)
    if config.ACCESS_CODE:
        return (None, None, None)
    session_id = create_authorized_session()
    with state.session_lock:
        session = state.authorized_sessions[session_id]
    return (
        session_id,
        session,
        session_cookie(session_id, secure=bool(getattr(handler.server, "is_https", False))),
    )


def requested_workspace_selector(handler: BaseHTTPRequestHandler) -> str:
    parsed = urllib.parse.urlparse(handler.path)
    query_values = urllib.parse.parse_qs(parsed.query)
    query_value = query_values.get("workspace", [""])[0]
    query_name_value = query_values.get("workspace_name", [""])[0]
    header_value = handler.headers.get("X-Workspace-ID", "")
    header_name_value = handler.headers.get("X-Workspace-Name", "")
    value = (header_value or header_name_value or query_value or query_name_value).strip()
    return value


def requested_workspace_password(handler: BaseHTTPRequestHandler) -> str:
    parsed = urllib.parse.urlparse(handler.path)
    query_value = urllib.parse.parse_qs(parsed.query).get("workspace_password", [""])[0]
    header_value = handler.headers.get("X-Workspace-Password", "")
    return (header_value or query_value).strip()


def set_session_workspace(session_id: str, workspace_id: str | None) -> None:
    with state.session_lock:
        session = state.authorized_sessions.get(session_id)
        if session is not None:
            session["workspace_id"] = workspace_id


def clear_workspace_selection_for_deleted_workspace(workspace_id: str) -> None:
    with state.session_lock:
        for session in state.authorized_sessions.values():
            if session.get("workspace_id") == workspace_id:
                session["workspace_id"] = None
