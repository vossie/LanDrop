#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root."
  exit 1
fi

REPO_URL="${REPO_URL:-https://github.com/vossie/LanDrop}"
REPO_OWNER="${REPO_OWNER:-vossie}"
REPO_NAME="${REPO_NAME:-LanDrop}"
REPO_REF="${REPO_REF:-}"
SERVICE_NAME="${SERVICE_NAME:-landrop}"
CONFIG_DIR="${CONFIG_DIR:-/etc/landrop}"
ENV_FILE="${ENV_FILE:-$CONFIG_DIR/landrop.env}"
TMP_DIR="$(mktemp -d)"
ARCHIVE_PATH="${TMP_DIR}/landrop.tar.gz"

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

ACTION="install"
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}\\.service"; then
  ACTION="upgrade"
elif [[ -f "${ENV_FILE}" ]]; then
  ACTION="upgrade"
fi

echo "Starting LanDrop ${ACTION} from ${REPO_URL}..."

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

bash "${SOURCE_DIR}/install-ubuntu-service.sh"

echo
echo "LanDrop ${ACTION} completed from ${REPO_URL}."
