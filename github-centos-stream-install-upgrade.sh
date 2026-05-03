#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root."
  exit 1
fi

PORT_OVERRIDE=""

usage() {
  cat <<'EOF'
Usage: github-centos-stream-install-upgrade.sh [--port PORT]

Options:
  --port PORT   Set the DassieDrop listen port for install or upgrade.
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

REPO_URL="${REPO_URL:-https://github.com/vossie/DassieDrop}"
REPO_OWNER="${REPO_OWNER:-vossie}"
REPO_NAME="${REPO_NAME:-DassieDrop}"
REPO_REF="${REPO_REF:-}"
SERVICE_NAME="${SERVICE_NAME:-dassiedrop}"
SERVICE_USER="${SERVICE_USER:-dassiedrop}"
SERVICE_GROUP="${SERVICE_GROUP:-dassiedrop}"
APP_DIR="${APP_DIR:-/opt/dassiedrop}"
DATA_DIR="${DATA_DIR:-/var/lib/dassiedrop}"
CONFIG_DIR="${CONFIG_DIR:-/etc/dassiedrop}"
ENV_FILE="${ENV_FILE:-$CONFIG_DIR/dassiedrop.env}"
SYSTEMD_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.11}"
TMP_DIR="$(mktemp -d)"
ARCHIVE_PATH="${TMP_DIR}/dassiedrop.tar.gz"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "${name} is required but not installed."
    exit 1
  fi
}

ensure_package() {
  local binary="$1"
  local package_name="$2"
  if ! command -v "${binary}" >/dev/null 2>&1; then
    dnf -y install "${package_name}"
  fi
}

download_archive() {
  local ref="$1"
  local url="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${ref}.tar.gz"
  curl -fsSL "${url}" -o "${ARCHIVE_PATH}"
}

load_existing_config() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    return
  fi

  while IFS='=' read -r key value; do
    [[ -n "${key}" ]] || continue
    [[ "${key}" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    if [[ -z "${!key+x}" ]]; then
      export "${key}=${value}"
    fi
  done < "${ENV_FILE}"
}

ensure_package curl curl
ensure_package tar tar
ensure_package python3.11 python3.11
require_command dnf
require_command systemctl
require_command "${PYTHON_BIN}"

ACTION="install"
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}\\.service"; then
  ACTION="upgrade"
elif [[ -f "${ENV_FILE}" ]]; then
  ACTION="upgrade"
fi

echo "Starting DassieDrop ${ACTION} for CentOS Stream from ${REPO_URL}..."

if [[ -n "${REPO_REF}" ]]; then
  download_archive "${REPO_REF}"
else
  if ! download_archive "main"; then
    download_archive "master"
  fi
fi

tar -xzf "${ARCHIVE_PATH}" -C "${TMP_DIR}"
SOURCE_DIR="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"

if [[ -z "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/app.py" ]]; then
  echo "Downloaded archive does not contain app.py."
  exit 1
fi

load_existing_config

HOST_VALUE="${HOST:-0.0.0.0}"
PORT_VALUE="${PORT:-8000}"
ACCESS_CODE_VALUE="${ACCESS_CODE:-change-me}"
SHARE_BASE_URL_VALUE="${SHARE_BASE_URL:-}"
APP_VERSION_VALUE="${APP_VERSION:-}"

if [[ -n "${PORT_OVERRIDE}" ]]; then
  PORT_VALUE="${PORT_OVERRIDE}"
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
    --shell /sbin/nologin \
    "${SERVICE_USER}"
fi

install -d -m 0755 "${APP_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${DATA_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${DATA_DIR}/uploads"
install -d -m 0755 "${CONFIG_DIR}"

install -m 0644 "${SOURCE_DIR}/app.py" "${APP_DIR}/app.py"
if [[ -f "${SOURCE_DIR}/README.md" ]]; then
  install -m 0644 "${SOURCE_DIR}/README.md" "${APP_DIR}/README.md"
fi
if [[ -f "${SOURCE_DIR}/VERSION" ]]; then
  install -m 0644 "${SOURCE_DIR}/VERSION" "${APP_DIR}/VERSION"
fi
if [[ -d "${SOURCE_DIR}/assets" ]]; then
  rm -rf "${APP_DIR}/assets"
  install -d -m 0755 "${APP_DIR}/assets"
  cp -R "${SOURCE_DIR}/assets/." "${APP_DIR}/assets/"
fi
if [[ -d "${SOURCE_DIR}/templates" ]]; then
  rm -rf "${APP_DIR}/templates"
  install -d -m 0755 "${APP_DIR}/templates"
  cp -R "${SOURCE_DIR}/templates/." "${APP_DIR}/templates/"
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
systemctl enable --now "${SERVICE_NAME}.service"

echo
echo "DassieDrop ${ACTION} completed from ${REPO_URL}."
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
