#!/usr/bin/env bash
# a4run.sh — minimal launcher used by the systemd fermentatorium.service unit.
#
# Role: finds the venv Python and exec's a4app.py in the foreground (required for
# systemd Type=simple).  All dependency installation and venv creation is handled
# by a4install.sh; this script intentionally stays minimal.
#
# Do NOT use this script to start the application manually — use a4start.sh instead.
# a4start.sh creates/activates the venv, installs dependencies, and launches the
# application in the background with health-check monitoring.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Always run from the app directory so relative paths (e.g. app.log, venv)
# resolve correctly regardless of how or where this script is invoked.
cd "$APP_DIR"

# Ensure FLASK_PORT is always set to a known value even when this script is
# invoked outside of systemd (which injects Environment="FLASK_PORT=5001").
# Without this, a4app.py would fall through to Flask's own built-in default of
# 5000 whenever the config file is absent — the same port used by older
# installations, causing a conflict.
export FLASK_PORT="${FLASK_PORT:-5001}"

if [[ -x "${APP_DIR}/venv/bin/python3" ]]; then
  PY="${APP_DIR}/venv/bin/python3"
elif [[ -x "${APP_DIR}/.venv/bin/python3" ]]; then
  PY="${APP_DIR}/.venv/bin/python3"
else
  echo "ERROR: No venv found. Create one at '${APP_DIR}/venv' or '${APP_DIR}/.venv'." >&2
  exit 1
fi

exec "${PY}" "${APP_DIR}/a4app.py"