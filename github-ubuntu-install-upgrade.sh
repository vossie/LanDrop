#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root."
  exit 1
fi

PORT_OVERRIDE=""

usage() {
  cat <<'EOF'
Usage: github-ubuntu-install-upgrade.sh [--port PORT]

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
CONFIG_DIR="${CONFIG_DIR:-/etc/dassiedrop}"
ENV_FILE="${ENV_FILE:-$CONFIG_DIR/dassiedrop.env}"
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
    apt-get update
    apt-get install -y "${package_name}"
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

require_command curl
require_command tar
require_command apt-get
ensure_package python3.11 python3.11
require_command "${PYTHON_BIN}"

ACTION="install"
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}\\.service"; then
  ACTION="upgrade"
elif [[ -f "${ENV_FILE}" ]]; then
  ACTION="upgrade"
fi

echo "Starting DassieDrop ${ACTION} from ${REPO_URL}..."

if [[ -n "${REPO_REF}" ]]; then
  download_archive "${REPO_REF}"
else
  if ! download_archive "main"; then
    download_archive "master"
  fi
fi

tar -xzf "${ARCHIVE_PATH}" -C "${TMP_DIR}"
SOURCE_DIR="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"

if [[ -z "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/install-ubuntu-service.sh" ]]; then
  echo "Downloaded archive does not contain install-ubuntu-service.sh."
  exit 1
fi

load_existing_config

if [[ -n "${PORT_OVERRIDE}" ]]; then
  export PORT="${PORT_OVERRIDE}"
fi

bash "${SOURCE_DIR}/install-ubuntu-service.sh"

echo
echo "DassieDrop ${ACTION} completed from ${REPO_URL}."
