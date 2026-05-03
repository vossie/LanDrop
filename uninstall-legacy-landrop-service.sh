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
REMOVE_DATA="${REMOVE_DATA:-0}"
REMOVE_USER="${REMOVE_USER:-0}"

echo "Uninstalling legacy ${SERVICE_NAME}..."

if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files | grep -q "^${SERVICE_NAME}\\.service"; then
    systemctl disable --now "${SERVICE_NAME}.service" || true
  fi
fi

rm -f "${SYSTEMD_UNIT}"

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl reset-failed || true
fi

rm -f "${ENV_FILE}"
rmdir --ignore-fail-on-non-empty "${CONFIG_DIR}" 2>/dev/null || true
rm -rf "${APP_DIR}"

if [[ "${REMOVE_DATA}" == "1" ]]; then
  rm -rf "${DATA_DIR}"
else
  echo "Keeping data directory: ${DATA_DIR}"
fi

if [[ "${REMOVE_USER}" == "1" ]]; then
  if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    userdel "${SERVICE_USER}" || true
  fi

  if getent group "${SERVICE_GROUP}" >/dev/null; then
    groupdel "${SERVICE_GROUP}" || true
  fi
else
  echo "Keeping service user/group: ${SERVICE_USER}:${SERVICE_GROUP}"
fi

echo
echo "Legacy uninstall complete."
if [[ "${REMOVE_DATA}" != "1" ]]; then
  echo "Data still present at ${DATA_DIR}"
fi
