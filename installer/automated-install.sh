#!/usr/bin/env bash
# automated-install.sh — One-command installer for Fermentatorium
#
# Designed to be piped directly from GitHub:
#   curl -sSL https://raw.githubusercontent.com/RabbitFarmer/fermentatorium/main/installer/automated-install.sh | sudo bash
#
# What this script does:
#   1. Installs OS dependencies (python3-venv, bluetooth, bluez, ca-certificates)
#   2. Clones the repository into /opt/fermentatorium using git
#      (preserving an existing install if re-run)
#   3. Delegates to /opt/fermentatorium/install.sh for:
#      - Python virtual-environment creation and pip install
#      - systemd service creation and activation
#
# Because the install uses git clone, the Update button in the web UI works
# immediately — no manual git init needed.
#
# Re-running is safe: existing config files and batch data are never overwritten.

set -euo pipefail

REMOTE_URL="https://github.com/RabbitFarmer/fermentatorium.git"
OPT_DIR="/opt/fermentatorium"

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        echo "ERROR: This script must be run as root."
        echo "Try:  curl -sSL <url> | sudo bash"
        exit 1
    fi
}

install_os_deps() {
    echo "→ Installing OS dependencies …"
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        git \
        python3 python3-venv python3-pip \
        bluetooth bluez \
        ca-certificates
    echo "  ✓ OS dependencies installed"
}

clone_or_update_repo() {
    if [[ -d "${OPT_DIR}/.git" ]]; then
        echo "→ Repository already exists at ${OPT_DIR} — pulling latest …"
        git -C "${OPT_DIR}" pull
        echo "  ✓ Repository updated"
    elif [[ -d "${OPT_DIR}" ]]; then
        # Directory exists but has no .git (e.g. a previous deploy_to_opt run).
        # Initialise git in-place so the existing files and user data are kept.
        echo "→ ${OPT_DIR} exists without a git repo — initialising git in-place …"
        git -C "${OPT_DIR}" init
        git -C "${OPT_DIR}" remote add origin "${REMOTE_URL}"
        git -C "${OPT_DIR}" fetch origin
        git -C "${OPT_DIR}" reset --hard origin/main
        echo "  ✓ git initialised and reset to latest code"
    else
        echo "→ Cloning repository into ${OPT_DIR} …"
        git clone "${REMOTE_URL}" "${OPT_DIR}"
        echo "  ✓ Repository cloned"
    fi
}

run_install_sh() {
    local install_script="${OPT_DIR}/install.sh"
    if [[ ! -f "${install_script}" ]]; then
        echo "ERROR: ${install_script} not found after clone. Aborting."
        exit 1
    fi
    echo "→ Running ${install_script} …"
    bash "${install_script}"
}

main() {
    require_root

    echo ""
    echo "=== Fermentatorium — Automated Installer ==="
    echo ""

    install_os_deps
    clone_or_update_repo
    run_install_sh

    echo ""
    echo "=== Installation complete ==="
    echo ""
    echo "Open the dashboard at:  http://<raspberry-pi-ip>:5001"
    echo "See ${OPT_DIR}/QUICK_START.md for first-run configuration."
    echo ""
}

main "$@"
