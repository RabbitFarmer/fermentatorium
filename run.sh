#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "${APP_DIR}/venv/bin/python3" ]]; then
  PY="${APP_DIR}/venv/bin/python3"
elif [[ -x "${APP_DIR}/.venv/bin/python3" ]]; then
  PY="${APP_DIR}/.venv/bin/python3"
else
  echo "ERROR: No venv found. Create one at '${APP_DIR}/venv' or '${APP_DIR}/.venv'." >&2
  exit 1
fi

exec "${PY}" "${APP_DIR}/app.py"