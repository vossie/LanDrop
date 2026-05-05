#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root."
  exit 1
fi

PORT_OVERRIDE=""
HTTPS_PORT_OVERRIDE=""

usage() {
  cat <<'EOF'
Usage: install-ubuntu-service.sh [--port PORT] [--https-port PORT]

Options:
  --port PORT        Set the DassieDrop HTTP listen port for this install.
  --https-port PORT  Set the DassieDrop HTTPS listen port for this install.
  --help        Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      if [[ $# -lt 2 ]]; then
        echo "--port requires a value."
        exit 1
      fi
      PORT_OVERRIDE="$2"
      shift 2
      ;;
    --https-port)
      if [[ $# -lt 2 ]]; then
        echo "--https-port requires a value."
        exit 1
      fi
      HTTPS_PORT_OVERRIDE="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

SERVICE_NAME="${SERVICE_NAME:-dassiedrop}"
SERVICE_USER="${SERVICE_USER:-dassiedrop}"
SERVICE_GROUP="${SERVICE_GROUP:-dassiedrop}"
APP_DIR="${APP_DIR:-/opt/dassiedrop}"
DATA_DIR="${DATA_DIR:-/var/lib/dassiedrop}"
CONFIG_DIR="${CONFIG_DIR:-/etc/dassiedrop}"
ENV_FILE="${ENV_FILE:-$CONFIG_DIR/dassiedrop.env}"
SYSTEMD_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.11}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOST_VALUE="${HOST:-0.0.0.0}"
HTTP_PORT_VALUE="${HTTP_PORT:-${PORT:-8000}}"
HTTPS_VALUE="${HTTPS:-}"
HTTPS_PORT_VALUE="${HTTPS_PORT:-8443}"
ACCESS_CODE_VALUE="${ACCESS_CODE:-change-me}"
SHARE_BASE_URL_VALUE="${SHARE_BASE_URL:-}"
APP_VERSION_VALUE="${APP_VERSION:-}"
CERT_DIR="${CERT_DIR:-${DATA_DIR}/certs}"
HTTPS_CERT_FILE_VALUE="${HTTPS_CERT_FILE:-${CERT_DIR}/dassiedrop-selfsigned.crt}"
HTTPS_KEY_FILE_VALUE="${HTTPS_KEY_FILE:-${CERT_DIR}/dassiedrop-selfsigned.key}"
HTTPS_SELF_SIGNED_HOST_VALUE="${HTTPS_SELF_SIGNED_HOST:-localhost}"
HTTPS_SELF_SIGNED_SANS_VALUE="${HTTPS_SELF_SIGNED_SANS:-}"

if [[ -n "${PORT_OVERRIDE}" ]]; then
  HTTP_PORT_VALUE="${PORT_OVERRIDE}"
fi

if [[ -n "${HTTPS_PORT_OVERRIDE}" ]]; then
  HTTPS_PORT_VALUE="${HTTPS_PORT_OVERRIDE}"
fi

echo "Installing ${SERVICE_NAME}..."

if ! command -v apt-get >/dev/null 2>&1; then
  echo "apt-get is required but not installed."
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required but not installed."
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3.11
fi

if [[ "${HTTPS_VALUE,,}" =~ ^(1|true|yes|on)$ ]] && ! command -v openssl >/dev/null 2>&1; then
  apt-get update
  apt-get install -y openssl
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "${PYTHON_BIN} is required but not installed."
  exit 1
fi

if ! getent group "${SERVICE_GROUP}" >/dev/null; then
  groupadd --system "${SERVICE_GROUP}"
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "${SERVICE_GROUP}" \
    --home-dir "${DATA_DIR}" \
    --create-home \
    --shell /usr/sbin/nologin \
    "${SERVICE_USER}"
fi

install -d -m 0755 "${APP_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${DATA_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${DATA_DIR}/uploads"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${CERT_DIR}"
install -d -m 0755 "${CONFIG_DIR}"

install -m 0644 "${SCRIPT_DIR}/app.py" "${APP_DIR}/app.py"
if [[ -f "${SCRIPT_DIR}/README.md" ]]; then
  install -m 0644 "${SCRIPT_DIR}/README.md" "${APP_DIR}/README.md"
fi
if [[ -f "${SCRIPT_DIR}/VERSION" ]]; then
  install -m 0644 "${SCRIPT_DIR}/VERSION" "${APP_DIR}/VERSION"
fi
if [[ -d "${SCRIPT_DIR}/assets" ]]; then
  rm -rf "${APP_DIR}/assets"
  install -d -m 0755 "${APP_DIR}/assets"
  cp -R "${SCRIPT_DIR}/assets/." "${APP_DIR}/assets/"
fi
if [[ -d "${SCRIPT_DIR}/templates" ]]; then
  rm -rf "${APP_DIR}/templates"
  install -d -m 0755 "${APP_DIR}/templates"
  cp -R "${SCRIPT_DIR}/templates/." "${APP_DIR}/templates/"
fi

chown root:root "${APP_DIR}/app.py"
if [[ -f "${APP_DIR}/README.md" ]]; then
  chown root:root "${APP_DIR}/README.md"
fi
if [[ -f "${APP_DIR}/VERSION" ]]; then
  chown root:root "${APP_DIR}/VERSION"
fi
if [[ -d "${APP_DIR}/assets" ]]; then
  chown -R root:root "${APP_DIR}/assets"
fi
if [[ -d "${APP_DIR}/templates" ]]; then
  chown -R root:root "${APP_DIR}/templates"
fi

cat > "${ENV_FILE}" <<EOF
HOST=${HOST_VALUE}
PORT=${HTTP_PORT_VALUE}
HTTP_PORT=${HTTP_PORT_VALUE}
HTTPS=${HTTPS_VALUE}
HTTPS_PORT=${HTTPS_PORT_VALUE}
HTTPS_CERT_FILE=${HTTPS_CERT_FILE_VALUE}
HTTPS_KEY_FILE=${HTTPS_KEY_FILE_VALUE}
HTTPS_SELF_SIGNED_HOST=${HTTPS_SELF_SIGNED_HOST_VALUE}
HTTPS_SELF_SIGNED_SANS=${HTTPS_SELF_SIGNED_SANS_VALUE}
UPLOAD_DIR=${DATA_DIR}/uploads
ACCESS_CODE=${ACCESS_CODE_VALUE}
SHARE_BASE_URL=${SHARE_BASE_URL_VALUE}
APP_VERSION=${APP_VERSION_VALUE}
EOF
chmod 0640 "${ENV_FILE}"
chown root:"${SERVICE_GROUP}" "${ENV_FILE}"

cat > "${SYSTEMD_UNIT}" <<EOF
[Unit]
Description=DassieDrop web app
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${PYTHON_BIN} ${APP_DIR}/app.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  systemctl enable "${SERVICE_NAME}.service"
  systemctl restart "${SERVICE_NAME}.service"
else
  systemctl enable --now "${SERVICE_NAME}.service"
fi

echo
echo "Installed ${SERVICE_NAME}."
echo "Service status:"
systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
echo
echo "Config file: ${ENV_FILE}"
echo "Code dir: ${APP_DIR}"
echo "Data dir: ${DATA_DIR}"
echo "Certificate dir: ${CERT_DIR}"
echo
echo "To change the access code, HTTP port, or HTTPS settings:"
echo "  sudoedit ${ENV_FILE}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
