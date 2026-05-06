import os
import subprocess
import time
from ipaddress import ip_address
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"
TEMPLATES_DIR = BASE_DIR / "templates"
VERSION_FILE = BASE_DIR / "VERSION"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads"))).resolve()
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GB
MAX_JSON_BODY_SIZE = int(os.environ.get("MAX_JSON_BODY_SIZE", str(1024 * 1024)))
MAX_TOTAL_STORAGE_BYTES = int(os.environ.get("MAX_TOTAL_STORAGE_BYTES", "0"))
EXPIRY_SECONDS = 24 * 60 * 60
MAX_TEXT_HISTORY = 200
MAX_FILE_HISTORY = 100
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()
SHARE_BASE_URL = os.environ.get("SHARE_BASE_URL", "").strip()
WORKSPACE_SUPER_PASSWORD = os.environ.get("WORKSPACE_SUPER_PASSWORD", "").strip()
HTTPS_ENABLED = os.environ.get("HTTPS", "").strip().lower() in {"1", "true", "yes", "on"}
HTTP_PORT = int(os.environ.get("HTTP_PORT", os.environ.get("PORT", "8000")))
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "8443"))
HTTPS_CERT_FILE = Path(
    os.environ.get("HTTPS_CERT_FILE", str(BASE_DIR / "certs" / "dassiedrop-selfsigned.crt"))
).resolve()
HTTPS_KEY_FILE = Path(
    os.environ.get("HTTPS_KEY_FILE", str(BASE_DIR / "certs" / "dassiedrop-selfsigned.key"))
).resolve()
HTTPS_SELF_SIGNED_HOST = os.environ.get("HTTPS_SELF_SIGNED_HOST", "localhost").strip() or "localhost"
HTTPS_SELF_SIGNED_SANS = os.environ.get("HTTPS_SELF_SIGNED_SANS", "").strip()
DEFAULT_WORKSPACE_ID = "default"
DEFAULT_WORKSPACE_NAME = "Default"
AUTH_FAILURE_WINDOW_SECONDS = int(os.environ.get("AUTH_FAILURE_WINDOW_SECONDS", "60"))
AUTH_MAX_FAILURES = int(os.environ.get("AUTH_MAX_FAILURES", "5"))
AUTH_LOCKOUT_SECONDS = int(os.environ.get("AUTH_LOCKOUT_SECONDS", "60"))
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(7 * 24 * 60 * 60)))
JANITOR_INTERVAL_SECONDS = float(os.environ.get("JANITOR_INTERVAL_SECONDS", "30"))


def now_ts() -> float:
    return time.time()


def load_app_version() -> str:
    configured = os.environ.get("APP_VERSION", "").strip()
    if configured:
        return configured
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        version = ""
    return version or "dev"


def is_ip_literal(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


def default_https_subject_alt_names(hostname: str) -> str:
    entries = ["DNS:localhost", "IP:127.0.0.1"]
    if hostname and hostname != "localhost":
        if is_ip_literal(hostname):
            entries.append(f"IP:{hostname}")
        else:
            entries.append(f"DNS:{hostname}")
    return ",".join(entries)


def ensure_https_certificate() -> tuple[Path, Path]:
    cert_path = HTTPS_CERT_FILE
    key_path = HTTPS_KEY_FILE
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    subject_alt_names = HTTPS_SELF_SIGNED_SANS or default_https_subject_alt_names(
        HTTPS_SELF_SIGNED_HOST
    )
    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-nodes",
        "-keyout",
        str(key_path),
        "-out",
        str(cert_path),
        "-days",
        "365",
        "-subj",
        f"/CN={HTTPS_SELF_SIGNED_HOST}",
        "-addext",
        f"subjectAltName={subject_alt_names}",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "OpenSSL is required to generate a self-signed certificate. "
            "Install it or set HTTPS_CERT_FILE and HTTPS_KEY_FILE to existing files."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(
            f"Failed to generate a self-signed certificate with OpenSSL: {details}"
        ) from exc
    return cert_path, key_path
