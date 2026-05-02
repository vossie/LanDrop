#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root."
  exit 1
fi

SERVICE_NAME="${SERVICE_NAME:-landrop}"
SERVICE_USER="${SERVICE_USER:-landrop}"
SERVICE_GROUP="${SERVICE_GROUP:-landrop}"
APP_DIR="${APP_DIR:-/opt/landrop}"
DATA_DIR="${DATA_DIR:-/var/lib/landrop}"
CONFIG_DIR="${CONFIG_DIR:-/etc/landrop}"
ENV_FILE="${ENV_FILE:-$CONFIG_DIR/landrop.env}"
SYSTEMD_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOST_VALUE="${HOST:-0.0.0.0}"
PORT_VALUE="${PORT:-8000}"
ACCESS_CODE_VALUE="${ACCESS_CODE:-change-me}"
SHARE_BASE_URL_VALUE="${SHARE_BASE_URL:-}"
APP_VERSION_VALUE="${APP_VERSION:-}"

echo "Installing ${SERVICE_NAME}..."

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not installed."
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required but not installed."
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
PORT=${PORT_VALUE}
UPLOAD_DIR=${DATA_DIR}/uploads
ACCESS_CODE=${ACCESS_CODE_VALUE}
SHARE_BASE_URL=${SHARE_BASE_URL_VALUE}
APP_VERSION=${APP_VERSION_VALUE}
EOF
chmod 0640 "${ENV_FILE}"
chown root:"${SERVICE_GROUP}" "${ENV_FILE}"

cat > "${SYSTEMD_UNIT}" <<EOF
[Unit]
Description=LanDrop web app
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 ${APP_DIR}/app.py
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
systemctl enable --now "${SERVICE_NAME}.service"

echo
echo "Installed ${SERVICE_NAME}."
echo "Service status:"
systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
echo
echo "Config file: ${ENV_FILE}"
echo "Code dir: ${APP_DIR}"
echo "Data dir: ${DATA_DIR}"
echo
echo "To change the access code or port:"
echo "  sudoedit ${ENV_FILE}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
