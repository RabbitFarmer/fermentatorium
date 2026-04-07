#!/bin/bash
# start.sh — user-facing launcher for the Fermentatorium application.
#
# Role: creates/activates the Python virtual environment, installs dependencies
# if needed, frees any conflicting ports, then starts app.py in the background
# with health-check monitoring.
#
# This is the script to run manually or configure for desktop autostart:
#   chmod +x ~/fermentatorium/start.sh
#   ./start.sh
#
# For headless/server autostart at boot, the systemd service installed by
# install.sh uses run.sh instead (which is intentionally minimal and runs
# app.py in the foreground as required by systemd Type=simple).

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ── Prevent concurrent execution ──────────────────────────────────────────────
# On systems where both the LXDE session autostart and the XDG .desktop
# autostart mechanism fire simultaneously (e.g. Raspberry Pi LXDE desktop),
# two copies of this script can be launched at the same time.  A file lock
# ensures only the first one proceeds; the second exits immediately.
# The lock is automatically released when this process exits (fd 9 is closed).
LOCK_DIR="$HOME/.cache/fermentatorium"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/start.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Another instance of start.sh is already running. Exiting."
    exit 0
fi

show_notification() {
    local title="$1"
    local message="$2"
    local urgency="${3:-normal}"
    if [ -n "$DISPLAY" ] && command -v notify-send > /dev/null 2>&1; then
        notify-send -u "$urgency" "$title" "$message" 2>/dev/null || true
    fi
}

# Open the browser in a normal window (address bar and dev tools accessible).
# Tries Chromium first; falls back to xdg-open.
# To re-enable kiosk mode, add --kiosk --new-window flags to the browser launch line below.
open_browser() {
    local url="$1"
    [ -n "$DISPLAY" ] || return 0
    local browser=""
    for candidate in chromium-browser chromium google-chrome google-chrome-stable; do
        if command -v "$candidate" > /dev/null 2>&1; then
            browser="$candidate"
            break
        fi
    done
    if [ -n "$browser" ]; then
        "$browser" "$url" >> app.log 2>&1 &
        disown $! 2>/dev/null || true
    elif command -v xdg-open > /dev/null 2>&1; then
        xdg-open "$url" &
        disown $! 2>/dev/null || true
    fi
}

# Kill any process listening on the given TCP port.
# Uses lsof (preferred) or fuser (fallback) — both available on Raspberry Pi OS.
kill_port() {
    local p="$1"
    local pids=""
    if command -v lsof > /dev/null 2>&1; then
        pids=$(lsof -ti ":${p}" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo "$pids" | xargs -r kill -TERM 2>/dev/null || true
            sleep 1
            pids=$(lsof -ti ":${p}" 2>/dev/null || true)
            if [ -n "$pids" ]; then
                echo "$pids" | xargs -r kill -KILL 2>/dev/null || true
                sleep 1
            fi
        fi
    elif command -v fuser > /dev/null 2>&1; then
        if fuser "${p}/tcp" > /dev/null 2>&1; then
            fuser -k "${p}/tcp" > /dev/null 2>/dev/null || true
            sleep 1
        fi
    fi
}

# ── Resolve FLASK_PORT early so we can do the "already running" check ─────────
if [ -z "$FLASK_PORT" ]; then
    if [ -f "config/system_config.json" ]; then
        CONFIG_PORT=$(grep -o '"flask_port" *: *[0-9]*' config/system_config.json 2>/dev/null | sed 's/.*: *//')
        if [ -n "$CONFIG_PORT" ]; then
            FLASK_PORT=$CONFIG_PORT
        else
            FLASK_PORT=5001
        fi
    else
        FLASK_PORT=5001
    fi
fi
export FLASK_PORT

# ── Clean up old autostart entries that open the browser to port 5000 ─────────
_cleanup_stale_port5000_entries() {
    local lxde_as="$HOME/.config/lxsession/LXDE-pi/autostart"
    if [ -f "$lxde_as" ] && grep -qE ':5000' "$lxde_as" 2>/dev/null; then
        cp "$lxde_as" "${lxde_as}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
        sed -i -E '/:5000/d' "$lxde_as"
    fi

    if [ -d "$HOME/.config/autostart" ]; then
        for _df in "$HOME/.config/autostart/"*.desktop; do
            [ -f "$_df" ] || continue
            case "$_df" in *fermentatorium*) continue ;; esac
            if grep -qE 'Exec=.*(chromium|firefox|epiphany|midori|xdg-open).*:5000' "$_df" 2>/dev/null; then
                rm -f "$_df"
            fi
        done
    fi
}
_cleanup_stale_port5000_entries

# ── Already running? ──────────────────────────────────────────────────────────
if curl -s --max-time 2 "http://127.0.0.1:$FLASK_PORT/" > /dev/null 2>&1; then
    show_notification "Fermentatorium" "Already running — opening dashboard." "normal"
    open_browser "http://127.0.0.1:$FLASK_PORT/"
    exit 0
fi

# ── Systemd service already managing the process? ─────────────────────────────
# On systems where both the fermentatorium systemd service and the desktop
# autostart are enabled, both launch paths fire on reboot: systemd starts
# run.sh → app.py, and the desktop autostart fires this script.  Because the
# HTTP check above only passes once Flask is listening (which takes a few
# seconds), both paths can race past it and start a second app.py before the
# first one has bound the port.
#
# If the systemd service is already active (or activating), we know app.py is
# coming up via that path — we just need to wait for it to be ready and then
# open the browser.  We must NOT start another instance.
_svc_state=$(systemctl show fermentatorium.service --property=ActiveState 2>/dev/null | cut -d= -f2)
if [ "$_svc_state" = "active" ] || [ "$_svc_state" = "activating" ]; then
    show_notification "Fermentatorium" "Service is starting — please wait…" "normal"
    _svc_retries=30
    for i in $(seq 1 $_svc_retries); do
        if curl -s --max-time 2 "http://127.0.0.1:$FLASK_PORT/" > /dev/null 2>&1; then
            show_notification "Fermentatorium" "Ready! Opening dashboard…" "normal"
            open_browser "http://127.0.0.1:$FLASK_PORT/?autostart=1"
            exit 0
        fi
        sleep 2
    done
    # Service is running but Flask is not responding — fall through to a fresh start.
    # The kill_port calls below will free the port so a new instance can bind it.
    show_notification "Fermentatorium" "Service unresponsive — attempting fresh start…" "normal"
    echo "WARNING: systemd service active but Flask did not respond after $((30 * 2)) s. Falling through to fresh start."
fi

show_notification "Fermentatorium" "Starting up — please wait…" "normal"

VENV_DIR=""
if [ -d ".venv" ]; then
    VENV_DIR=".venv"
elif [ -d "venv" ]; then
    VENV_DIR="venv"
else
    VENV_DIR=".venv"
    if ! python3 -m venv "$VENV_DIR"; then
        echo "ERROR: Failed to create a virtual environment. Exiting."
        exit 1
    fi
fi

source "$VENV_DIR/bin/activate"

export PIP_DISABLE_PIP_VERSION_CHECK=1

if [ -f "requirements.txt" ]; then
    # Only run pip install when requirements.txt has actually changed.
    # A hash of the file is stored in the cache directory; if it matches the
    # current file we skip the install entirely, which saves 10-30 s on every
    # repeat start (especially on slower hardware like Raspberry Pi).
    REQ_HASH_FILE="$LOCK_DIR/requirements.hash"
    current_hash=$(md5sum requirements.txt 2>/dev/null | cut -d' ' -f1)
    stored_hash=$(cat "$REQ_HASH_FILE" 2>/dev/null || echo "")

    if [ -n "$current_hash" ] && [ "$current_hash" = "$stored_hash" ]; then
        echo "Requirements unchanged — skipping pip install."
    else
        echo "Installing/updating Python packages…"
        if timeout 300 pip install --quiet --disable-pip-version-check -r requirements.txt 2>>app.log; then
            # Record the hash only on success so a partial install is retried next time.
            echo "$current_hash" > "$REQ_HASH_FILE"
        else
            echo "WARNING: pip install failed; some packages may be missing"
            if ! timeout 10 "$VENV_DIR/bin/python3" -c "import flask" 2>/dev/null; then
                echo "ERROR: Flask not available. Cannot start application."
                exit 1
            fi
        fi
    fi
fi

kill_port 5000
if [ "$FLASK_PORT" != "5000" ]; then
    kill_port "$FLASK_PORT"
fi

PYTHON_PATH="$VENV_DIR/bin/python3"
APP_PATH="$SCRIPT_DIR/app.py"

# SKIP_BROWSER_OPEN tells app.py not to open its own browser window.
# start.sh is responsible for opening the browser (below), so we avoid
# two windows racing to open the same URL.
env SKIP_BROWSER_OPEN=1 FLASK_DEBUG=1 FLASK_ENV=development nohup "$PYTHON_PATH" "$APP_PATH" > app.log 2>&1 &
APP_PID=$!

disown -h $APP_PID 2>/dev/null || true

# Brief poll to confirm the process didn't die immediately (replaces a fixed sleep).
_alive_checks=0
while [ $_alive_checks -lt 4 ]; do
    sleep 0.5
    _alive_checks=$((_alive_checks + 1))
    if ! ps -p $APP_PID > /dev/null 2>&1; then
        echo "ERROR: Application process died immediately after launch!"
        echo "Last 30 lines of app.log:"
        tail -30 app.log 2>/dev/null || echo "  (no log file yet)"
        show_notification "Fermentatorium" "Process died on launch — check app.log for details." "critical"
        exit 1
    fi
done

RETRIES=60
RETRY_DELAY=1
APP_STARTED=false

for i in $(seq 1 $RETRIES); do
    if curl -s --max-time 2 "http://127.0.0.1:$FLASK_PORT/" > /dev/null 2>&1; then
        APP_STARTED=true
        break
    fi
    if [ $((i % 10)) -eq 0 ]; then
        show_notification "Fermentatorium" "Still starting up… ($i/$RETRIES)" "normal"
    fi
    sleep $RETRY_DELAY
done

if [ "$APP_STARTED" = true ]; then
    show_notification "Fermentatorium" "Ready! Opening dashboard…" "normal"
    open_browser "http://127.0.0.1:$FLASK_PORT/?autostart=1"
else
    echo "ERROR: Application did not respond after $((RETRIES * RETRY_DELAY)) seconds."
    echo "Last 30 lines of app.log:"
    tail -30 app.log 2>/dev/null || echo "  (no log file yet)"
    show_notification "Fermentatorium" "Timed out waiting for server — check app.log for details." "critical"
fi
