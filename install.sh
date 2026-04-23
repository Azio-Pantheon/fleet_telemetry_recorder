#!/usr/bin/env bash
# One-shot installer for fleet-telemetry-recorder on a printer's Pi.
#
# - creates a venv at ~/fleet-telemetry-recorder-venv
# - installs this package + its deps
# - drops the systemd unit with the current user's paths
# - enables + starts the service
#
# Re-run to upgrade after pulling a newer source tree.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
VENV="${HOME}/fleet-telemetry-recorder-venv"
UNIT_NAME="fleet-telemetry-recorder.service"
UNIT_SRC="${SRC_DIR}/systemd/${UNIT_NAME}"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"

echo "[install] creating venv at ${VENV}"
python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --upgrade pip
"${VENV}/bin/pip" install "${SRC_DIR}"

echo "[install] writing ${UNIT_DEST}"
sed -e "s|{USER}|${USER_NAME}|g" -e "s|{VENV}|${VENV}|g" "${UNIT_SRC}" \
    | sudo tee "${UNIT_DEST}" > /dev/null

echo "[install] enabling service"
sudo systemctl daemon-reload
sudo systemctl enable "${UNIT_NAME}"
sudo systemctl restart "${UNIT_NAME}"

echo "[install] done. tail the log with:"
echo "    journalctl -u ${UNIT_NAME} -f"
