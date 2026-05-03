#!/usr/bin/env bash
# run.sh — minimal launcher used by the systemd fermentatorium.service unit.
#
# Role: finds the venv Python and exec's app.py in the foreground (required for
# systemd Type=simple).  All dependency installation and venv creation is handled
# by install.sh; this script intentionally stays minimal.
#
# Do NOT use this script to start the application manually — use start.sh instead.
# start.sh creates/activates the venv, installs dependencies, and launches the
# application in the background with health-check monitoring.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Always run from the app directory so relative paths (e.g. app.log, venv)
# resolve correctly regardless of how or where this script is invoked.
cd "$APP_DIR"

# Ensure FLASK_PORT is always set to a known value even when this script is
# invoked outside of systemd (which injects Environment="FLASK_PORT=5001").
# Without this, app.py would fall through to Flask's own built-in default of
# 5000 whenever the config file is absent — the same port used by older
# installations, causing a conflict.
export FLASK_PORT="${FLASK_PORT:-5001}"

# The systemd service has no display — suppress the browser-open code in app.py.
export SKIP_BROWSER_OPEN=1

# Auto-update: pull the latest code from git before starting.
# This ensures a service restart picks up any fixes pushed to the remote
# without requiring a separate manual "Update System" step.
# Trust assumption: the remote is the owner's own GitHub repository.
# --ff-only limits pulls to fast-forwards only, so force-pushed or diverged
# remotes are rejected rather than blindly applied.
# Failures (missing .git, no network, local modifications, timeout) are logged
# but do not prevent the service from starting with the existing code.
_REMOTE_URL="https://github.com/RabbitFarmer/fermentatorium.git"

if command -v git > /dev/null 2>&1; then
    if ! git -C "${APP_DIR}" rev-parse --git-dir > /dev/null 2>&1; then
        # No .git directory — this installation was deployed without git
        # (e.g. via deploy_to_opt.sh which excludes .git/).  Initialise git
        # in-place so this and all future restarts can auto-update normally.
        # Config files and runtime data are gitignored and will not be touched.
        echo "No git repository found at ${APP_DIR} — initialising git in-place to enable auto-updates …" >&2
        _git_ok=true
        git -C "${APP_DIR}" init > /dev/null 2>&1 || _git_ok=false
        # set-url is safe whether or not 'origin' already exists (idempotent);
        # if origin is absent, add it instead.
        if ! git -C "${APP_DIR}" remote set-url origin "${_REMOTE_URL}" > /dev/null 2>&1; then
            git -C "${APP_DIR}" remote add origin "${_REMOTE_URL}" > /dev/null 2>&1 || _git_ok=false
        fi
        timeout 120 git -C "${APP_DIR}" fetch --quiet origin main > /dev/null 2>&1 || _git_ok=false
        git -C "${APP_DIR}" reset --hard origin/main > /dev/null 2>&1 || _git_ok=false
        if [ "${_git_ok}" = "true" ]; then
            echo "Git bootstrap complete — running with latest code." >&2
        else
            echo "WARNING: git bootstrap failed — starting with existing code." >&2
        fi
    else
        # .git exists — do a normal fast-forward pull.
        if ! timeout 60 git -C "${APP_DIR}" pull --quiet --ff-only 2>&1; then
            echo "WARNING: auto git pull failed — starting with existing code" >&2
        fi
    fi
fi

if [[ -x "${APP_DIR}/venv/bin/python3" ]]; then
  PY="${APP_DIR}/venv/bin/python3"
elif [[ -x "${APP_DIR}/.venv/bin/python3" ]]; then
  PY="${APP_DIR}/.venv/bin/python3"
else
  echo "ERROR: No venv found. Create one at '${APP_DIR}/venv' or '${APP_DIR}/.venv'." >&2
  exit 1
fi

exec "${PY}" "${APP_DIR}/app.py"