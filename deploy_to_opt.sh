#!/usr/bin/env bash
# deploy_to_opt.sh — Deploy a clean public copy of Fermentatorium to /opt/fermentatorium
#
# This script creates an outside-user copy of the application in /opt/fermentatorium:
#   - Copies all runtime application files (Python, HTML, static assets, scripts)
#   - Omits developer-only debugging documentation
#   - Removes personal data from configuration templates
#   - Rewrites end-user documentation (README, QUICK_START) for outside users
#   - Fixes any stale/broken paths and links inherited from the developer copy
#   - Sets appropriate file permissions
#
# Usage:
#   sudo bash /path/to/fermentatorium/deploy_to_opt.sh
#
# Run from the repository root, or provide REPO_DIR explicitly:
#   REPO_DIR=/path/to/fermentatorium sudo bash deploy_to_opt.sh
#
# Re-running is safe — existing config and data files are preserved.
#
# ─── Additional steps after running this script ───────────────────────────────
# 1. Mark the GitHub repository public (you already know this one).
# 2. Install the systemd service so the app starts at boot:
#      sudo /opt/fermentatorium/install.sh
#    This will:
#      • Install OS dependencies (python3-venv, bluetooth, etc.)
#      • Create a Python virtual environment inside /opt/fermentatorium/
#      • Install Python dependencies from requirements.txt
#      • Create and enable fermentatorium.service (runs on port 5001)
# 3. Add your user to the bluetooth group (done by install.sh, but requires
#    logging out and back in to take effect):
#      sudo usermod -aG bluetooth "$USER"  && newgrp bluetooth
# 4. Open the web dashboard and complete first-run configuration:
#      http://<raspberry-pi-ip>:5001
#    Configure: brewery name, Kasa plug IP addresses, Tilt hydrometer colors,
#    SMTP / Pushover / ntfy credentials (if you want notifications).
# 5. Optional — desktop autostart (if the Pi has a monitor):
#      bash /opt/fermentatorium/install_desktop_autostart.sh
# 6. Optional — set up remote access via Raspberry Pi Connect:
#      sudo apt install rpi-connect && rpi-connect signin
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Resolve paths ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-${SCRIPT_DIR}}"
OPT_DIR="/opt/fermentatorium"

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        echo "ERROR: This script must be run as root."
        echo "Try:  sudo bash ${BASH_SOURCE[0]}"
        exit 1
    fi
}

# ── Step 1: Sync application files ────────────────────────────────────────────
sync_files() {
    echo "→ Syncing application files to ${OPT_DIR} …"
    mkdir -p "${OPT_DIR}"

    # rsync: copy everything except developer artefacts and runtime data.
    # --delete removes stale files if the deploy is re-run.
    rsync -a --delete \
        --exclude='.git/' \
        --exclude='.venv/' \
        --exclude='venv/' \
        --exclude='env/' \
        --exclude='logs/' \
        --exclude='batches/' \
        --exclude='export/' \
        --exclude='temp_control/' \
        --exclude='*.pyc' \
        --exclude='__pycache__/' \
        --exclude='*.log' \
        --exclude='*.db' \
        --exclude='*.sqlite' \
        --exclude='*.bak' \
        --exclude='SUMMARY.md' \
        --exclude='TRACING_README.md' \
        --exclude='TRACING_GUIDE.md' \
        "${REPO_DIR}/" "${OPT_DIR}/"

    echo "  ✓ Files synced"
}

# ── Step 2: Remove developer-only files ───────────────────────────────────────
remove_dev_files() {
    echo "→ Removing developer-only files …"

    # These three files document an internal debugging session and are not
    # relevant or useful to outside users.
    for f in SUMMARY.md TRACING_README.md TRACING_GUIDE.md; do
        rm -f "${OPT_DIR}/${f}"
    done

    # deploy_to_opt.sh itself is a developer tool — outside users don't need it.
    rm -f "${OPT_DIR}/deploy_to_opt.sh"

    echo "  ✓ Developer-only files removed"
}

# ── Step 3: Remove personal data from config templates ────────────────────────
clean_config_templates() {
    echo "→ Cleaning config templates …"

    # system_config.json.template shipped with "ThreeControl" as the brewery
    # name — replace it with a neutral placeholder so new users are prompted to
    # enter their own name rather than inheriting someone else's.
    local tmpl="${OPT_DIR}/config/system_config.json.template"
    if [[ -f "${tmpl}" ]]; then
        sed -i 's/"brewery_name": *"ThreeControl"/"brewery_name": "My Brewery"/' "${tmpl}"
        echo "  ✓ system_config.json.template: brewery name cleared"
    fi
}

# ── Step 4: Clean utils/README.md ─────────────────────────────────────────────
clean_utils_readme() {
    echo "→ Cleaning utils/README.md …"
    local f="${OPT_DIR}/utils/README.md"
    [[ -f "${f}" ]] || return 0

    # The README contains the demo data from the developer's own brew session
    # (specific beer name, brew ID, dates, gravity figures).  Replace those
    # lines with generic placeholders so no personal data leaks out.
    python3 - "${f}" <<'PYEOF'
import re, sys

path = sys.argv[1]
with open(path) as fh:
    text = fh.read()

# Replace the personal "Overview" bullet block with a generic description.
text = re.sub(
    r'The demo data has been imported.*?- \*\*Estimated ABV\*\*.*?\n',
    (
        'The import utilities can create a demo fermentation dataset '
        'so you can explore the charting and history features before '
        'connecting live Tilt hardware.\n'
    ),
    text,
    flags=re.DOTALL,
)

# Remove or anonymise any remaining personal identifiers.
text = re.sub(r'803 Blonde Ale Clone of 805', 'Sample Beer', text)
# Note: 'Demo Batch' is intentionally preserved — it is generic and suitable.
text = re.sub(r'cf38d0a8', '<brew-id>', text)
text = re.sub(r'Dec \d+, 2025[^)]*Jan \d+, 2026', 'sample date range', text)
text = re.sub(r'~15 days', 'sample duration', text)
text = re.sub(r'1\.049', '1.XXX', text)
text = re.sub(r'1\.004', '1.00X', text)
text = re.sub(r'5\.9%', 'X.X%', text)

with open(path, 'w') as fh:
    fh.write(text)

print('  ✓ utils/README.md: personal beer data removed')
PYEOF
}

# ── Step 4b: Clean utils/setup_demo.sh ────────────────────────────────────────
clean_setup_demo() {
    echo "→ Cleaning utils/setup_demo.sh …"
    local f="${OPT_DIR}/utils/setup_demo.sh"
    [[ -f "${f}" ]] || return 0

    # Replace the developer's personal beer name and brew ID with generic
    # demo placeholders, and fix stale references to the old project name.
    python3 - "${f}" <<'PYEOF'
import sys

path = sys.argv[1]
with open(path) as fh:
    text = fh.read()

# Fix old project name references.
text = text.replace('Three Control - Demo Setup', 'Fermentatorium - Demo Setup')
text = text.replace('"threecontrol root directory"', '"fermentatorium root directory"')
text = text.replace('app3.py', 'app.py')

# Replace personal beer data with generic demo values.
text = text.replace("'803 Blonde Ale Clone of 805'", "'Demo Pale Ale'")
text = text.replace("'12/25/2025'", "'01/01/2025'")
text = text.replace("'cf38d0a8'", "'demo0001'")
text = text.replace("'2025-12-25T14:27:59Z'", "'2025-01-01T12:00:00Z'")

with open(path, 'w') as fh:
    fh.write(text)

print('  ✓ utils/setup_demo.sh: personal data replaced with generic demo values')
PYEOF
}

# ── Step 5: Rewrite README.md for outside users ───────────────────────────────
fix_readme() {
    echo "→ Updating README.md for outside users …"
    local f="${OPT_DIR}/README.md"
    [[ -f "${f}" ]] || return 0

    python3 - "${f}" <<'PYEOF'
import re, sys

path = sys.argv[1]
with open(path) as fh:
    text = fh.read()

# 1. Remove broken link to NOTIFICATIONS.md (file does not exist in the repo).
text = re.sub(r'\s*- See \[NOTIFICATIONS\.md\]\(NOTIFICATIONS\.md\)[^\n]*\n', '\n', text)

# 2. The "Option 2: Systemd Service" section contains stale references to the
#    old project name ("threecontrol-") and a non-existent install_service.sh.
#    Replace the whole option block with the correct install.sh invocation.
#    Use #{2,} so we don't stop at # comments inside code blocks (which use
#    single `# ` for inline comments).
old_opt2 = re.compile(
    r'^#### Option 2: Systemd Service.*?(?=^#{2,} )',
    re.DOTALL | re.MULTILINE,
)
new_opt2 = (
    "#### Option 2: Systemd Service (Recommended for headless setups)\n\n"
    "For headless setups or if you prefer the application to run as a "
    "background service:\n\n"
    "```bash\n"
    "# Run the automated service installer\n"
    "sudo /opt/fermentatorium/install.sh\n"
    "```\n\n"
    "The installer will:\n"
    "- ✓ Install OS dependencies (python3-venv, bluetooth, bluez, etc.)\n"
    "- ✓ Add your user to the `bluetooth` group for BLE (Tilt) access\n"
    "- ✓ Create a Python virtual environment and install Python dependencies\n"
    "- ✓ Create and enable `fermentatorium.service` (runs at boot, no browser)\n\n"
    "Manage the service:\n\n"
    "```bash\n"
    "sudo systemctl status fermentatorium.service --no-pager\n"
    "sudo systemctl restart fermentatorium.service\n"
    "journalctl -u fermentatorium.service -n 200 --no-pager\n"
    "```\n\n"
)
text = old_opt2.sub(new_opt2, text, count=1)

# 3. Fix the Troubleshooting section that links to non-existent INSTALLATION.md.
#    Use #{2,} so we don't stop at # comments inside code blocks.
old_trouble = re.compile(
    r'^### Troubleshooting Installation\n\n'
    r'If you encounter errors during installation.*?'
    r'(?=^#{2,} |\Z)',
    re.DOTALL | re.MULTILINE,
)
new_trouble = (
    "### Troubleshooting Installation\n\n"
    "Common issues and solutions:\n\n"
    "- **PEP 668 \"externally-managed-environment\" errors** — "
    "run `sudo apt install python3-venv` then re-run `install.sh`.\n"
    "- **Bluetooth/BLE not working** — ensure you are in the `bluetooth` group "
    "(`sudo usermod -aG bluetooth $USER`) and have logged out and back in.\n"
    "- **Kasa plug not found** — verify the plug is on the same Wi-Fi network "
    "and note its IP address from your router's DHCP table.\n"
    "- **Port conflict** — see the Port Conflict section below.\n\n"
)
text = old_trouble.sub(new_trouble, text, count=1)

# 4. Remove stale links to AUTO_START_TIMING_FIX.md and INSTALLATION.md.
text = re.sub(r'- \[AUTO_START_TIMING_FIX\.md\][^\n]*\n', '', text)
text = re.sub(r'- \[INSTALLATION\.md\][^\n]*\n', '', text)
# Remove any remaining inline refs to those files.
text = re.sub(r'\[INSTALLATION\.md\]\([^)]*\)', 'the troubleshooting section above', text)
text = re.sub(r'\[AUTO_START_TIMING_FIX\.md\]\([^)]*\)', 'the autostart instructions above', text)

# 5. Fix old project paths.
text = text.replace('/home/pi/threecontrol-/', '/opt/fermentatorium/')
text = text.replace('bash /full/path/to/threecontrol-/install_service.sh',
                    'sudo /opt/fermentatorium/install.sh')
text = text.replace('# bash /home/pi/threecontrol-/install_service.sh',
                    '# sudo /opt/fermentatorium/install.sh')

# 6. Normalise path examples to /opt/fermentatorium.
text = text.replace('~/fermentatorium/', '/opt/fermentatorium/')
text = text.replace('~/fermentatorium', '/opt/fermentatorium')

with open(path, 'w') as fh:
    fh.write(text)

print('  ✓ README.md updated')
PYEOF
}

# ── Step 6: Rewrite QUICK_START.md for outside users ─────────────────────────
write_quick_start() {
    echo "→ Writing user-facing QUICK_START.md …"
    cat > "${OPT_DIR}/QUICK_START.md" << 'QSEOF'
# Quick Start — Fermentatorium

Fermentatorium is a Raspberry Pi-based fermentation monitor and temperature
controller for homebrewing. This guide gets you from a fresh Raspberry Pi OS
image to a running system in the shortest possible path.

---

## Option A — One-Command Install (Recommended)

Run this single command on a fresh Raspberry Pi OS installation (requires
internet access and `sudo`):

```bash
curl -sSL https://raw.githubusercontent.com/RabbitFarmer/fermentatorium/main/installer/automated-install.sh | sudo bash
```

> **Security note:** Piping directly to `bash` executes the script without
> reviewing it first. If you prefer to inspect the script before running it,
> download it first and then execute:
> ```bash
> curl -sSL https://raw.githubusercontent.com/RabbitFarmer/fermentatorium/main/installer/automated-install.sh -o /tmp/fermentatorium-install.sh
> less /tmp/fermentatorium-install.sh        # review the script
> sudo bash /tmp/fermentatorium-install.sh
> ```

This downloads and runs the installer, which:

1. Installs OS dependencies (`python3-venv`, `bluetooth`, `bluez`, etc.)
2. Clones the repository into a temporary directory
3. Creates a Python virtual environment and installs Python dependencies
4. Creates and enables the `fermentatorium.service` systemd service

After the command completes, open a browser and navigate to:

```
http://<raspberry-pi-ip>:5001
```

---

## Option B — Install from /opt (this copy)

If you are reading this file inside `/opt/fermentatorium`, you can install
directly from this directory:

```bash
sudo /opt/fermentatorium/install.sh
```

The installer auto-detects its location, so no path editing is needed.

After installation, access the dashboard at:

```
http://<raspberry-pi-ip>:5001
```

---

## First-Run Configuration

On first run, the application automatically copies the config templates to
`/opt/fermentatorium/config/` and starts with safe defaults.

Open the web dashboard and navigate to **System Settings** to configure:

| Setting | Where | Notes |
|---------|-------|-------|
| Brewery name / Brewer name | System Settings → General | Appears in notifications |
| Kasa plug IP addresses | Temp Control Settings | For temperature control |
| Tilt hydrometer colors | Tilt Config | One per fermenter |
| Email / Pushover / ntfy | System Settings → Push/Email | Optional, for alerts |

---

## Starting and Stopping

### If installed as a systemd service (default):

```bash
# Status
sudo systemctl status fermentatorium.service

# Restart
sudo systemctl restart fermentatorium.service

# Logs
journalctl -u fermentatorium.service -n 100 --no-pager
```

### Manual start (desktop / development):

```bash
bash /opt/fermentatorium/start.sh
```

This script creates a virtual environment if needed, installs dependencies,
frees port 5001 if occupied, and starts the app in the background.

---

## Desktop Autostart (optional — Pi with monitor)

If your Raspberry Pi has a monitor and you want the browser to open
automatically at login:

```bash
bash /opt/fermentatorium/install_desktop_autostart.sh
```

---

## Remote Access (optional)

Access the dashboard from anywhere using **Raspberry Pi Connect** (free,
no VPN or port forwarding needed):

```bash
sudo apt install rpi-connect
rpi-connect signin
```

Then log in at [connect.raspberrypi.com](https://connect.raspberrypi.com) and
open `http://localhost:5001` through the remote browser session.

---

## Requirements

- Raspberry Pi 4 or Pi 3B+ (64-bit OS recommended)
- MicroSD card ≥ 16 GB
- Python 3.9 or later
- Bluetooth enabled (built-in on most Pi models)
- (Optional) TP-Link Kasa smart plugs for temperature control
- (Optional) Tilt Hydrometer(s) for fermentation monitoring

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| BLE / Tilt not found | Not in bluetooth group | `sudo usermod -aG bluetooth $USER` then log out/in |
| Port 5001 in use | Another process | `start.sh` clears it automatically; or `sudo lsof -i :5001` |
| Kasa plug unreachable | Wrong IP or different subnet | Check router DHCP table; plug and Pi must be on same network |
| `python3-venv` missing | Minimal OS image | `sudo apt install python3-venv python3-full` |

For more detail, see [README.md](README.md).
QSEOF
    echo "  ✓ QUICK_START.md written"
}

# ── Step 7: Set file permissions ───────────────────────────────────────────────
set_permissions() {
    echo "→ Setting file permissions …"

    # Make the opt directory tree readable by all users.
    chmod -R a+rX "${OPT_DIR}"

    # Shell scripts must be executable.
    find "${OPT_DIR}" -maxdepth 1 -name "*.sh" -exec chmod 755 {} +
    find "${OPT_DIR}/installer" -name "*.sh" -exec chmod 755 {} + 2>/dev/null || true

    echo "  ✓ Permissions set"
}

# ── Step 4c: Clean utils/verify_demo_data.py ──────────────────────────────────
clean_verify_demo() {
    echo "→ Cleaning utils/verify_demo_data.py …"
    local f="${OPT_DIR}/utils/verify_demo_data.py"
    [[ -f "${f}" ]] || return 0

    # The script looks for a batch file with the developer's personal brew ID.
    # Replace it with the generic ID that setup_demo.sh now creates.
    sed -i "s/BREWID = 'cf38d0a8'/BREWID = 'demo0001'/" "${f}"
    echo "  ✓ utils/verify_demo_data.py: personal brew ID replaced"
}

# ── Step 4d: Fix stale project-name strings in app.py ─────────────────────────
clean_app_py() {
    echo "→ Fixing stale project-name strings in app.py …"
    local f="${OPT_DIR}/app.py"
    [[ -f "${f}" ]] || return 0

    # The test email and push subjects still say "ThreeControl" (the old project
    # name), which would appear in the user's inbox.  Replace with the current
    # project name so the message makes sense to an outside user.
    sed -i 's/app.py - Three Controller main Flask application./app.py - Fermentatorium main Flask application./g' "${f}"
    sed -i 's/TEST - ThreeControl/TEST - Fermentatorium/g' "${f}"
    sed -i 's/from your ThreeControl system\./from your Fermentatorium system./g' "${f}"
    sed -i "s/brewery_name=system_cfg.get('brewery_name', 'ThreeControl')/brewery_name=system_cfg.get('brewery_name', 'Fermentatorium')/" "${f}"
    echo "  ✓ app.py: stale project-name strings updated"
}


main() {
    require_root

    echo ""
    echo "=== Fermentatorium — Deploy to ${OPT_DIR} ==="
    echo ""

    sync_files
    remove_dev_files
    clean_config_templates
    clean_utils_readme
    clean_setup_demo
    clean_verify_demo
    clean_app_py
    fix_readme
    write_quick_start
    set_permissions

    echo ""
    echo "=== Deployment complete ==="
    echo ""
    echo "Location : ${OPT_DIR}"
    echo "Dashboard: http://<raspberry-pi-ip>:5001  (after running install.sh)"
    echo ""
    echo "Next steps:"
    echo "  1. sudo ${OPT_DIR}/install.sh          # install service + dependencies"
    echo "  2. Log out and back in                  # activate bluetooth group"
    echo "  3. Open http://<raspberry-pi-ip>:5001   # complete first-run config"
    echo "  4. (Optional) bash ${OPT_DIR}/install_desktop_autostart.sh"
    echo "  5. (Optional) sudo apt install rpi-connect && rpi-connect signin"
    echo ""
    echo "See ${OPT_DIR}/QUICK_START.md for full details."
    echo ""
}

main "$@"
