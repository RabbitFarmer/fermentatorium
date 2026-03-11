#!/bin/bash
# a4start.sh — user-facing launcher for the Fermentatorium application.
#
# Role: creates/activates the Python virtual environment, installs dependencies
# if needed, frees any conflicting ports, then starts a4app.py in the background
# with health-check monitoring.
#
# This is the script to run manually or configure for desktop autostart:
#   chmod +x ~/fermentatorium/a4start.sh
#   ./a4start.sh
#
# For headless/server autostart at boot, the systemd service installed by
# a4install.sh uses a4run.sh instead (which is intentionally minimal and runs
# a4app.py in the foreground as required by systemd Type=simple).

echo "=== Fermentatorium Startup ==="

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

show_notification() {
    local title="$1"
    local message="$2"
    local urgency="${3:-normal}"
    if [ -n "$DISPLAY" ] && command -v notify-send > /dev/null 2>&1; then
        notify-send -u "$urgency" "$title" "$message" 2>/dev/null || true
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
            echo "[startup] Freeing port ${p} (PIDs: $pids)..."
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
            echo "[startup] Freeing port ${p} via fuser..."
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
            echo "Using port $FLASK_PORT from config/system_config.json"
        else
            FLASK_PORT=5001
            echo "Using default port $FLASK_PORT"
        fi
    else
        FLASK_PORT=5001
        echo "Using default port $FLASK_PORT"
    fi
else
    echo "Using port $FLASK_PORT from environment variable"
fi
export FLASK_PORT

# ── Clean up old autostart entries that open the browser to port 5000 ─────────
# Previous installations may have left LXDE autostart lines or .desktop files
# that tell Chromium to open http://127.0.0.1:5000 at login.  When the app now
# runs on port 5001, those stale entries cause "cannot connect to :5000" and
# prevent the startup page from appearing.  Remove them once and for all.
_cleanup_stale_port5000_entries() {
    # 1. LXDE native autostart (Raspberry Pi OS LXDE-pi desktop)
    local lxde_as="$HOME/.config/lxsession/LXDE-pi/autostart"
    if [ -f "$lxde_as" ] && grep -qE ':5000' "$lxde_as" 2>/dev/null; then
        echo "Removing stale port-5000 browser entry from LXDE autostart..."
        # The sed below removes the offending line, so this backup is created only
        # once — the grep will find nothing on the next a4start.sh run.
        cp "$lxde_as" "${lxde_as}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
        sed -i -E '/:5000/d' "$lxde_as"
        echo "  Done. Backup saved alongside the original file."
    fi

    # 2. XDG/GNOME-style .desktop autostart files in ~/.config/autostart/
    #    Look for any file that launches a browser (chromium, firefox, etc.) to :5000
    #    but is NOT the fermentatorium launcher itself.
    if [ -d "$HOME/.config/autostart" ]; then
        for _df in "$HOME/.config/autostart/"*.desktop; do
            [ -f "$_df" ] || continue
            # Skip the fermentatorium desktop entry
            case "$_df" in *fermentatorium*) continue ;; esac
            if grep -qE 'Exec=.*(chromium|firefox|epiphany|midori|xdg-open).*:5000' "$_df" 2>/dev/null; then
                echo "Removing stale port-5000 browser autostart: $_df"
                rm -f "$_df"
            fi
        done
    fi
}
_cleanup_stale_port5000_entries

# ── Already running? ──────────────────────────────────────────────────────────
# If a Fermentatorium instance is already responding on the configured port
# (e.g. the systemd service started it at boot), skip the full start sequence,
# show a notification and open the browser instead of trying to re-launch.
if curl -s --max-time 2 "http://127.0.0.1:$FLASK_PORT/" > /dev/null 2>&1; then
    echo "Fermentatorium is already running on port $FLASK_PORT."
    show_notification "Fermentatorium" "Already running — opening dashboard." "normal"
    if [ -n "$DISPLAY" ] && command -v xdg-open > /dev/null 2>&1; then
        xdg-open "http://127.0.0.1:$FLASK_PORT/" &
    fi
    exit 0
fi

show_notification "Fermentatorium" "Starting up — please wait…" "normal"

echo "Checking for virtual environment..."
VENV_DIR=""

if [ -d ".venv" ]; then
    VENV_DIR=".venv"
elif [ -d "venv" ]; then
    VENV_DIR="venv"
else
    VENV_DIR=".venv"
    echo "No virtual environment found. Creating .venv..."
    if ! python3 -m venv "$VENV_DIR"; then
        echo "ERROR: Failed to create a virtual environment. Exiting."
        exit 1
    fi
    echo "Virtual environment created successfully at $VENV_DIR"
fi

echo "Activating virtual environment ($VENV_DIR)..."
source "$VENV_DIR/bin/activate"
echo "Virtual environment activated."

export PIP_DISABLE_PIP_VERSION_CHECK=1

if [ -f "requirements.txt" ]; then
    echo "Checking dependencies..."
    if "$VENV_DIR/bin/python3" -c "import flask, bleak" 2>/dev/null; then
        echo "Dependencies already satisfied (skipping pip install)"
    else
        echo "Installing/updating dependencies from requirements.txt..."
        if ! pip install --quiet --disable-pip-version-check -r requirements.txt 2>>app.log; then
            echo "WARNING: Failed to install dependencies"
            if ! "$VENV_DIR/bin/python3" -c "import flask" 2>/dev/null; then
                echo "ERROR: Flask not available. Cannot start application."
                exit 1
            fi
        else
            echo "Dependencies installed successfully."
        fi
    fi
else
    echo "Warning: requirements.txt not found. Skipping dependency installation."
fi

echo "Cleaning Python cache..."
find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$SCRIPT_DIR" -type f -name "*.pyc" -delete 2>/dev/null || true

# Free any processes already occupying port 5000 (Flask's built-in default, used by
# older installations from the previous repo) and the configured port before starting
# Python. a4app.py also calls free_port() internally, but doing it here at the shell
# level means Python starts up against a clean network state from the very first line.
echo "Freeing port 5000 (legacy Flask default) before start..."
kill_port 5000
if [ "$FLASK_PORT" != "5000" ]; then
    echo "Freeing port $FLASK_PORT before start..."
    kill_port "$FLASK_PORT"
fi

echo "Starting the application..."
PYTHON_PATH="$VENV_DIR/bin/python3"
APP_PATH="$SCRIPT_DIR/a4app.py"

nohup "$PYTHON_PATH" "$APP_PATH" > app.log 2>&1 &
APP_PID=$!

disown -h $APP_PID 2>/dev/null || true

echo "Application started with PID $APP_PID"

sleep 2

if ! ps -p $APP_PID > /dev/null 2>&1; then
    echo "ERROR: Application process died immediately after launch!"
    echo "Last 30 lines of app.log:"
    tail -30 app.log 2>/dev/null || echo "  (no log file yet)"
    show_notification "Fermentatorium" "Process died on launch — check app.log for details." "critical"
    exit 1
fi

echo "Waiting for application to respond on http://127.0.0.1:$FLASK_PORT..."

RETRIES=30
RETRY_DELAY=2
APP_STARTED=false

for i in $(seq 1 $RETRIES); do
    if curl -s --max-time 2 "http://127.0.0.1:$FLASK_PORT/" > /dev/null 2>&1; then
        echo "Application is responding!"
        APP_STARTED=true
        break
    fi
    echo "  Still waiting… ($i/$RETRIES)"
    # Send a desktop notification every ~10 s so the user can see progress.
    if [ $((i % 5)) -eq 0 ]; then
        show_notification "Fermentatorium" "Still starting up… ($i/$RETRIES)" "normal"
    fi
    sleep $RETRY_DELAY
done

if [ "$APP_STARTED" = true ]; then
    show_notification "Fermentatorium" "Ready! Opening dashboard…" "normal"
    if [ -n "$DISPLAY" ] && command -v xdg-open > /dev/null 2>&1; then
        echo "Opening browser at http://127.0.0.1:$FLASK_PORT/startup ..."
        xdg-open "http://127.0.0.1:$FLASK_PORT/startup" &
    else
        echo "No display detected - running in headless mode"
    fi
else
    echo "======================================================================="
    echo "WARNING: Application did not respond after $((RETRIES * RETRY_DELAY)) seconds."
    echo "Last 30 lines of app.log:"
    tail -30 app.log 2>/dev/null || echo "  (no log file yet)"
    echo "======================================================================="
    show_notification "Fermentatorium" "Timed out waiting for server — check app.log for details." "critical"
fi

echo "======================================================================="
echo "  Application PID: $APP_PID"
echo "  Access dashboard: http://127.0.0.1:$FLASK_PORT"
echo "  Application log:  app.log"
echo "======================================================================="
