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


def get_session_by_id(session_id: str, touch: bool = False) -> dict | None:
    if not session_id:
        return None
    with state.session_lock:
        session = state.authorized_sessions.get(session_id)
        if session is None:
            return None
        now = config.now_ts()
        last_seen = float(session.get("last_seen_at") or session.get("created_at") or now)
        if now - last_seen > config.SESSION_TTL_SECONDS:
            state.authorized_sessions.pop(session_id, None)
            return None
        if touch:
            session["last_seen_at"] = now
        return session


def get_session(handler: BaseHTTPRequestHandler) -> tuple[str | None, dict | None]:
    cookies = parse_cookies(handler.headers.get("Cookie", ""))
    session_id = cookies.get("session")
    if not session_id:
        return (None, None)
    session = get_session_by_id(session_id, touch=True)
    if session is None:
        return (None, None)
    return (session_id, session)


def is_authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not config.ACCESS_CODE:
        return True

    if api_key_is_valid(handler):
        return True

    session_id, _ = get_session(handler)
    return session_id is not None


def expected_api_key() -> str:
    return config.API_KEY or config.ACCESS_CODE


def api_key_is_valid(handler: BaseHTTPRequestHandler) -> bool:
    configured = expected_api_key()
    if not configured:
        return False
    api_key = handler.headers.get("X-API-Key", "").strip()
    return bool(api_key) and hmac.compare_digest(api_key, configured)


def create_authorized_session(workspace_id: str | None = None) -> str:
    session_id = make_session_id()
    now = config.now_ts()
    with state.session_lock:
        state.authorized_sessions[session_id] = {
            "workspace_id": workspace_id,
            "csrf_token": secrets.token_urlsafe(24),
            "created_at": now,
            "last_seen_at": now,
        }
    return session_id


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    return str(handler.client_address[0])


def throttle_scope(scope: str, subject: str = "") -> str:
    if subject:
        return f"{scope}:{subject}"
    return scope


def throttle_key(handler: BaseHTTPRequestHandler, scope: str, subject: str = "") -> str:
    return f"{client_ip(handler)}|{throttle_scope(scope, subject)}"


def throttle_status(handler: BaseHTTPRequestHandler, scope: str, subject: str = "") -> tuple[bool, int]:
    now = config.now_ts()
    key = throttle_key(handler, scope, subject)
    with state.auth_attempt_lock:
        attempt = state.auth_attempts.get(key)
        if attempt is None:
            return (True, 0)
        locked_until = float(attempt.get("locked_until", 0.0) or 0.0)
        failures = [
            ts
            for ts in attempt.get("failures", [])
            if now - float(ts) <= config.AUTH_FAILURE_WINDOW_SECONDS
        ]
        if failures != attempt.get("failures", []):
            attempt["failures"] = failures
        if locked_until > now:
            return (False, max(1, int(locked_until - now)))
        if not failures:
            state.auth_attempts.pop(key, None)
        return (True, 0)


def record_throttle_failure(handler: BaseHTTPRequestHandler, scope: str, subject: str = "") -> int:
    now = config.now_ts()
    key = throttle_key(handler, scope, subject)
    with state.auth_attempt_lock:
        attempt = state.auth_attempts.setdefault(key, {"failures": [], "locked_until": 0.0})
        locked_until = float(attempt.get("locked_until", 0.0) or 0.0)
        if locked_until > now:
            return max(1, int(locked_until - now))
        failures = [
            ts
            for ts in attempt.get("failures", [])
            if now - float(ts) <= config.AUTH_FAILURE_WINDOW_SECONDS
        ]
        failures.append(now)
        attempt["failures"] = failures
        if len(failures) >= config.AUTH_MAX_FAILURES:
            attempt["locked_until"] = now + config.AUTH_LOCKOUT_SECONDS
            attempt["failures"] = []
            return config.AUTH_LOCKOUT_SECONDS
        attempt["locked_until"] = 0.0
        return 0


def clear_throttle_failures(handler: BaseHTTPRequestHandler, scope: str, subject: str = "") -> None:
    key = throttle_key(handler, scope, subject)
    with state.auth_attempt_lock:
        state.auth_attempts.pop(key, None)


def cleanup_throttle_failures() -> None:
    now = config.now_ts()
    with state.auth_attempt_lock:
        expired_keys = []
        for key, attempt in state.auth_attempts.items():
            locked_until = float(attempt.get("locked_until", 0.0) or 0.0)
            failures = [
                ts
                for ts in attempt.get("failures", [])
                if now - float(ts) <= config.AUTH_FAILURE_WINDOW_SECONDS
            ]
            if locked_until > now:
                attempt["failures"] = failures
                continue
            if failures:
                attempt["failures"] = failures
                attempt["locked_until"] = 0.0
                continue
            expired_keys.append(key)
        for key in expired_keys:
            state.auth_attempts.pop(key, None)


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


def csrf_token(session: dict | None) -> str:
    if not session:
        return ""
    value = session.get("csrf_token")
    return value if isinstance(value, str) else ""


def csrf_required(handler: BaseHTTPRequestHandler) -> bool:
    if handler.command not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if handler.path == "/login":
        return False
    if api_key_is_valid(handler):
        return False
    return bool(parse_cookies(handler.headers.get("Cookie", "")).get("session"))


def validate_csrf(handler: BaseHTTPRequestHandler) -> bool:
    if not csrf_required(handler):
        return True
    _, session = get_session(handler)
    if session is None:
        return False
    return hmac.compare_digest(
        handler.headers.get("X-CSRF-Token", "").strip(),
        csrf_token(session),
    )


def cleanup_authorized_sessions() -> None:
    now = config.now_ts()
    with state.session_lock:
        expired = [
            session_id
            for session_id, session in state.authorized_sessions.items()
            if now - float(session.get("last_seen_at") or session.get("created_at") or now)
            > config.SESSION_TTL_SECONDS
        ]
        for session_id in expired:
            state.authorized_sessions.pop(session_id, None)


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
    header_value = handler.headers.get("X-Workspace-Password", "")
    return header_value.strip()


def requested_entry_password(handler: BaseHTTPRequestHandler) -> str:
    return handler.headers.get("X-Entry-Password", "").strip()


def requested_access_password(handler: BaseHTTPRequestHandler) -> str:
    return handler.headers.get("X-Access-Password", "").strip()


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
