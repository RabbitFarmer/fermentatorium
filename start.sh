#!/bin/bash

echo "=== Three Controller Startup ==="

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

if [ -z "$FLASK_PORT" ]; then
    if [ -f "config/system_config.json" ]; then
        CONFIG_PORT=$(grep -o '"'"'flask_port'"'" *: *[0-9]*' config/system_config.json 2>/dev/null | sed 's/.*: *//')
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

echo "Starting the application..."
PYTHON_PATH="$(which python3)"
APP_PATH="$SCRIPT_DIR/app.py"

if [ -z "$DISPLAY" ]; then
    export SKIP_BROWSER_OPEN=1
fi
nohup "$PYTHON_PATH" "$APP_PATH" > app3.log 2>&1 &
APP_PID=$!

disown -h $APP_PID 2>/dev/null || true

echo "Application started with PID $APP_PID"

sleep 2

if ! ps -p $APP_PID > /dev/null 2>&1; then
    echo "ERROR: Application process died immediately after launch!"
    echo "Last 30 lines of app3.log:"
    tail -30 app3.log 2>/dev/null || echo "  (no log file yet)"
    show_notification "Three Controller Failed" "Application failed to start." "critical"
    exit 1
fi

echo "Waiting for application to respond on http://127.0.0.1:$FLASK_PORT..."

RETRIES=30
RETRY_DELAY=2

for i in $(seq 1 $RETRIES); do
    if curl -s http://127.0.0.1:$FLASK_PORT > /dev/null 2>&1; then
        echo "Application is responding!"
        show_notification "Three Controller Ready" "Application is ready!" "normal"
        break
    fi
    sleep $RETRY_DELAY
done

if [ -n "$DISPLAY" ]; then
    echo "Display detected - app3.py will open browser automatically"
    show_notification "Three Controller Ready" "Dashboard will open in browser shortly" "normal"
else
    echo "No display detected - running in headless mode"
fi

echo "======================================================================="
echo "Startup completed successfully!"
echo "======================================================================="
echo "  Application PID: $APP_PID"
echo "  Access dashboard: http://127.0.0.1:$FLASK_PORT"
echo "  Application log: app3.log"
echo "======================================================================="
