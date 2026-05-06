#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root."
  exit 1
fi

PORT_OVERRIDE=""
HTTPS_PORT_OVERRIDE=""
SILENT_MODE=0

usage() {
  cat <<'EOF'
Usage: install-ubuntu-service.sh [--port PORT] [--https-port PORT] [--silent]

Options:
  --port PORT        Set the DassieDrop HTTP listen port for this install.
  --https-port PORT  Set the DassieDrop HTTPS listen port for this install.
  --silent           Generate missing ACCESS_CODE and API_KEY values automatically.
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
    --silent)
      SILENT_MODE=1
      shift
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
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

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

is_missing_secret() {
  local value="${1:-}"
  local trimmed="${value#"${value%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  [[ -z "${trimmed}" || "${trimmed,,}" == "null" ]]
}

is_missing_config_value() {
  local value="${1:-}"
  local trimmed="${value#"${value%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  [[ -z "${trimmed}" || "${trimmed,,}" == "null" ]]
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
    return
  fi
  head -c 32 /dev/urandom | sha256sum | awk '{print substr($1, 1, 32)}'
}

GENERATED_ACCESS_CODE=""
GENERATED_API_KEY=""

resolve_secret() {
  local var_name="$1"
  local prompt_label="$2"
  local current_value="$3"

  if ! is_missing_secret "${current_value}"; then
    printf '%s' "${current_value}"
    return
  fi

  if [[ "${SILENT_MODE}" == "1" ]]; then
    local generated
    generated="$(generate_secret)"
    if [[ "${var_name}" == "ACCESS_CODE_VALUE" ]]; then
      GENERATED_ACCESS_CODE="${generated}"
    else
      GENERATED_API_KEY="${generated}"
    fi
    printf '%s' "${generated}"
    return
  fi

  if [[ ! -r /dev/tty ]]; then
    echo "${prompt_label} is missing. Set it in the environment/config or rerun with --silent." >&2
    exit 1
  fi

  local entered=""
  read -r -p "Enter ${prompt_label} (leave blank to auto-generate): " entered </dev/tty
  if is_missing_secret "${entered}"; then
    entered="$(generate_secret)"
    if [[ "${var_name}" == "ACCESS_CODE_VALUE" ]]; then
      GENERATED_ACCESS_CODE="${entered}"
    else
      GENERATED_API_KEY="${entered}"
    fi
  fi
  printf '%s' "${entered}"
}

resolve_update_check_enabled() {
  local current_value="$1"
  if ! is_missing_config_value "${current_value}"; then
    local lowered="${current_value,,}"
    if [[ "${lowered}" =~ ^(1|true|yes|on)$ ]]; then
      printf '1'
    else
      printf '0'
    fi
    return
  fi

  if [[ "${SILENT_MODE}" == "1" ]]; then
    printf '0'
    return
  fi

  if [[ ! -r /dev/tty ]]; then
    echo "UPDATE_CHECK_ENABLED is missing. Rerun interactively or use --silent to keep it off." >&2
    exit 1
  fi

  local answer=""
  read -r -p "Enable daily update checks? [y/N]: " answer </dev/tty
  if [[ "${answer,,}" =~ ^(y|yes)$ ]]; then
    printf '1'
  else
    printf '0'
  fi
}

load_existing_config

HOST_VALUE="${HOST:-0.0.0.0}"
HTTP_PORT_VALUE="${HTTP_PORT:-${PORT:-8000}}"
HTTPS_VALUE="${HTTPS:-1}"
HTTPS_PORT_VALUE="${HTTPS_PORT:-8443}"
ACCESS_CODE_VALUE="${ACCESS_CODE:-}"
API_KEY_VALUE="${API_KEY:-}"
SHARE_BASE_URL_VALUE="${SHARE_BASE_URL:-}"
APP_VERSION_VALUE="${APP_VERSION:-}"
UPDATE_CHECK_ENABLED_VALUE="${UPDATE_CHECK_ENABLED:-}"
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

ACCESS_CODE_VALUE="$(resolve_secret "ACCESS_CODE_VALUE" "ACCESS_CODE" "${ACCESS_CODE_VALUE}")"
API_KEY_VALUE="$(resolve_secret "API_KEY_VALUE" "API_KEY" "${API_KEY_VALUE}")"
UPDATE_CHECK_ENABLED_VALUE="$(resolve_update_check_enabled "${UPDATE_CHECK_ENABLED_VALUE}")"

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

install -m 0644 "${REPO_DIR}/app.py" "${APP_DIR}/app.py"
if [[ -f "${REPO_DIR}/README.md" ]]; then
  install -m 0644 "${REPO_DIR}/README.md" "${APP_DIR}/README.md"
fi
if [[ -f "${REPO_DIR}/VERSION" ]]; then
  install -m 0644 "${REPO_DIR}/VERSION" "${APP_DIR}/VERSION"
fi
if [[ -d "${REPO_DIR}/dassiedrop" ]]; then
  rm -rf "${APP_DIR}/dassiedrop"
  install -d -m 0755 "${APP_DIR}/dassiedrop"
  cp -R "${REPO_DIR}/dassiedrop/." "${APP_DIR}/dassiedrop/"
fi
if [[ -d "${REPO_DIR}/assets" ]]; then
  rm -rf "${APP_DIR}/assets"
  install -d -m 0755 "${APP_DIR}/assets"
  cp -R "${REPO_DIR}/assets/." "${APP_DIR}/assets/"
fi
if [[ -d "${REPO_DIR}/templates" ]]; then
  rm -rf "${APP_DIR}/templates"
  install -d -m 0755 "${APP_DIR}/templates"
  cp -R "${REPO_DIR}/templates/." "${APP_DIR}/templates/"
fi

chown root:root "${APP_DIR}/app.py"
if [[ -f "${APP_DIR}/README.md" ]]; then
  chown root:root "${APP_DIR}/README.md"
fi
if [[ -f "${APP_DIR}/VERSION" ]]; then
  chown root:root "${APP_DIR}/VERSION"
fi
if [[ -d "${APP_DIR}/dassiedrop" ]]; then
  chown -R root:root "${APP_DIR}/dassiedrop"
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
API_KEY=${API_KEY_VALUE}
SHARE_BASE_URL=${SHARE_BASE_URL_VALUE}
APP_VERSION=${APP_VERSION_VALUE}
UPDATE_CHECK_ENABLED=${UPDATE_CHECK_ENABLED_VALUE}
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
if [[ -n "${GENERATED_ACCESS_CODE}" ]]; then
  echo
  echo "Generated ACCESS_CODE: ${GENERATED_ACCESS_CODE}"
fi
if [[ -n "${GENERATED_API_KEY}" ]]; then
  echo "Generated API_KEY: ${GENERATED_API_KEY}"
fi
