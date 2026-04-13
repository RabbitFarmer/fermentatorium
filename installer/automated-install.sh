#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/RabbitFarmer/fermentatorium.git"
WORKDIR="/tmp/fermentatorium-installer"
REPO_DIR="${WORKDIR}/fermentatorium"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: must run as root."
    echo "Try:"
    echo "  curl -sSL https://raw.githubusercontent.com/RabbitFarmer/fermentatorium/main/installer/automated-install.sh | sudo bash"
    exit 1
  fi
}

install_bootstrap_deps() {
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates \
    git
}

fresh_clone() {
  rm -rf "${WORKDIR}"
  mkdir -p "${WORKDIR}"
  git clone --depth 1 "${REPO_URL}" "${REPO_DIR}"
  # If running via `sudo`, the checkout is owned by root but install.sh will
  # create the venv as SUDO_USER (the non-root invoking user).  Fix ownership
  # now so that user can write into the repo directory.
  if [[ -n "${SUDO_USER:-}" ]]; then
    chown -R "${SUDO_USER}:${SUDO_USER}" "${WORKDIR}"
  fi
}

run_install() {
  cd "${REPO_DIR}"
  chmod +x ./install.sh
  ./install.sh
}

main() {
  require_root
  install_bootstrap_deps
  fresh_clone
  run_install

  echo ""
  echo "Fermentatorium installed."
  echo "Status: systemctl status fermentatorium.service --no-pager -l"
  echo "Logs:   journalctl -u fermentatorium.service -n 200 --no-pager"
  echo "Open:   http://<raspberry-pi-ip>:5001"
}

main "$@"