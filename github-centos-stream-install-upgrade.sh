#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="${REPO_OWNER:-vossie}"
REPO_NAME="${REPO_NAME:-DassieDrop}"
TMP_SCRIPT="$(mktemp)"

cleanup() {
  rm -f "${TMP_SCRIPT}"
}
trap cleanup EXIT

fetch_script() {
  local ref="$1"
  curl -fsSL "https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${ref}/scripts/github-centos-stream-install-upgrade.sh" -o "${TMP_SCRIPT}"
}

if ! fetch_script "master"; then
  fetch_script "main"
fi

exec bash "${TMP_SCRIPT}" "$@"
