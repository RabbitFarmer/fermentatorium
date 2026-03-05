#!/usr/bin/env bash
set -euo pipefail

APP_USER="fermentatorium"
APP_GROUP="fermentatorium"

INSTALL_DIR="/opt/fermentatorium"
SERVICE_PATH="/etc/systemd/system/fermentatorium.service"

# Prefer venv/, but accept .venv/ if it already exists.
PREFERRED_VENV_NAME="venv"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: must run as root. Try: sudo $0"
    exit 1
  fi
}

install_os_deps() {
  apt-get update
  apt-get install -y --no-install-recommends \
    rsync \
    python3 python3-venv python3-pip \
    bluetooth bluez \
    ca-certificates
}

ensure_user_group() {
  groupadd --system "${APP_GROUP}" 2>/dev/null || true

  if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    useradd --system \
      --gid "${APP_GROUP}" \
      --create-home --home-dir "/home/${APP_USER}" \
      --shell /usr/sbin/nologin \
      "${APP_USER}"
  fi

  # BLE access (Tilt/BT)
  if getent group bluetooth >/dev/null; then
    usermod -aG bluetooth "${APP_USER}" || true
  else
    echo "WARN: bluetooth group not found; skipping usermod -aG bluetooth"
  fi
}

sync_app_files() {
  # Expect installer is run from within the repo checkout
  local src_dir
  src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  mkdir -p "${INSTALL_DIR}"

  # Copy everything except local venvs/caches/etc.
  rsync -a --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude 'venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    "${src_dir}/" "${INSTALL_DIR}/"

  chown -R "${APP_USER}:${APP_GROUP}" "${INSTALL_DIR}"
}

pick_venv_dir() {
  if [[ -d "${INSTALL_DIR}/${PREFERRED_VENV_NAME}" ]]; then
    echo "${INSTALL_DIR}/${PREFERRED_VENV_NAME}"
  elif [[ -d "${INSTALL_DIR}/.venv" ]]; then
    echo "${INSTALL_DIR}/.venv"
  else
    echo "${INSTALL_DIR}/${PREFERRED_VENV_NAME}"
  fi
}

ensure_venv_and_deps() {
  local venv_dir
  venv_dir="$(pick_venv_dir)"

  if [[ ! -x "${venv_dir}/bin/python3" ]]; then
    sudo -u "${APP_USER}" -H python3 -m venv "${venv_dir}"
  fi

  sudo -u "${APP_USER}" -H "${venv_dir}/bin/python3" -m pip install --upgrade pip

  if [[ -f "${INSTALL_DIR}/requirements.txt" ]]; then
    sudo -u "${APP_USER}" -H "${venv_dir}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
  else
    echo "WARN: ${INSTALL_DIR}/requirements.txt not found; skipping pip install -r"
  fi
}

install_systemd_service() {
  cat > "${SERVICE_PATH}" <<'UNIT'
[Unit]
Description=Fermentatorium (Tilt Temperature Controller and Fermentation Monitor)
After=network.target bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
User=fermentatorium
Group=fermentatorium
WorkingDirectory=/opt/fermentatorium

Environment="FLASK_PORT=5001"
Environment="FLASK_DEBUG=0"

ExecStart=/opt/fermentatorium/run.sh

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

  systemctl daemon-reload
  systemctl enable --now fermentatorium.service
}

main() {
  require_root
  install_os_deps
  ensure_user_group
  sync_app_files
  ensure_venv_and_deps
  install_systemd_service

  echo ""
  echo "Installed to: ${INSTALL_DIR}"
  echo "Service:"
  systemctl status fermentatorium.service --no-pager -l || true
  echo ""
  echo "Logs:"
  echo "  journalctl -u fermentatorium.service -n 200 --no-pager"
}

main "$@"