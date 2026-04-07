#!/bin/bash
# stopit.sh — stop all Fermentatorium processes regardless of how they were started.
# Handles both: systemd service AND desktop-autostart (nohup) launches.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ── 1. Stop systemd service (if installed and running) ────────────────────────
if systemctl list-units --full --all 2>/dev/null | grep -q 'fermentatorium.service'; then
    echo "Stopping systemd service..."
    sudo systemctl stop fermentatorium 2>/dev/null || true
fi

# ── 2. Kill app.py Python process (desktop autostart / nohup launch) ──────────
echo "Killing app.py processes..."
pkill -TERM -f "python.*app\.py" 2>/dev/null || true
sleep 1
pkill -KILL -f "python.*app\.py" 2>/dev/null || true

# ── 3. Free the Flask port ─────────────────────────────────────────────────────
FLASK_PORT=5001
if [ -f "config/system_config.json" ]; then
    CONFIG_PORT=$(grep -o '"flask_port" *: *[0-9]*' config/system_config.json 2>/dev/null | sed 's/.*: *//')
    if [ -n "$CONFIG_PORT" ]; then
        FLASK_PORT=$CONFIG_PORT
    fi
fi

kill_port() {
    local p="$1"
    local pids=""
    if command -v lsof > /dev/null 2>&1; then
        pids=$(lsof -ti ":${p}" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo "$pids" | xargs -r kill -TERM 2>/dev/null || true
            sleep 1
            pids=$(lsof -ti ":${p}" 2>/dev/null || true)
            [ -n "$pids" ] && echo "$pids" | xargs -r kill -KILL 2>/dev/null || true
        fi
    elif command -v fuser > /dev/null 2>&1; then
        fuser -k "${p}/tcp" > /dev/null 2>/dev/null || true
    fi
}

echo "Freeing port $FLASK_PORT..."
kill_port "$FLASK_PORT"
kill_port 5000

# ── 4. Remove stale flock so start.sh can run again ───────────────────────────
LOCK_FILE="$HOME/.cache/fermentatorium/start.lock"
if [ -f "$LOCK_FILE" ]; then
    rm -f "$LOCK_FILE"
    echo "Removed stale lock file."
fi

echo "Fermentatorium stopped."
