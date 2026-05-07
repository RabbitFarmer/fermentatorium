#!/usr/bin/env bash
set -euo pipefail

# The app runs directly from the repository checkout — no copy to /opt is made.
# REPO_DIR is always the directory that contains this script.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_PATH="/etc/systemd/system/fermentatorium.service"

# Prefer venv/, but accept .venv/ if it already exists.
PREFERRED_VENV_NAME="venv"

# Run the service as the user who invoked sudo (i.e. the repo owner).
# Falls back to the current user if not running under sudo.
APP_USER="${SUDO_USER:-$(id -un)}"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: must run as root. Try: sudo $0"
    exit 1
  fi
}

install_os_deps() {
  apt-get update
  apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    bluetooth bluez \
    ca-certificates
}

ensure_bluetooth_access() {
  # Grant the repo user BLE access so the Tilt scanner can work.
  if getent group bluetooth >/dev/null; then
    usermod -aG bluetooth "${APP_USER}" || true
  else
    echo "WARN: bluetooth group not found; skipping usermod -aG bluetooth"
  fi
}

ensure_default_configs() {
  local cfg_dir="${REPO_DIR}/config"
  mkdir -p "${cfg_dir}"

  copy_if_missing() {
    local template="$1"
    local dest="$2"
    if [[ -f "${template}" && ! -f "${dest}" ]]; then
      cp -a "${template}" "${dest}"
    fi
  }

  copy_if_missing "${cfg_dir}/system_config.json.template"        "${cfg_dir}/system_config.json"
  copy_if_missing "${cfg_dir}/temp_control_config.json.template"  "${cfg_dir}/temp_control_config.json"
  copy_if_missing "${cfg_dir}/tilt_config.json.template"          "${cfg_dir}/tilt_config.json"
  copy_if_missing "${cfg_dir}/tilt_table.json.template"           "${cfg_dir}/tilt_table.json"
}

pick_venv_dir() {
  if [[ -d "${REPO_DIR}/${PREFERRED_VENV_NAME}" ]]; then
    echo "${REPO_DIR}/${PREFERRED_VENV_NAME}"
  elif [[ -d "${REPO_DIR}/.venv" ]]; then
    echo "${REPO_DIR}/.venv"
  else
    echo "${REPO_DIR}/${PREFERRED_VENV_NAME}"
  fi
}

ensure_venv_and_deps() {
  local venv_dir
  venv_dir="$(pick_venv_dir)"

  if [[ ! -x "${venv_dir}/bin/python3" ]]; then
    sudo -u "${APP_USER}" -H python3 -m venv "${venv_dir}"
  fi

  sudo -u "${APP_USER}" -H "${venv_dir}/bin/python3" -m pip install --upgrade pip

  if [[ -f "${REPO_DIR}/requirements.txt" ]]; then
    sudo -u "${APP_USER}" -H "${venv_dir}/bin/pip" install -r "${REPO_DIR}/requirements.txt"
  else
    echo "WARN: ${REPO_DIR}/requirements.txt not found; skipping pip install -r"
  fi
}

fix_repo_ownership() {
  # The automated installer clones the repository as root, but the service runs
  # as APP_USER.  Without this step, git pull (auto-update at startup and via the
  # web UI Update button) fails with:
  #   error: insufficient permission for adding an object to repository database
  # Chowning the entire repo directory ensures APP_USER can write objects,
  # update source files, and save config/log files at runtime.
  if [[ "${APP_USER}" == "root" ]]; then
    # Running directly as root (no sudo) — ownership is already correct.
    return
  fi
  echo "→ Setting ${REPO_DIR} ownership to ${APP_USER} …"
  chown -R "${APP_USER}:" "${REPO_DIR}"
  echo "  ✓ Ownership set"
}

install_systemd_service() {
  # Note: heredoc uses double-quotes so REPO_DIR and APP_USER are expanded here.
  cat > "${SERVICE_PATH}" <<UNIT
[Unit]
Description=Fermentatorium (Tilt Temperature Controller and Fermentation Monitor)
After=network-online.target network.target bluetooth.service
Wants=network-online.target bluetooth.service

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${REPO_DIR}

Environment="FLASK_PORT=5001"

ExecStart=${REPO_DIR}/run.sh

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
  ensure_bluetooth_access
  ensure_default_configs
  ensure_venv_and_deps
  install_systemd_service
  fix_repo_ownership

  echo ""
  echo "Running from: ${REPO_DIR}"
  echo "Service user: ${APP_USER}"
  echo "Service:"
  systemctl status fermentatorium.service --no-pager -l || true
  echo ""
  echo "Logs:"
  echo "  journalctl -u fermentatorium.service -n 200 --no-pager"
}

main "$@"