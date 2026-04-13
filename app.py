#!/usr/bin/env python3
"""
app.py - Three Controller main Flask application.

This file provides the full Flask app used in the conversation:
- BLE scanning (BleakScanner) if available
- Per-brew JSONL files under batches/{brewid}.jsonl (migrates legacy batch_{COLOR}_{BREWID}_{MMDDYYYY}.jsonl)
- Restricted control log in temp_control_log.jsonl
- Kasa worker integration (if kasa_worker available)
- Per-batch append_sample_to_batch_jsonl and forward_to_third_party_if_configured
- Chart Plotly page and /chart_data/<identifier> endpoint
- UI routes: dashboard, tilt_config, batch_settings, temp_config, update_temp_config, temp_report,
  export_temp_csv, scan_kasa_plugs, live_snapshot, reset_logs, exit_system, system_config,
  backup_system, restore_system, list_backups, update_system
- Program entry runs Flask on 0.0.0.0:5001
"""

import asyncio
import hashlib
import itertools
import json
import os
import queue
import re
import shutil
import smtplib
import sys
import threading
import time
from collections import deque, defaultdict
from datetime import datetime
from glob import glob as glob_func
from math import ceil
from multiprocessing import Process, Queue
import multiprocessing  # Needed for set_start_method and get_all_start_methods
from urllib.parse import urlparse
import urllib.request
import urllib.error
import subprocess
import signal
import webbrowser
import socket

from email.mime.text import MIMEText
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   url_for, make_response)

# Optional imports
try:
    from bleak import BleakScanner
except Exception:
    BleakScanner = None

try:
    from tilt_static import TILT_UUIDS, COLOR_MAP
except Exception:
    TILT_UUIDS = {}
    COLOR_MAP = {}

from tilt_table import (
    load_tilt_table, save_tilt_table,
    upsert_device_from_reading,
    set_device_variances, get_device_variances,
    normalize_mac,
)

try:
    from kasa_manager import KasaManager
    _kasa_manager_available = True
except Exception:
    KasaManager = None
    _kasa_manager_available = False

try:
    import requests
except Exception:
    requests = None

# Optional psutil for process management
try:
    import psutil
except Exception:
    psutil = None

# Import log_error and log_kasa_command for kasa logging
try:
    from logger import log_error, log_kasa_command, log_kasa_diag, log_notification, log_event
except Exception:
    def log_error(msg, **extra):
        print(f"[ERROR] {msg}")
    def log_kasa_command(mode, url, action, success=None, error=None):
        pass
    def log_kasa_diag(level, msg, **extra):
        pass
    def log_notification(notification_type, subject, body, success, tilt_color=None, error=None):
        pass
    def log_event(event_type, message, tilt_color=None):
        pass

# Resolve the directory that contains this file so that templates, static
# assets, and data files are always loaded from *this* repository — even if
# multiple copies of the project exist on the machine or the process is
# started from a different working directory.
_HERE = os.path.abspath(os.path.dirname(__file__))

# --plug command-line switch: forces all Kasa plugs to use the legacy IOT
# protocol on port 9999, bypassing KLAP discovery and credential-based
# authentication entirely.  Useful as a workaround for newer KLAP-capable
# devices (e.g. EP25 v2.6+) when KLAP negotiation fails.
# NOTE: evaluated at module level so it is True in the parent process but
# False in spawned worker subprocesses (sys.argv differs there), which is
# the desired behavior — the flag only affects the parent's call-sites.
_FORCE_IOT_PORT = '--plug' in sys.argv

app = Flask(
    __name__,
    template_folder=os.path.join(_HERE, 'templates'),
    static_folder=os.path.join(_HERE, 'static'),
)

# Add cache control headers to prevent browser caching of HTML pages
@app.after_request
def add_header(response):
    """Add headers to prevent browser caching of HTML pages.
    
    This ensures users always see the latest version of the page,
    preventing issues where old cached versions are displayed.
    Static files (CSS, JS, images) are still cached normally.

    X-Fermentatorium-Server is stamped on every response so the user can
    confirm in browser DevTools (Network tab → response headers) that the
    response is coming from this copy of app.py and not from an old
    process, nginx, or another web server on the same machine.
    """
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = 'Thu, 01 Jan 1970 00:00:00 GMT'
    # Stamp every response (HTML and JSON alike) so network-level inspection
    # can confirm which server binary is handling the request.
    response.headers['X-Fermentatorium-Server'] = f'app.py pid={os.getpid()} path={_HERE}'
    return response

# --- Files and global constants ---------------------------------------------
# All paths are absolute and anchored to this file's directory so that
# running the program from any working directory (or from a different copy
# of the project) always reads/writes the correct files.
LOG_PATH    = os.path.join(_HERE, 'temp_control', 'temp_control_log.jsonl')
TEMP_CONTROL_DIR = os.path.dirname(LOG_PATH)  # temp_control/ directory

# Regex that matches any valid per-color (or legacy shared) temp-control log filename.
# Valid: temp_control_log.jsonl, temp_control_log_orange.jsonl, temp_control_log_black.jsonl …
_TEMP_CONTROL_LOG_RE = re.compile(r'^temp_control_log[a-z_]*\.jsonl$')

def _get_control_log_path(tilt_color):
    """Return the per-color temp control log path; falls back to LOG_PATH if no color."""
    if tilt_color:
        safe = tilt_color.lower().replace(' ', '_')
        return os.path.join(TEMP_CONTROL_DIR, f'temp_control_log_{safe}.jsonl')
    return LOG_PATH

def _list_all_control_log_files():
    """Return sorted list of all per-color (and fallback) temp control log paths that exist."""
    found = []
    if os.path.exists(TEMP_CONTROL_DIR):
        for fname in sorted(os.listdir(TEMP_CONTROL_DIR)):
            # Exclude backup copies (contain '.bak' anywhere in the name)
            if _TEMP_CONTROL_LOG_RE.match(fname) and '.bak' not in fname:
                found.append(os.path.join(TEMP_CONTROL_DIR, fname))
    return found
BATCHES_DIR = os.path.join(_HERE, 'batches')
PER_PAGE = 30

# Browser-warning capture log.  Written by the /client_log route when the JS
# interceptor in the page templates sends console.warn / console.error entries
# to the server.  The file lives in logs/ so it appears automatically in the
# Log Management page alongside other .log files.
BROWSER_WARN_LOG = os.path.join(_HERE, 'logs', 'browser_warnings.log')

# Config files
TILT_CONFIG_FILE   = os.path.join(_HERE, 'config', 'tilt_config.json')
TEMP_CFG_FILE      = os.path.join(_HERE, 'config', 'temp_control_config.json')
SYSTEM_CFG_FILE    = os.path.join(_HERE, 'config', 'system_config.json')

# Valid tab names for system config page (using set for O(1) lookup)
VALID_SYSTEM_CONFIG_TABS = {'main-settings', 'push-email', 'logging-integrations', 'backup-restore'}

# Chart caps
DEFAULT_CHART_LIMIT = 3000
MAX_CHART_LIMIT = 3000
MAX_ALL_LIMIT = 10000
MAX_FILENAME_LENGTH = 50

# In-memory buffer for temperature control readings
# Stores recent readings in memory (not immediately written to file)
# Max 1440 entries = 2 days at 2-minute intervals (prevents memory bloat)
# Readings can be exported to CSV if needed
TEMP_READING_BUFFER_SIZE = 1440
temp_reading_buffer = deque(maxlen=TEMP_READING_BUFFER_SIZE)

# --- Initialize config files from templates if they don't exist -------------
def ensure_config_files():
    """
    Ensure config files exist by copying from templates if needed.
    This prevents rsync/git pull from overwriting user's configuration data.
    """
    config_files = [
        ('config/system_config.json', 'config/system_config.json.template'),
        ('config/temp_control_config.json', 'config/temp_control_config.json.template'),
        ('config/tilt_config.json', 'config/tilt_config.json.template')
    ]
    
    for config_file, template_file in config_files:
        if not os.path.exists(config_file):
            if os.path.exists(template_file):
                try:
                    shutil.copy2(template_file, config_file)
                    print(f"[INIT] Created {config_file} from {template_file}")
                except Exception as e:
                    print(f"[INIT] Error copying {template_file} to {config_file}: {e}")
            else:
                print(f"[INIT] Warning: Neither {config_file} nor {template_file} exists")

ensure_config_files()

# --- Stop other app.py processes on startup --------------------------------
def stop_other_app_py():
    current_pid = os.getpid()
    stopped = []
    errors = []
    if psutil:
        try:
            # Collect matching processes first, then kill them together with
            # their children.  On Linux, 'fork'-spawned kasa_worker processes
            # inherit the parent's cmdline ("python3 app.py"), so they would
            # match the same filter.  We kill the *parent* app.py process first
            # and then explicitly terminate its children (forked workers) rather
            # than letting them become long-lived orphans.
            targets = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    pid = proc.info['pid']
                    if pid == current_pid:
                        continue
                    cmdline = proc.info.get('cmdline') or []
                    name = proc.info.get('name') or ''
                    if any('app.py' in str(p) for p in cmdline) or 'app.py' in name:
                        targets.append(proc)
                except Exception:
                    continue

            for proc in targets:
                try:
                    # Grab children before terminating the parent (they become
                    # un-findable as children once the parent is gone).
                    try:
                        children = proc.children(recursive=True)
                    except Exception:
                        children = []
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    stopped.append(proc.pid)
                    # Also terminate any forked worker children that inherited
                    # the same cmdline and would otherwise become orphans.
                    for child in children:
                        if child.pid == current_pid:
                            continue
                        try:
                            child.terminate()
                            child.wait(timeout=2)
                        except Exception:
                            try:
                                child.kill()
                            except Exception as e:
                                errors.append((child.pid, str(e)))
                        stopped.append(child.pid)
                except Exception as e:
                    errors.append((proc.pid, str(e)))
            return {"stopped": stopped, "errors": errors}
        except Exception as e:
            errors.append(("psutil_iter", str(e)))

    try:
        pgrep = subprocess.run(['pgrep', '-f', 'app.py'], capture_output=True, text=True)
        if pgrep.returncode == 0:
            for line in pgrep.stdout.splitlines():
                try:
                    pid = int(line.strip())
                except Exception:
                    continue
                if pid == current_pid:
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                    stopped.append(pid)
                except Exception as e:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        stopped.append(pid)
                    except Exception as e2:
                        errors.append((pid, f"{e} / {e2}"))
    except Exception as e:
        errors.append(("pgrep", str(e)))

    return {"stopped": stopped, "errors": errors}

def find_process_using_port(port):
    """
    Find the process ID (PID) using a specific port.
    
    Args:
        port: Port number to check
    
    Returns:
        PID of the process using the port, or None if not found
    """
    # Try using lsof first (most reliable on Linux/Unix)
    try:
        result = subprocess.run(
            ['lsof', '-ti', f':{port}'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split('\n')[0])
            return pid
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
        pass
    
    # Try using psutil if available
    if psutil:
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == port and conn.status == 'LISTEN':
                    pid = conn.pid
                    if pid:
                        return pid
        except (psutil.AccessDenied, AttributeError) as e:
            pass
    
    # Try using netstat as a fallback
    try:
        result = subprocess.run(
            ['netstat', '-tlnp'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if f':{port}' in line and 'LISTEN' in line:
                    # Extract PID from netstat output (format: PID/program)
                    parts = line.split()
                    for part in parts:
                        if '/' in part:
                            try:
                                pid = int(part.split('/')[0])
                                return pid
                            except ValueError:
                                continue
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        pass
    
    return None

def kill_process_using_port(port):
    """
    Find and kill the process using a specific port.
    
    Args:
        port: Port number to free up
    
    Returns:
        Dictionary with 'killed' (PID if successful) and 'error' (error message if failed)
    """
    pid = find_process_using_port(port)
    
    if not pid:
        return {"killed": None, "error": "No process found using the port"}
    
    current_pid = os.getpid()
    if pid == current_pid:
        return {"killed": None, "error": "Cannot kill own process"}
    
    try:
        # Try graceful termination first (SIGTERM)
        os.kill(pid, signal.SIGTERM)
        
        # Wait briefly for process to exit (up to 1 second: 10 x 100ms)
        for _ in range(10):
            time.sleep(0.1)
            try:
                # Check if process still exists (Unix-specific: signal 0 checks existence without killing)
                os.kill(pid, 0)
            except OSError:
                # Process no longer exists
                return {"killed": pid, "error": None}
        
        # If still running, force kill (SIGKILL)
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        
        return {"killed": pid, "error": None}
        
    except ProcessLookupError:
        # Process already gone
        return {"killed": pid, "error": None}
    except PermissionError:
        error_msg = f"Permission denied to kill process {pid} (try running with sudo)"
        return {"killed": None, "error": error_msg}
    except Exception as e:
        error_msg = f"Failed to kill process {pid}: {str(e)}"
        return {"killed": None, "error": error_msg}

def is_port_available(port, host='0.0.0.0'):
    """
    Check if a port is available for binding.
    
    Args:
        port: Port number to check
        host: Host address to bind to (default: '0.0.0.0')
    
    Returns:
        True if port is available, False otherwise
    
    Note:
        Does not set SO_REUSEADDR to ensure accurate detection of ports
        in use (including those in TIME_WAIT state).
    """
    try:
        # Create a socket and try to bind to the port
        # Do not set SO_REUSEADDR to accurately detect port conflicts
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((host, port))
        sock.close()
        return True
    except (socket.error, OSError) as e:
        return False

def wait_for_port_release(port, host='0.0.0.0', max_wait_seconds=10):
    """
    Wait for a port to become available after stopping other processes.
    
    Args:
        port: Port number to wait for
        host: Host address (default: '0.0.0.0')
        max_wait_seconds: Maximum time to wait in seconds
    
    Returns:
        True if port became available, False if timeout
    """
    start_time = time.time()
    wait_interval = 0.5  # Check every 500ms
    
    while time.time() - start_time < max_wait_seconds:
        if is_port_available(port, host):
            elapsed = time.time() - start_time
            return True
        time.sleep(wait_interval)
    
    return False

def attempt_to_free_port(port, host='0.0.0.0'):
    """
    Attempt to free a port by killing the process using it.
    
    Args:
        port: Port number to free
        host: Host address (default: '0.0.0.0')
    
    Returns:
        True if port was freed successfully, False otherwise
    """
    kill_result = kill_process_using_port(port)
    
    if kill_result.get('killed'):
        if wait_for_port_release(port, host, max_wait_seconds=10):
            return True
        else:
            print(f"[ERROR] Port {port} is still in use after killing process")
            print(f"[ERROR] Please manually check what is using port {port}")
            return False
    else:
        print(f"[ERROR] Could not free port {port}: {kill_result.get('error')}")
        return False

# stop_other_app_py() is called inside if __name__ == '__main__' to prevent
# it from running in spawned worker subprocesses (e.g. the kasa_manager
# worker).  When multiprocessing uses the 'spawn' start method, Python
# re-imports this module (app.py) in the child process.  Any module-level
# code that kills processes would kill the parent Flask process.
stopped_info = {"stopped": [], "errors": []}

# --- Utilities --------------------------------------------------------------
def load_json(path, fallback):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return fallback

def save_json(path, data):
    """
    Save data to a JSON file with pretty formatting.
    
    Creates parent directories if they don't exist.
    
    Args:
        path (str): Path to the JSON file
        data: Data to serialize to JSON
        
    Returns:
        bool: True if save succeeded, False if there was an error
    """
    try:
        # Ensure the directory exists
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"[LOG] Error saving JSON to {path}: {e}")
        return False

# --- New: Append batch metadata to batch jsonl ------------------------------
def append_batch_metadata_to_batch_jsonl(color, batch_entry):
    """Append a batch_metadata event to the relevant batch JSONL file."""
    brewid = batch_entry.get("brewid")
    if not color or not brewid:
        return False
    path = batch_jsonl_filename(color, brewid)
    entry = {
        "event": "batch_metadata",
        "payload": dict(batch_entry, timestamp=datetime.utcnow().replace(microsecond=0).isoformat() + "Z")
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return True
    except Exception as e:
        print(f"[LOG] Could not append batch_metadata for {color}: {e}")
        return False

# --- Restricted control-log writer -----------------------------------------
ALLOWED_EVENTS = {
    "tilt_reading": "SAMPLE",
    "temp_control_reading": "TEMP CONTROL READING",
    "heating_on": "HEATING-PLUG TURNED ON",
    "heating_off": "HEATING-PLUG TURNED OFF",
    "cooling_on": "COOLING-PLUG TURNED ON",
    "cooling_off": "COOLING-PLUG TURNED OFF",
    "temp_below_low_limit": "TEMP BELOW LOW LIMIT",
    "temp_above_high_limit": "TEMP ABOVE HIGH LIMIT",
    "temp_in_range": "IN RANGE",
    "temp_control_mode": "MODE_SELECTED",
    "temp_control_mode_changed": "MODE_CHANGED",
    "temp_control_started": "TEMP CONTROL STARTED",
    "temp_control_safety_shutdown": "SAFETY SHUTDOWN - CONTROL TILT INACTIVE",
    "temp_control_blocked_on": "SAFETY - BLOCKED ON COMMAND (NO TILT CONNECTION)",
    "temp_control_safety_off": "SAFETY - TURNING OFF (NO TILT CONNECTION)",
    "kasa_command_timeout": "KASA COMMAND TIMEOUT - PENDING FLAG CLEARED",
}

# Create a set of allowed event values for O(1) lookup performance
ALLOWED_EVENT_VALUES = set(ALLOWED_EVENTS.values())

def _format_control_log_entry(event_type, payload):
    # Use UTC for the ISO timestamp (with Z suffix) for consistency and compatibility
    ts_utc = datetime.utcnow()
    iso_ts = ts_utc.replace(microsecond=0).isoformat() + "Z"
    
    # Use local time for date and time fields so they're readable in the user's timezone
    ts_local = datetime.now()
    date = ts_local.strftime("%Y-%m-%d")
    time_str = ts_local.strftime("%H:%M:%S")

    tilt_color = ""
    try:
        if isinstance(payload, dict):
            tilt_color = payload.get("tilt_color") or payload.get("tilt") or payload.get("color") or ""
    except Exception:
        tilt_color = ""

    def _to_float(val):
        try:
            if val is None or val == "":
                return None
            return float(val)
        except Exception:
            return None

    low = _to_float(payload.get("low_limit") if isinstance(payload, dict) else None)
    high = _to_float(payload.get("high_limit") if isinstance(payload, dict) else None)

    cur = None
    grav = None
    if isinstance(payload, dict):
        cur = payload.get("current_temp")
        if cur is None:
            cur = payload.get("temp_f") if payload.get("temp_f") is not None else payload.get("temp")
        grav = payload.get("gravity") or payload.get("grav") or payload.get("sg")

    cur = _to_float(cur)
    grav = _to_float(grav)

    event_label = ALLOWED_EVENTS.get(event_type, event_type)
    
    # Get brewid from tilt config if we have a tilt_color
    brewid = None
    if tilt_color and 'tilt_cfg' in globals():
        try:
            brewid = tilt_cfg.get(tilt_key_base(tilt_color), {}).get('brewid')
        except Exception:
            brewid = None

    entry = {
        "timestamp": iso_ts,
        "date": date,
        "time": time_str,
        "tilt_color": tilt_color,
        "brewid": brewid,
        "low_limit": low,
        "current_temp": cur,
        "temp_f": cur,
        "gravity": grav,
        "high_limit": high,
        "event": event_label
    }
    return entry

def append_control_log(event_type, payload):
    if event_type not in ALLOWED_EVENTS:
        return
    
    # Check if any controller has heating or cooling enabled
    has_active_controller = False
    if 'temp_cfg' in globals() and 'controllers' in temp_cfg:
        for controller in temp_cfg.get('controllers', []):
            enable_heat = bool(controller.get("enable_heating"))
            enable_cool = bool(controller.get("enable_cooling"))
            if enable_heat or enable_cool:
                has_active_controller = True
                break
    
    if not has_active_controller:
        return
    
    try:
        entry = _format_control_log_entry(event_type, payload or {})
        tilt_color = entry.get("tilt_color", "")
        log_path = _get_control_log_path(tilt_color)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, 'a') as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[LOG] Failed to append to control log: {e}")

def log_periodic_temp_reading():
    """
    Record periodic temperature control readings in memory for all active controllers.
    
    This function is called by periodic_temp_control() after each control loop
    iteration to record temperature readings for the temperature control chart.
    
    The readings are stored in memory (not logged to file) to avoid creating
    excessive log entries. The in-memory buffer is limited to TEMP_READING_BUFFER_SIZE 
    entries (default 1440 = 2 days at 2-minute intervals, prevents memory bloat).
    
    The readings are used for:
    - Chart visualization (/chart_data/TempControl endpoint)
    - Main display (via controller['current_temp'])
    - CSV export if users want granular detail
    
    The readings are logged at the configured update_interval (default 2 minutes),
    which is the same interval at which the temperature control loop runs.
    This is separate from Tilt readings for fermentation monitoring which are 
    logged at tilt_logging_interval_minutes (default 15 minutes).
    
    Unlike file-based event logging, this bypasses the enable_heating/enable_cooling
    gate to ensure readings are recorded whenever temperature control monitoring
    is active, regardless of whether heating or cooling is enabled.
    
    The recorded data includes:
    - Current temperature (temp_f)
    - Low and high temperature limits
    - Tilt color being monitored
    - Controller ID
    - Timestamp
    - Event type: "TEMP CONTROL READING"
    """
    # Record readings for all active controllers
    if 'controllers' not in temp_cfg:
        return
    
    for controller in temp_cfg.get('controllers', []):
        # Only record if this controller's monitoring is active
        if not controller.get("temp_control_active", False):
            continue
        
        try:
            # Create timestamp
            ts = datetime.utcnow()
            iso_ts = ts.replace(microsecond=0).isoformat() + "Z"
            
            # Create reading entry
            entry = {
                "timestamp": iso_ts,
                "date": ts.strftime("%Y-%m-%d"),
                "time": ts.strftime("%H:%M:%S"),
                "controller_id": controller.get("controller_id", 0),
                "tilt_color": controller.get("tilt_color", ""),
                "brewid": None,  # Temperature control readings don't have brewid
                "low_limit": controller.get("low_limit"),
                "current_temp": controller.get("current_temp"),
                "temp_f": controller.get("current_temp"),
                "gravity": None,
                "high_limit": controller.get("high_limit"),
                "event": "TEMP CONTROL READING"
            }
            
            # Add to in-memory buffer (automatically drops oldest when full)
            temp_reading_buffer.append(entry)
            
        except Exception as e:
            print(f"[LOG] Failed to record periodic temp reading for controller {controller.get('controller_id', '?')}: {e}")


@app.template_filter('localtime')
def localtime_filter(iso_str):
    from datetime import datetime, timezone
    try:
        if not iso_str:
            return ''
        if isinstance(iso_str, datetime):
            dt = iso_str
        else:
            s = str(iso_str)
            if s.endswith('Z'):
                try:
                    dt = datetime.fromisoformat(s.rstrip('Z')).replace(tzinfo=timezone.utc)
                except Exception:
                    return s
            else:
                try:
                    dt = datetime.fromisoformat(s)
                except Exception:
                    try:
                        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
                        dt = dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        return s

        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            dt = dt.replace(tzinfo=timezone.utc)

        local_tz = datetime.now().astimezone().tzinfo
        local_dt = dt.astimezone(local_tz)
        return local_dt.strftime('%Y-%m-%d %I:%M:%S %p')
    except Exception:
        return iso_str

# --- Config migration from single to 3-controller format ------------------
def migrate_temp_config_to_multi_controller(old_cfg):
    """
    Convert old single-controller config to new 3-controller array format.
    
    If config already has 'controllers' key, returns it unchanged.
    If config has old format (direct fields), migrates to new format with
    old settings in controller[0] and empty controllers[1] and [2].
    
    Args:
        old_cfg (dict): Loaded config (may be old or new format)
        
    Returns:
        dict: Config in new 3-controller array format
    """
    
    # If already new format, return as-is
    if 'controllers' in old_cfg:
        return old_cfg
    
    # If empty or new install, use template defaults
    if not old_cfg or 'low_limit' not in old_cfg:
        # Load from template
        template_cfg = load_json(TEMP_CFG_FILE + '.template', {})
        if 'controllers' in template_cfg:
            return template_cfg
        # Fallback to creating empty 3-controller structure
        return {
            "controllers": [
                {"controller_id": i, "low_limit": 50, "high_limit": 54, 
                 "enable_heating": False, "enable_cooling": False, "tilt_color": "",
                 "heating_plug": "", "cooling_plug": "", "compressor_delay": 5,
                 "current_temp": None, "heater_on": False, "cooler_on": False,
                 "temp_control_enabled": True, "temp_control_active": False,
                 "mode": "Off", "status": "Not Configured"}
                for i in range(3)
            ]
        }
    
    # Migrate old single-controller format to new format
    print(f"[MIGRATION] Migrating old single-controller config to 3-controller format")
    
    # Create backup of old config
    backup_path = TEMP_CFG_FILE + '.backup'
    save_json(backup_path, old_cfg)
    print(f"[MIGRATION] Backup saved to {backup_path}")
    
    # Move all old settings to controller[0]
    controller_0 = dict(old_cfg, controller_id=0)
    
    # Create empty controllers 1 and 2 with defaults
    default_controller = {
        "controller_id": 0,
        "low_limit": 50,
        "high_limit": 54,
        "enable_heating": False,
        "enable_cooling": False,
        "tilt_color": "",
        "heating_plug": "",
        "cooling_plug": "",
        "compressor_delay": 5,
        "current_temp": None,
        "heater_on": False,
        "cooler_on": False,
        "heater_pending": False,
        "cooler_pending": False,
        "heating_error": False,
        "cooling_error": False,
        "notifications_trigger": False,
        "notification_last_sent": None,
        "notification_comm_failure": False,
        "email_error": False,
        "control_initialized": True,
        "last_logged_low_limit": 50,
        "last_logged_high_limit": 54,
        "last_logged_enable_heating": False,
        "last_logged_enable_cooling": False,
        "warnings_mode": "NONE",
        "temp_control_enabled": True,
        "temp_control_active": False,
        "in_range_trigger_armed": True,
        "above_limit_trigger_armed": True,
        "below_limit_trigger_armed": True,
        "log_temp_control_tilt": True,
        "mode": "Off",
        "status": "Not Configured"
    }
    
    controller_1 = dict(default_controller, controller_id=1)
    controller_2 = dict(default_controller, controller_id=2)
    
    new_cfg = {
        "controllers": [controller_0, controller_1, controller_2]
    }
    
    # Save migrated config
    save_json(TEMP_CFG_FILE, new_cfg)
    print(f"[MIGRATION] Migration complete. New config saved to {TEMP_CFG_FILE}")
    
    return new_cfg

# --- Load configs ----------------------------------------------------------
tilt_cfg = load_json(TILT_CONFIG_FILE, {})
temp_cfg_raw = load_json(TEMP_CFG_FILE, {})
temp_cfg_raw = migrate_temp_config_to_multi_controller(temp_cfg_raw)
temp_cfg = temp_cfg_raw
system_cfg = load_json(SYSTEM_CFG_FILE, {})
tilt_table = load_tilt_table()  # MAC-keyed device registry (persists calibration by device)


def _parse_plug_port(value):
    """Return an int port in 1-65535, or None if blank/invalid."""
    try:
        p = int(value)
        return p if 1 <= p <= 65535 else None
    except (TypeError, ValueError):
        return None


def ensure_temp_defaults_for_controller(controller):
    """
    Ensure a single controller dict has all required default fields.
    
    Args:
        controller (dict): Controller configuration dictionary
    """
    controller.setdefault("current_temp", None)
    # CRITICAL FIX for issue #289: Handle None values from corrupted config files
    # If limits are explicitly set to None in the config file, setdefault won't replace them
    # because the key exists. We need to explicitly check for None and reset to defaults.
    # ALSO validate that limits are numeric types - if strings, try to convert; if invalid, reset to 0.0
    # This ensures controller always contains the ACTUAL values used by control logic
    low_val = controller.get("low_limit")
    if low_val is None:
        controller["low_limit"] = 0.0
    elif isinstance(low_val, (int, float)):
        controller["low_limit"] = float(low_val)  # Ensure it's float, not int
    else:
        # Try to convert string to float
        try:
            controller["low_limit"] = float(low_val)
        except (ValueError, TypeError):
            print(f"[LOG] WARNING: low_limit cannot be converted to float (type={type(low_val).__name__}, value={low_val}), resetting to 0.0")
            controller["low_limit"] = 0.0
    
    high_val = controller.get("high_limit")
    if high_val is None:
        controller["high_limit"] = 0.0
    elif isinstance(high_val, (int, float)):
        controller["high_limit"] = float(high_val)  # Ensure it's float, not int
    else:
        # Try to convert string to float
        try:
            controller["high_limit"] = float(high_val)
        except (ValueError, TypeError):
            print(f"[LOG] WARNING: high_limit cannot be converted to float (type={type(high_val).__name__}, value={high_val}), resetting to 0.0")
            controller["high_limit"] = 0.0
    controller.setdefault("enable_heating", False)
    controller.setdefault("enable_cooling", False)
    controller.setdefault("heating_plug", "")
    controller.setdefault("cooling_plug", "")
    controller.setdefault("heating_plug_port", None)
    controller.setdefault("cooling_plug_port", None)
    controller.setdefault("heater_on", False)
    controller.setdefault("cooler_on", False)
    controller.setdefault("heater_pending", False)
    controller.setdefault("cooler_pending", False)
    controller.setdefault("heater_pending_since", None)
    controller.setdefault("cooler_pending_since", None)
    controller.setdefault("heating_error", False)
    controller.setdefault("cooling_error", False)
    controller.setdefault("heating_error_notified", False)
    controller.setdefault("cooling_error_notified", False)
    controller.setdefault("heating_kasa_error_since", 0)       # epoch when heating error first started (0 = no error)
    controller.setdefault("cooling_kasa_error_since", 0)       # epoch when cooling error first started (0 = no error)
    controller.setdefault("heating_kasa_error_notified_at", 0) # epoch of last heating error notification
    controller.setdefault("cooling_kasa_error_notified_at", 0) # epoch of last cooling error notification
    controller.setdefault("notifications_trigger", False)
    controller.setdefault("notification_last_sent", None)
    controller.setdefault("notification_comm_failure", False)
    controller.setdefault("push_error", False)
    controller.setdefault("email_error", False)
    controller.setdefault("control_initialized", False)
    controller.setdefault("last_logged_low_limit", controller.get("low_limit"))
    controller.setdefault("last_logged_high_limit", controller.get("high_limit"))
    controller.setdefault("last_logged_enable_heating", controller.get("enable_heating"))
    controller.setdefault("last_logged_enable_cooling", controller.get("enable_cooling"))
    controller.setdefault("mode", "--")
    # New flag to turn on/off the entire temp-control UI and behavior:
    controller.setdefault("temp_control_enabled", True)
    # New flag to control active monitoring/recording (user-controlled switch):
    # Preserve the saved state so that controllers that were ON before an
    # unplanned interruption (power failure, crash) restart automatically.
    # Graceful shutdown (exit_system) sets this to False before exiting, so
    # controllers always start OFF after a normal shutdown.
    controller.setdefault("temp_control_active", False)
    # Trigger states for event-based logging:
    controller.setdefault("in_range_trigger_armed", True)
    controller.setdefault("above_limit_trigger_armed", True)
    controller.setdefault("below_limit_trigger_armed", True)
    # Swapped plug detection:
    controller.setdefault("heater_baseline_temp", None)
    controller.setdefault("heater_baseline_time", None)
    controller.setdefault("cooler_baseline_temp", None)
    controller.setdefault("cooler_baseline_time", None)
    controller.setdefault("swapped_plugs_detected", False)
    controller.setdefault("swapped_plugs_notified", False)
    controller.setdefault("swapped_plug_type", "")  # "heating" or "cooling"

def ensure_temp_defaults():
    """Ensure all 3 controllers have required default fields."""
    if 'controllers' in temp_cfg:
        for controller in temp_cfg.get('controllers', []):
            ensure_temp_defaults_for_controller(controller)

ensure_temp_defaults()

def ensure_all_tilts():
    try:
        for color in TILT_UUIDS.values():
            if color not in tilt_cfg:
                tilt_cfg[color] = {
                    "beer_name": "",
                    "batch_name": "",
                    "ferm_start_date": "",
                    "recipe_og": "",
                    "recipe_fg": "",
                    "recipe_abv": "",
                    "actual_og": None,
                    "brewid": "",
                    "og_confirmed": False
                }
    except Exception:
        pass

ensure_all_tilts()

_TZ_ABBREV_MAP = {
    'EST': 'America/New_York',
    'EDT': 'America/New_York',
    'CST': 'America/Chicago',
    'CDT': 'America/Chicago',
    'MST': 'America/Denver',
    'MDT': 'America/Denver',
    'PST': 'America/Los_Angeles',
    'PDT': 'America/Los_Angeles',
    'UTC': 'UTC'
}
tz = (system_cfg.get('timezone') if isinstance(system_cfg, dict) else None) or os.environ.get('TZ') or 'UTC'
tz = _TZ_ABBREV_MAP.get(tz, tz)
os.environ['TZ'] = tz
try:
    time.tzset()
except Exception:
    pass

# --- Kasa manager (single subprocess for all controllers/plugs) -----------
# KasaManager replaces the old per-controller kasa_queues[], kasa_result_queues[],
# kasa_procs[], and _kasa_proc_locks[] arrays.  It is initialised in
# __main__ after multiprocessing.set_start_method('spawn') is called.
_NUM_KASA_CONTROLLERS = 3
kasa_manager = KasaManager() if _kasa_manager_available else None

# Event set by _background_startup_sync when the initial plug-state query has
# finished (success or failure).  periodic_temp_control waits on this before
# running its first control cycle so that we don't issue kasa commands before
# we know the actual plug states.
_startup_sync_complete = threading.Event()

# --- Tilt Pro / mini-pro detection constants -------------------------------
# Tilt Pro and Tilt mini-pro broadcast gravity in 10000x encoding (vs standard 1000x).
# A raw gravity value above this threshold identifies a Pro device.
TILT_PRO_GRAVITY_THRESHOLD = 5000
TILT_PRO_GRAVITY_DIVISOR = 10000.0
TILT_PRO_TEMP_DIVISOR = 10.0
TILT_STANDARD_GRAVITY_DIVISOR = 1000.0

# --- Live runtime data -----------------------------------------------------
live_tilts = {}
tilt_status = {}

last_tilt_log_ts = {}
batch_notification_state = {}  # Track notification state per tilt/brewid

# Rate-limiter for tilt_table saves: epoch timestamp of last save (0 = never saved)
_tilt_table_last_saved: float = 0
_TILT_TABLE_SAVE_INTERVAL_SECONDS = 300  # Save at most every 5 minutes

# Notification timing constants
DAILY_REPORT_COOLDOWN_HOURS = 23  # Prevent duplicate daily reports (allows timing variance)
DAILY_REPORT_WINDOW_MINUTES = 5   # Time window for daily report triggering
BATCH_MONITORING_INTERVAL_SECONDS = 300  # Check signal loss and daily reports every 5 minutes

# Notification retry constants (Option A: Auto-retry with exponential backoff)
NOTIFICATION_MAX_RETRIES = 2  # Maximum retry attempts (total: 1 initial + 2 retries = 3 attempts)
NOTIFICATION_RETRY_INTERVALS = [300, 1800]  # Retry after 5 minutes, then 30 minutes (in seconds)
notification_retry_queue = []  # Queue of failed notifications pending retry

# Pending notification queue (for deduplication)
NOTIFICATION_PENDING_DELAY_SECONDS = 10  # Delay before sending to allow deduplication
pending_notifications = []  # Queue of notifications pending send with 10-second delay

def generate_brewid(beer_name, batch_name, date_str):
    id_str = f"{beer_name}-{batch_name}-{date_str}"
    return hashlib.sha256(id_str.encode('utf-8')).hexdigest()[:8]

# --- Tilt-key helpers -------------------------------------------------------
# A tilt key is either:
#   "Color"      – a plain color name, e.g. "Black"
#   "Color:MAC"  – a color+MAC composite, e.g. "Black:AA:BB:CC:DD:EE:FF"
# Composite keys let controllers target a specific physical Tilt device when
# multiple devices of the same color (standard vs Pro/mini-pro) are present.

def tilt_key_base(key: str) -> str:
    """Return the base color from a tilt key that may be 'Color' or 'Color:MAC'."""
    return key.split(':', 1)[0] if key else key

def tilt_key_mac(key: str):
    """Return the MAC portion from a composite tilt key 'Color:MAC', or None."""
    if not key:
        return None
    parts = key.split(':', 1)
    return parts[1] if len(parts) == 2 else None

def get_tilt_color_hex(tilt_key: str) -> str:
    """Return the CSS hex color for a tilt_key (plain color OR 'Color:MAC' composite)."""
    return COLOR_MAP.get(tilt_key_base(tilt_key), '#888') if tilt_key else '#888'

def tilt_display_label(tilt_key: str, live_info: dict = None) -> str:
    """Return a human-readable label for a tilt key.

    Examples:
        "Black"                    → "Black"
        "Black:AA:BB:CC:DD:EE:FF"  → "Black (Pro) — AA:BB:CC:DD:EE:FF"
    """
    if not tilt_key:
        return ''
    base = tilt_key_base(tilt_key)
    mac  = tilt_key_mac(tilt_key)
    if not mac:
        return base
    type_label = ''
    if live_info is not None:
        if live_info.get('is_pro'):
            type_label = ' (Pro)'
        else:
            type_label = ' (Standard)'
    return f"{base}{type_label} \u2014 {mac}"

def update_live_tilt(color, gravity, temp_f, rssi, mac_address=None, is_pro=False):
    global _tilt_table_last_saved
    cfg = tilt_cfg.get(color, {})
    # Preserve existing MAC address if a new (non-None, non-empty) one is not provided
    existing_mac = live_tilts.get(color, {}).get("mac_address", "")
    resolved_mac = mac_address if mac_address else existing_mac

    # Apply calibration variances — prefer MAC-keyed tilt_table (survives color reassignment),
    # fall back to color-keyed tilt_cfg for backwards compat.
    if resolved_mac and normalize_mac(resolved_mac) in tilt_table:
        temp_variance, gravity_variance = get_device_variances(tilt_table, mac=resolved_mac)
    else:
        temp_variance    = float(cfg.get("temp_variance",    0) or 0)
        gravity_variance = float(cfg.get("gravity_variance", 0) or 0)

    raw_gravity = round(gravity, 3) if gravity is not None else None
    adj_temp_f  = round(temp_f    + temp_variance,    1) if temp_f    is not None else None
    adj_gravity = round(raw_gravity + gravity_variance, 3) if raw_gravity is not None else None

    tilt_entry = {
        "gravity": round(gravity, 3) if gravity is not None else None,
        "temp_f": temp_f,
        "rssi": rssi,
        "timestamp": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "color_code": COLOR_MAP.get(color, "#333"),
        "beer_name": cfg.get("beer_name", ""),
        "batch_name": cfg.get("batch_name", ""),
        "brewid": cfg.get("brewid", ""),
        "recipe_og": cfg.get("recipe_og", ""),
        "recipe_fg": cfg.get("recipe_fg", ""),
        "recipe_abv": cfg.get("recipe_abv", ""),
        "actual_og": cfg.get("actual_og", ""),
        "og_confirmed": cfg.get("og_confirmed", False),
        "original_gravity": cfg.get("actual_og", 0),
        "mac_address": resolved_mac,
        "is_pro": is_pro,
        # Calibration
        "temp_variance":    temp_variance,
        "gravity_variance": gravity_variance,
        "adj_temp_f":       adj_temp_f,
        "adj_gravity":      adj_gravity,
    }

    # Write to the plain color key (backward compatibility, used when no MAC qualifier set)
    live_tilts[color] = tilt_entry

    # Also write to the MAC-qualified composite key so controllers that reference a
    # specific physical device (e.g. tilt_color = "Black:AA:BB:CC:DD:EE:FF") can
    # look up temperature readings directly.
    if resolved_mac:
        mac_n = normalize_mac(resolved_mac)
        if mac_n:
            live_tilts[f"{color}:{mac_n}"] = tilt_entry

    # Populate the MAC-keyed tilt_table so per-device calibration persists.
    # upsert_device_from_reading() is the single place that creates/updates table entries;
    # it was previously imported but never called from the BLE read path.
    if resolved_mac:
        try:
            uuid_str = next((k for k, v in TILT_UUIDS.items() if v == color), "")
            tilt_type = "pro" if is_pro else "standard"
            rec = upsert_device_from_reading(
                tilt_table,
                mac=resolved_mac,
                tilt_color=color,
                uuid=uuid_str,
                rssi=rssi,
                temp_f=temp_f,
                gravity=gravity,
            )
            # Store detected type on the record (upsert_device_from_reading preserves it if set)
            if rec.get("tilt_type", "unknown") == "unknown":
                rec["tilt_type"] = tilt_type
            # Rate-limited save: at most every _TILT_TABLE_SAVE_INTERVAL_SECONDS
            now_ts = time.time()
            if now_ts - _tilt_table_last_saved >= _TILT_TABLE_SAVE_INTERVAL_SECONDS:
                save_tilt_table(tilt_table)
                _tilt_table_last_saved = now_ts
        except Exception as e:
            print(f"[LOG] Error updating tilt_table for {color}/{resolved_mac}: {e}")

def get_active_tilts():
    """
    Filter live_tilts to only include tilts that have sent data recently.
    
    Returns:
        dict: Dictionary of active tilts (those within the inactivity timeout)
    """
    # Get timeout from system config, default to 30 minutes
    timeout_minutes = int(system_cfg.get('tilt_inactivity_timeout_minutes', 30))
    now = datetime.utcnow()
    active_tilts = {}
    
    for color, info in live_tilts.items():
        # Composite-key entries (e.g. "Black:AA:BB:CC:DD:EE:FF") are internal lookups
        # for MAC-qualified controllers.  Exclude them from the display dict — the
        # plain-color entry already represents that device for UI/dashboard purposes.
        if ':' in color:
            continue
        timestamp_str = info.get('timestamp')
        if not timestamp_str:
            # No timestamp means we can't determine activity - exclude for safety
            continue
        
        try:
            # Parse ISO 8601 timestamp (remove 'Z' suffix for naive UTC datetime)
            # This is consistent with how timestamps are created: datetime.utcnow().isoformat() + "Z"
            timestamp = datetime.fromisoformat(timestamp_str.rstrip('Z'))
            
            elapsed_minutes = (now - timestamp).total_seconds() / 60.0
            
            if elapsed_minutes < timeout_minutes:
                active_tilts[color] = info
        except Exception as e:
            # Unable to parse timestamp - likely corrupted data, exclude from display
            print(f"[LOG] Error parsing timestamp for {color}: {e}, excluding from active tilts")
    
    return active_tilts

def _find_controller_by_tilt(color, mac=None):
    """
    Return the controller whose tilt matches *color* (and optionally *mac*), or ``None``.

    Match priority (highest to lowest):
    1. Exact composite key match: controller.tilt_color == "Color:MAC"
       (used when a controller is configured for a specific physical device)
    2. Exact plain-color match: controller.tilt_color == "Color"
       (backward compat: any device of this color)
    3. Prefix match when no MAC is supplied: controller.tilt_color starts with "Color:"
       (for callers that only have the color, finds a MAC-qualified controller)
    """
    mac_n = normalize_mac(mac) if mac else None
    composite_key = f"{color}:{mac_n}" if mac_n else None

    # Pass 1: exact composite-key match (MAC-specific assignment)
    if composite_key:
        for ctrl in temp_cfg.get('controllers', []):
            if ctrl.get('tilt_color') == composite_key:
                return ctrl

    # Pass 2: exact plain-color match (any device of this color)
    for ctrl in temp_cfg.get('controllers', []):
        if ctrl.get('tilt_color') == color:
            return ctrl

    # Pass 3: prefix match — when only color is known, still find a MAC-qualified controller
    # (This helps when legacy callers don't pass a MAC, e.g. some notification paths)
    if not mac_n:
        for ctrl in temp_cfg.get('controllers', []):
            tc = ctrl.get('tilt_color', '')
            if tc.startswith(f"{color}:"):
                return ctrl

    return None

def get_control_tilt_color(controller):
    """
    Get the color of the Tilt currently being used for temperature control.
    
    Important behavior:
    - If a Tilt is explicitly assigned (tilt_color is set), ALWAYS return that color,
      even if the Tilt is currently offline or inactive. This ensures safety shutdown
      triggers when the assigned Tilt is not operational.
    - If no Tilt is assigned (tilt_color is empty), use fallback logic to find any
      available Tilt with temperature data.
    
    Args:
        controller (dict): Controller configuration dictionary
    
    Returns:
        str: The color of the Tilt being used, or None if no Tilt is being used.
    """
    # First check if a Tilt is explicitly assigned
    color = controller.get("tilt_color")
    if color:
        # If a Tilt is explicitly assigned, ALWAYS return it
        # Even if it's not in live_tilts (offline/inactive)
        # This ensures safety shutdown triggers when assigned Tilt is not operational
        # We do NOT fall back to another Tilt in this case
        return color
    
    # If no explicit assignment (tilt_color is empty), check if we're using a fallback Tilt
    # This happens when tilt_color is empty but we still get temp from a Tilt
    for tilt_color, info in live_tilts.items():
        if info.get("temp_f") is not None:
            return tilt_color
    
    return None

def get_current_temp_for_control_tilt(controller):
    """
    Get the current temperature from the Tilt assigned to temperature control.
    
    Important behavior:
    - If a Tilt is explicitly assigned, ONLY use that Tilt's temperature.
    - Do NOT fall back to another Tilt if the assigned one is offline/inactive.
    - If no Tilt is assigned, use fallback logic to get temp from any available Tilt.
    
    Args:
        controller (dict): Controller configuration dictionary
    
    Returns:
        float: Temperature in Fahrenheit, or None if no temperature available.
    """
    color = controller.get("tilt_color")
    if color:
        # Tilt is explicitly assigned - ONLY use that Tilt.
        # When the assigned key is a composite "Color:MAC", also try the plain
        # color key as a fallback so a brief gap in composite-key writes (e.g.
        # right after app restart before the first mini-pro broadcast) does not
        # wrongly return None and trigger the safety shutdown.
        for key in [color, tilt_key_base(color)]:
            if key in live_tilts:
                return live_tilts[key].get("temp_f")
        # Assigned Tilt is not available in live_tilts at all - return None.
        # This will trigger safety shutdown.
        return None
    
    # No explicit assignment - use fallback logic
    # Return temperature from any available Tilt
    for info in live_tilts.values():
        if info.get("temp_f") is not None:
            return info.get("temp_f")
    return None

def is_control_tilt_active(controller):
    """
    Check if the Tilt being used for temperature control is currently active.
    
    For temperature control safety, uses a shorter timeout than general Tilt monitoring:
    - Temperature control timeout: 2 × update_interval + 1 min buffer (default: 2×2+1 = 5 min)
    - The extra minute prevents false shutdowns when the mini-pro's broadcast interval
      aligns with the 60-second BLE scanner restart cycle.
    - This ensures KASA plugs turn off quickly if Tilt signal is lost
    - Much shorter than the general 30-minute inactivity timeout used for display/notifications
    
    Grace period for newly assigned Tilts:
    - When a Tilt is first assigned to temp control, there's a 15-minute grace period
    - During this grace period, the system allows time for the Tilt to start broadcasting
    - This prevents immediate shutdown when setting up a new batch
    - After grace period, normal 4-minute timeout applies
    
    This includes both explicitly assigned Tilts (via tilt_color setting) and
    fallback Tilts (when tilt_color is empty but temperature is sourced from a Tilt).
    
    Args:
        controller (dict): Controller configuration dictionary
    
    Returns:
        bool: True if the control Tilt is active (within temp control timeout) OR if no Tilt is being used.
              False only if a Tilt is being used for control but is inactive (safety shutdown condition).
    """
    # Get the color of the Tilt actually being used for control
    control_color = get_control_tilt_color(controller)
    
    if not control_color:
        # No Tilt is being used for temp control - allow control to proceed
        # (temperature might be set manually)
        return True
    
    # Check if we're in the grace period for a newly assigned Tilt
    # Grace period: 15 minutes from when Tilt was assigned to temp control
    assignment_timestamp = controller.get("tilt_assignment_time")
    if assignment_timestamp:
        try:
            from datetime import datetime
            assignment_time = datetime.fromisoformat(assignment_timestamp)
            now = datetime.utcnow()
            minutes_since_assignment = (now - assignment_time).total_seconds() / 60.0
            
            # Grace period: 15 minutes
            grace_period_minutes = 15
            
            if minutes_since_assignment < grace_period_minutes:
                # We're in grace period - allow control to proceed even if Tilt is inactive
                # This gives time for Tilt to start broadcasting and for user to complete setup
                print(f"[TEMP_CONTROL] Grace period active: {minutes_since_assignment:.1f}/{grace_period_minutes} minutes elapsed")
                return True
        except Exception as e:
            print(f"[LOG] Error checking assignment time: {e}")
            # If we can't parse the assignment time, continue with normal checks
    
    # For temperature control, use a much shorter timeout than general monitoring
    # Timeout = 2 × update_interval (2 missed readings)
    # Example: 2 min update interval → 4 min timeout
    try:
        update_interval_minutes = int(system_cfg.get("update_interval", 2))
    except Exception:
        update_interval_minutes = 2
    
    # Temperature control timeout: 2 missed readings + 1-minute buffer.
    # The +1 buffer prevents false safety-shutdowns when the mini-pro's broadcast
    # interval aligns with the 60-second BLE scanner restart cycle, which can
    # create a gap that reaches exactly the 2× boundary.
    temp_control_timeout_minutes = update_interval_minutes * 2 + 1

    # Check if the control Tilt has sent data within the temp control timeout.
    # When tilt_color is a composite key ("Color:MAC") fall back to the plain
    # color key so a brief gap in composite-key writes (e.g. right after app
    # restart, before the first mini-pro broadcast) does not falsely trigger the
    # safety shutdown while the plain-key entry is still fresh.
    tilt_key = control_color
    if tilt_key not in live_tilts:
        tilt_key = tilt_key_base(control_color)
        if tilt_key not in live_tilts:
            return False

    tilt_info = live_tilts[tilt_key]
    timestamp_str = tilt_info.get('timestamp')
    if not timestamp_str:
        return False
    
    try:
        from datetime import datetime
        timestamp = datetime.fromisoformat(timestamp_str.rstrip('Z'))
        now = datetime.utcnow()
        elapsed_minutes = (now - timestamp).total_seconds() / 60.0
        
        # Tilt is active if it's within the temp control timeout
        return elapsed_minutes < temp_control_timeout_minutes
    except Exception as e:
        print(f"[LOG] Error checking control Tilt activity for {control_color}: {e}")
        # If we can't determine activity, assume inactive for safety
        return False

def log_tilt_reading(color, gravity, temp_f, rssi, mac="", is_pro=False):
    """
    Log tilt readings with interval-based rate limiting and batch tracking.
    
    This function handles:
    - Rate-limited logging based on tilt usage:
      * Temperature control tilts: use system_cfg['update_interval'] (configurable, default 2 min)
      * Fermentation tracking tilts: use system_cfg['tilt_logging_interval_minutes'] (configurable, default 15 min)
    - Recording readings to control log and batch-specific JSONL files
    - Forwarding to third-party services if configured
    - Triggering batch notifications (signal loss, fermentation start, etc.)
    
    Args:
        color: Tilt color identifier
        gravity: Specific gravity reading
        temp_f: Temperature in Fahrenheit
        rssi: Bluetooth signal strength
        mac: Bluetooth MAC address of the Tilt device
        is_pro: True if this is a Tilt Pro or Tilt mini-pro (high-precision format)
    """
    cfg = tilt_cfg.get(color, {})
    brewid = cfg.get('brewid', '')
    
    # Rate limiting based on tilt usage:
    # - If tilt is assigned to temperature control: use system_cfg['update_interval'] for responsive control
    # - Otherwise: use system_cfg['tilt_logging_interval_minutes'] for fermentation tracking
    # Both intervals are configurable in System Settings page
    # Multi-controller: use helper to find the controller owning this tilt (if any).
    # Pass the MAC address so a MAC-qualified controller is found first.
    control_controller = _find_controller_by_tilt(color, mac=mac)
    is_control_tilt = control_controller is not None
    
    if is_control_tilt:
        # Use system_cfg['update_interval'] for temperature control tilt
        # This keeps control tilt logging synchronized with the temperature control loop
        try:
            interval_minutes = int(system_cfg.get('update_interval', 2))
        except (ValueError, TypeError):
            interval_minutes = 2  # Fallback if not configured or invalid
    else:
        # Use system_cfg['tilt_logging_interval_minutes'] for fermentation tracking
        # This is the "Tilt Reading Logging Interval" setting in System Settings
        try:
            interval_minutes = int(system_cfg.get('tilt_logging_interval_minutes', 15))
        except (ValueError, TypeError):
            interval_minutes = 15  # Fallback if not configured or invalid
    
    now = datetime.utcnow()
    # Rate limit per MAC address so multiple tilts of the same color each log independently
    rate_key = mac if mac else color
    last_log = last_tilt_log_ts.get(rate_key)
    
    if last_log:
        elapsed = (now - last_log).total_seconds() / 60.0
        if elapsed < interval_minutes:
            return
    
    last_tilt_log_ts[rate_key] = now
    
    # Create payload
    payload = {
        "timestamp": now.replace(microsecond=0).isoformat() + "Z",
        "tilt_color": color,
        "gravity": round(gravity, 3) if gravity is not None else None,
        "temp_f": temp_f,
        "rssi": rssi,
        "beer_name": cfg.get("beer_name", ""),
        "batch_name": cfg.get("batch_name", ""),
        "brewid": brewid,
        "recipe_og": cfg.get("recipe_og", ""),
        "actual_og": cfg.get("actual_og"),
        "og_confirmed": cfg.get("og_confirmed", False),
        "mac": mac,
        "is_pro": is_pro,
    }
    
    # Include temperature control limits in the payload if this is the control tilt
    # This ensures the control log has complete information for debugging and charting
    if is_control_tilt:
        payload["low_limit"] = control_controller.get("low_limit")
        payload["high_limit"] = control_controller.get("high_limit")
    
    # Log to control log
    append_control_log("tilt_reading", payload)
    
    # Log to batch-specific jsonl
    if brewid:
        append_sample_to_batch_jsonl(color, brewid, payload)
    
    # Forward to third-party if configured
    forward_to_third_party_if_configured(payload)
    
    # Track batch notification state and check triggers
    check_batch_notifications(color, gravity, temp_f, brewid, cfg)

def detection_callback(device, advertisement_data):
    try:
        mfg_data = advertisement_data.manufacturer_data
        if not mfg_data:
            return
        # Tilt uses Apple iBeacon format: manufacturer ID 0x004C (76).
        # Explicitly look up the Apple entry rather than taking the first dict
        # value, so that devices with multiple manufacturer data entries
        # (e.g. Tilt mini-pro) are still decoded correctly.
        raw = mfg_data.get(76)
        if not raw:
            return
        if len(raw) < 22:
            return
        uuid = raw[2:18].hex()
        color = TILT_UUIDS.get(uuid) or TILT_UUIDS.get(uuid.lower()) or TILT_UUIDS.get(uuid.upper())
        if not color:
            return
        try:
            raw_temp = int.from_bytes(raw[18:20], byteorder='big')
            raw_gravity = int.from_bytes(raw[20:22], byteorder='big')
            # Tilt Pro / Tilt mini-pro broadcasts with higher precision:
            # raw gravity > TILT_PRO_GRAVITY_THRESHOLD indicates the 10000x encoding (vs standard 1000x)
            is_pro = raw_gravity > TILT_PRO_GRAVITY_THRESHOLD
            if is_pro:
                temp_f = round(raw_temp / TILT_PRO_TEMP_DIVISOR, 1)
                gravity = raw_gravity / TILT_PRO_GRAVITY_DIVISOR
            else:
                temp_f = raw_temp
                gravity = raw_gravity / TILT_STANDARD_GRAVITY_DIVISOR
        except Exception:
            return
        rssi = advertisement_data.rssi
        update_live_tilt(color, gravity, temp_f, rssi, mac_address=device.address, is_pro=is_pro)
        try:
            log_tilt_reading(color, gravity, temp_f, rssi, mac=device.address, is_pro=is_pro)
        except Exception as log_err:
            print(f"[BLE] log_tilt_reading failed for {color}: {log_err}")
    except Exception as e:
        print("[BLE] detection_callback exception:", e)

# --- Batch rotation / archival (legacy, kept for compatibility) ------------
def rotate_and_archive_old_history(color, old_brewid, old_cfg):
    try:
        if not old_brewid and not color:
            return False
        os.makedirs(BATCHES_DIR, exist_ok=True)
        archive_name = f"{color}_{old_cfg.get('beer_name','unknown')}_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        safe_archive = os.path.join(BATCHES_DIR, archive_name.replace(' ', '_'))
        moved = 0
        remaining_lines = []
        color_log_path = _get_control_log_path(color)
        if os.path.exists(color_log_path):
            with open(color_log_path, 'r') as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        remaining_lines.append(line)
                        continue
                    if obj.get('event') != 'SAMPLE':
                        remaining_lines.append(line)
                        continue
                    payload = obj or {}
                    if isinstance(payload, dict) and payload.get('brewid') == old_brewid:
                        with open(safe_archive, 'a') as af:
                            af.write(json.dumps(obj) + "\n")
                        moved += 1
                    else:
                        remaining_lines.append(line)
        try:
            with open(color_log_path, 'w') as f:
                f.writelines(remaining_lines)
        except Exception as e:
            print(f"[LOG] Error rewriting log after archive: {e}")

        # Only log mode change if we actually archived samples
        if moved > 0:
            # Find the controller assigned to this tilt color to get accurate limit data
            ctrl_for_color = _find_controller_by_tilt(color) or {}
            append_control_log("temp_control_mode_changed", {"tilt_color": color, "low_limit": ctrl_for_color.get("low_limit"), "current_temp": ctrl_for_color.get("current_temp"), "high_limit": ctrl_for_color.get("high_limit")})
        return True
    except Exception as e:
        print(f"[LOG] rotate_and_archive_old_history error: {e}")
        return False

# --- batches: per-batch jsonl helpers --------------------------------------
def ensure_batches_dir():
    try:
        os.makedirs(BATCHES_DIR, exist_ok=True)
    except Exception as e:
        print(f"[LOG] Could not create batches dir {BATCHES_DIR}: {e}")

def normalize_to_mmddyyyy(date_str):
    if not date_str:
        return datetime.utcnow().strftime("%m%d%Y")
    for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d", "%m%d%Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%m%d%Y")
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%m%d%Y")
    except Exception:
        return datetime.utcnow().strftime("%m%d%Y")

def normalize_to_yyyymmdd(date_str):
    """Convert various date formats to YYYYmmdd format."""
    if not date_str:
        return datetime.utcnow().strftime("%Y%m%d")
    for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d", "%m%d%Y", "%Y%m%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y%m%d")
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%d")
    except Exception:
        return datetime.utcnow().strftime("%Y%m%d")

def sanitize_filename(name):
    """Sanitize a string for use in a filename."""
    # Replace invalid characters with underscore
    invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t']
    result = name
    for char in invalid_chars:
        result = result.replace(char, '_')
    # Also replace spaces and remove control characters
    result = result.replace(' ', '_')
    # Remove ASCII control characters (0-31)
    result = ''.join(c if ord(c) >= 32 else '_' for c in result)
    # Limit length to avoid overly long filenames
    return result[:MAX_FILENAME_LENGTH]

def batch_jsonl_filename(color, brewid, created_date_mmddyyyy=None, beer_name=None, batch_name=None):
    """Generate batch JSONL filename in format: brewname_YYYYmmdd_brewid.jsonl
    
    First searches for an existing file containing the brewid.
    If found, returns that file to prevent multiple files for the same batch.
    If not found, generates a new filename.
    """
    ensure_batches_dir()
    bid = (brewid or "unknown")
    
    # First, search for any existing file that contains this brewid
    # Match brewid as complete token: either whole filename or preceded by underscore
    try:
        for fn in os.listdir(BATCHES_DIR):
            if not fn.endswith('.jsonl'):
                continue
            # Remove .jsonl extension for matching
            name_without_ext = fn.removesuffix('.jsonl')
            # Match if brewid is the entire name, or ends with _brewid
            # This ensures exact token matching: "abc" matches "abc.jsonl" or "name_abc.jsonl"
            # but NOT "xyzabc.jsonl" (no underscore separator before brewid)
            if name_without_ext == bid or name_without_ext.endswith(f"_{bid}"):
                # Found an existing file with this brewid
                existing_path = os.path.join(BATCHES_DIR, fn)
                print(f"[BATCH] Found existing batch file for brewid {bid}: {fn}")
                return existing_path
    except Exception as e:
        print(f"[BATCH] Error searching for existing batch file: {e}")
    
    # No existing file found, generate a new filename
    # Get beer_name from tilt config if not provided
    if beer_name is None:
        cfg = tilt_cfg.get(color, {})
        beer_name = cfg.get("beer_name", "")
    
    # Create filename with brew name, date, and brewid
    if beer_name:
        safe_beer_name = sanitize_filename(beer_name)
    else:
        safe_beer_name = "Batch"
    
    # Convert date to YYYYmmdd format
    if created_date_mmddyyyy:
        date_yyyymmdd = normalize_to_yyyymmdd(created_date_mmddyyyy)
    else:
        date_yyyymmdd = datetime.utcnow().strftime("%Y%m%d")
    
    fname = f"{safe_beer_name}_{date_yyyymmdd}_{bid}.jsonl"
    print(f"[BATCH] Creating new batch file for brewid {bid}: {fname}")
    return os.path.join(BATCHES_DIR, fname)

def ensure_batch_jsonl_exists(color, brewid, meta=None, created_date_mmddyyyy=None):
    beer_name = None
    if meta and isinstance(meta, dict):
        beer_name = meta.get("beer_name", "")
    if not beer_name:
        cfg = tilt_cfg.get(color, {})
        beer_name = cfg.get("beer_name", "")
    
    path = batch_jsonl_filename(color, brewid, created_date_mmddyyyy=created_date_mmddyyyy, beer_name=beer_name)
    if not os.path.exists(path):
        # Try to migrate legacy files (both old formats)
        try:
            # Try pattern 1: batch_{COLOR}_{brewid}_
            legacy_pattern1 = f"batch_{(color or '').upper()}_{brewid}_"
            # Try pattern 2: {brewid}.jsonl
            legacy_pattern2 = f"{brewid}.jsonl"
            
            migrated = False
            for fn in os.listdir(BATCHES_DIR):
                if migrated:
                    break
                if fn.startswith(legacy_pattern1) or fn == legacy_pattern2:
                    legacy_path = os.path.join(BATCHES_DIR, fn)
                    try:
                        os.rename(legacy_path, path)
                        print(f"[MIGRATE] Renamed legacy {legacy_path} -> {path}")
                        migrated = True
                    except Exception as e:
                        print(f"[MIGRATE] Could not rename {legacy_path} -> {path}: {e}")
        except Exception as e:
            print(f"[MIGRATE] Migration scan failed: {e}")
        try:
            header = {
                "event": "batch_metadata",
                "payload": {
                    "tilt_color": color,
                    "brewid": brewid,
                    "created_date": (created_date_mmddyyyy or datetime.utcnow().strftime("%m%d%Y")),
                    "meta": meta or {}
                }
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(header) + "\n")
        except Exception as e:
            print(f"[LOG] Could not create batch jsonl {path}: {e}")
    return path

def append_sample_to_batch_jsonl(color, brewid, sample_payload, created_date_mmddyyyy=None):
    cfg = tilt_cfg.get(color, {})
    beer_name = cfg.get("beer_name", "")
    path = batch_jsonl_filename(color, brewid, created_date_mmddyyyy=created_date_mmddyyyy, beer_name=beer_name)
    try:
        if not os.path.exists(path):
            ensure_batch_jsonl_exists(color, brewid, meta={"beer_name": beer_name, "batch_name": cfg.get("batch_name", "")}, created_date_mmddyyyy=created_date_mmddyyyy)
        entry = {"event": "sample", "payload": sample_payload}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return True
    except Exception as e:
        print(f"[LOG] append_sample_to_batch_jsonl failed for {color}/{brewid}: {e}")
        return False

def write_normalized_tilt_reading(payload, event_name="tilt_reading"):
    try:
        entry = {"event": event_name, "payload": payload}
        tilt_color = payload.get("tilt_color", "") if isinstance(payload, dict) else ""
        log_path = _get_control_log_path(tilt_color)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return True
    except Exception as e:
        print(f"[LOG] write_normalized_tilt_reading failed: {e}")
        return False

def forward_to_third_party_if_configured(payload):
    """
    Forward tilt reading data to configured external services.
    
    Supports two configuration methods:
    1. Per-tilt external_url in tilt_cfg[color] (highest priority)
    2. System-wide external_url_0, external_url_1, external_url_2 in system_cfg
    
    The function will try to send to all configured URLs.
    Automatically transforms the payload to Brewers Friend format if URL contains "brewersfriend.com".
    """
    color = (payload.get("tilt_color") or "").upper()
    if not color:
        return {"forwarded": False, "reason": "no color"}
    
    if requests is None:
        return {"forwarded": False, "reason": "requests library not available"}
    
    # Collect all URLs to forward to
    urls_to_forward = []
    
    # 1. Check per-tilt configuration
    tc = tilt_cfg.get(color) or {}
    tilt_url = tc.get("external_url")
    if tilt_url:
        urls_to_forward.append({
            "url": tilt_url,
            "method": (tc.get("external_method") or "POST").upper(),
            "send_json": bool(tc.get("external_json")) if ("external_json" in tc) else True
        })
    
    # 2. Check system-wide configuration
    # Support new format (external_urls array) and old format (external_url_0, etc.) for backwards compatibility
    external_urls = system_cfg.get("external_urls", [])
    
    if external_urls and isinstance(external_urls, list):
        # New format: per-URL configuration
        for url_config in external_urls:
            if not isinstance(url_config, dict):
                continue
            url = url_config.get("url", "").strip()
            if not url:
                continue
            
            # Get field map if specified
            field_map_id = url_config.get("field_map_id", "default")
            predefined_maps = get_predefined_field_maps()
            field_map = predefined_maps.get(field_map_id, {}).get("map")
            
            # Handle custom field map JSON
            if field_map_id == "custom" and url_config.get("custom_field_map"):
                try:
                    field_map = json.loads(url_config["custom_field_map"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    field_map = None
            
            urls_to_forward.append({
                "url": url,
                "method": url_config.get("method", "POST").upper(),
                "send_json": (url_config.get("content_type", "form") == "json"),
                "timeout": int(url_config.get("timeout_seconds", 8)),
                "field_map": field_map
            })
    else:
        # Old format: external_url_0, external_url_1, external_url_2 with shared settings
        for i in range(3):
            sys_url = system_cfg.get(f"external_url_{i}")
            if sys_url:
                # Parse old external_field_map if present
                field_map = None
                if system_cfg.get("external_field_map"):
                    try:
                        field_map = json.loads(system_cfg["external_field_map"])
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                
                urls_to_forward.append({
                    "url": sys_url,
                    "method": system_cfg.get("external_method", "POST").upper(),
                    "send_json": (system_cfg.get("external_content_type", "form") == "json"),
                    "timeout": int(system_cfg.get("external_timeout_seconds", 8)),
                    "field_map": field_map
                })
    
    if not urls_to_forward:
        return {"forwarded": False, "reason": "no external_url configured"}
    
    # Forward to all configured URLs
    results = []
    for config in urls_to_forward:
        url = config["url"]
        method = config["method"]
        send_json = config["send_json"]
        timeout = config.get("timeout", 8)
        field_map = config.get("field_map")
        
        # Transform payload for Brewers Friend if needed (uses original payload)
        if "brewersfriend.com" in url.lower():
            # Brewers Friend expects a specific format with numeric values
            transformed_payload = {
                "name": payload.get("tilt_color", "Tilt"),
                "temp": payload.get("temp_f") if payload.get("temp_f") is not None else 0,
                "temp_unit": "F",
                "gravity": payload.get("gravity") if payload.get("gravity") is not None else 0,
                "gravity_unit": "G",
                "beer": payload.get("beer_name", "") or payload.get("batch_name", ""),
                "comment": f"Batch: {payload.get('batch_name', '')} | BrewID: {payload.get('brewid', '')}"
            }
            forwarding_payload = transformed_payload
            # Brewers Friend always uses JSON
            send_json = True
        elif field_map:
            # Apply field map transformation if provided and not Brewers Friend
            forwarding_payload = {}
            for logical_field, remote_field in field_map.items():
                if logical_field in payload:
                    forwarding_payload[remote_field] = payload[logical_field]
        else:
            # Use original payload if no transformation needed
            forwarding_payload = payload
        
        headers = {}
        try:
            if send_json:
                headers["Content-Type"] = "application/json"
                resp = requests.request(method, url, json=forwarding_payload, headers=headers, timeout=timeout)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                formdata = {k: ("" if v is None else v) for k, v in forwarding_payload.items() if isinstance(v, (str, int, float)) or v is None}
                resp = requests.request(method, url, data=formdata, headers=headers, timeout=timeout)
            
            result = {"url": url, "forwarded": True, "status_code": resp.status_code, "text": resp.text[:500]}
            results.append(result)
            print(f"[FORWARD] Successfully forwarded tilt {color} to {url}, status: {resp.status_code}")
        except Exception as e:
            result = {"url": url, "forwarded": False, "error": str(e)}
            results.append(result)
            print(f"[FORWARD] Error forwarding tilt {color} to {url}: {e}")
    
    # Return summary
    success_count = sum(1 for r in results if r.get("forwarded"))
    return {
        "forwarded": success_count > 0,
        "success_count": success_count,
        "total_count": len(results),
        "results": results
    }

# --- Notifications helpers -------------------------------------------------
def _smtp_send(recipient, subject, body):
    cfg = system_cfg
    sending_email = cfg.get("sending_email") or cfg.get("email")
    if not (isinstance(cfg, dict) and sending_email):
        error_msg = "SMTP configuration incomplete: sender email not configured"
        print(f"[LOG] {error_msg}")
        return False, error_msg
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sending_email
        msg["To"] = recipient
        smtp_host = cfg.get("smtp_host", "localhost")
        smtp_port = int(cfg.get("smtp_port", 25))
        is_gmail = "gmail" in smtp_host.lower()
        # Use sending_email as username and smtp_password (or sending_email_password) for authentication
        smtp_password = cfg.get("smtp_password") or cfg.get("sending_email_password")
        # Google App Passwords are displayed with spaces (e.g. "abcd efgh ijkl mnop") but
        # must be used without spaces for authentication.  Use split/join rather than
        # replace(" ", "") so that non-breaking spaces and other Unicode whitespace
        # (which Google's website sometimes inserts) are also removed.
        if smtp_password:
            smtp_password = "".join(smtp_password.split())
        # Pre-validate Gmail App Password format before attempting a network call.
        # App Passwords are always exactly 16 alphanumeric characters; regular Google
        # account passwords will be rejected by Gmail with a 534 (WebLoginRequired) or 535 error.
        if is_gmail and smtp_password and (len(smtp_password) != 16 or not smtp_password.isalnum()):
            return False, (
                "Gmail authentication requires a 16-character App Password, not your regular Gmail password. "
                f"The password stored is {len(smtp_password)} characters, which does not match the App Password format. "
                "To generate an App Password: Google Account → Security → 2-Step Verification → App Passwords. "
                "Open App Passwords: https://myaccount.google.com/apppasswords"
            )
        # Port 465 requires SSL from the start; port 587 (and others) use STARTTLS.
        # smtp_starttls may be absent from configs saved before this option was introduced;
        # in that case default to True for Gmail on port 587, which requires STARTTLS.
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            starttls_setting = cfg.get("smtp_starttls")
            # Default to True for port 587 (the standard STARTTLS submission port) when
            # smtp_starttls is absent from the config (i.e. saved before this option existed).
            use_starttls = bool(starttls_setting) if starttls_setting is not None else (smtp_port == 587)
            if use_starttls:
                server.starttls()
        if sending_email and smtp_password and len(smtp_password) > 0:
            server.login(sending_email, smtp_password)
        server.sendmail(sending_email, [recipient], msg.as_string())
        server.quit()
        return True, "Success"
    except Exception as e:
        original_error = str(e)
        print(f"[LOG] SMTP send failed: {original_error}")
        
        # Provide helpful error message for Gmail authentication issues
        is_gmail_host = "gmail" in cfg.get("smtp_host", "").lower()
        is_outlook_host = any(s in cfg.get("smtp_host", "").lower() for s in ("outlook", "hotmail", "live.com", "microsoft"))
        if "BadCredentials" in original_error or (("534" in original_error or "535" in original_error) and is_gmail_host) or ("WebLoginRequired" in original_error):
            error_msg = (
                "Gmail authentication failed. "
                "When 2-Step Verification is enabled on a Google account, regular passwords are blocked for third-party apps. "
                "You must use a Gmail App Password (a 16-character password generated specifically for this app). "
                "To set one up: Google Account → Security → 2-Step Verification → App Passwords "
                "(https://myaccount.google.com/apppasswords). "
                "If you already have an App Password, verify that: "
                "1) It was entered without spaces (Google displays them with spaces but they must be saved without spaces), "
                "2) It is still valid at https://myaccount.google.com/apppasswords, "
                "3) The sending email address matches the Google account where the App Password was created. "
                f"Original error: {original_error}"
            )
        elif is_outlook_host and ("SmtpClientAuthentication" in original_error or "5.7.139" in original_error):
            error_msg = (
                "Outlook/Hotmail personal accounts do not support basic SMTP authentication. "
                "Microsoft has permanently disabled username+password SMTP access for personal Outlook.com, "
                "Hotmail.com, and Live.com accounts — there is no setting to re-enable it. "
                "To send email notifications, switch to one of these alternatives: "
                "1) ntfy.sh push notifications (free, zero credentials — recommended), "
                "2) A Gmail account with an App Password (smtp.gmail.com, port 587), or "
                "3) A Microsoft 365 business/work account where an admin has re-enabled SMTP AUTH. "
                f"Original error: {original_error}"
            )
        else:
            error_msg = original_error
        
        return False, error_msg

def send_email(subject, body):
    recipient = system_cfg.get("email", "").strip()
    # Fall back to the sending email address when no separate recipient is configured
    if not recipient:
        recipient = system_cfg.get("sending_email", "").strip()
    if not recipient:
        print("[LOG] No recipient email configured")
        temp_cfg["email_error"] = True
        return False, "No recipient email configured"
    success, error_msg = _smtp_send(recipient, subject, body)
    temp_cfg["email_error"] = not success
    return success, error_msg

def send_push(body, subject="Fermenter Notification"):
    """
    Send push notification using configured push provider.
    
    Supported providers:
    - Pushover (paid, $5 one-time per platform, very reliable)
    - ntfy (free, open-source, self-hostable)
    """
    push_provider = system_cfg.get("push_provider", "pushover").lower()
    
    if push_provider == "ntfy":
        return _send_push_ntfy(body, subject)
    else:  # Default to Pushover
        return _send_push_pushover(body, subject)

def _send_push_pushover(body, subject="Fermenter Notification"):
    """Send push notification using Pushover API"""
    if not requests:
        error_msg = "requests library not installed. Run: pip install requests"
        print(f"[LOG] {error_msg}")
        temp_cfg["push_error"] = True
        return False, error_msg
    
    # Get Pushover credentials from config
    user_key = system_cfg.get("pushover_user_key", "").strip()
    api_token = system_cfg.get("pushover_api_token", "").strip()
    
    # Validate configuration
    if not user_key or not api_token:
        error_msg = "Pushover User Key and API Token must be configured in System Settings. Sign up at https://pushover.net"
        print(f"[LOG] {error_msg}")
        temp_cfg["push_error"] = True
        return False, error_msg
    
    try:
        # Pushover API endpoint
        url = "https://api.pushover.net/1/messages.json"
        
        # Prepare payload
        payload = {
            "token": api_token,
            "user": user_key,
            "title": subject,
            "message": body,
            "priority": 0  # Normal priority
        }
        
        # Optional: Set device if configured
        device = system_cfg.get("pushover_device", "").strip()
        if device:
            payload["device"] = device
        
        # Send push notification
        response = requests.post(url, data=payload, timeout=10)
        
        if response.status_code == 200:
            print(f"[LOG] Pushover push notification sent successfully")
            temp_cfg["push_error"] = False
            return True, "Success"
        else:
            error_msg = f"Pushover returned status {response.status_code}: {response.text[:200]}"
            print(f"[LOG] {error_msg}")
            temp_cfg["push_error"] = True
            return False, error_msg
        
    except Exception as e:
        error_msg = f"Pushover push notification failed: {str(e)}"
        print(f"[LOG] {error_msg}")
        temp_cfg["push_error"] = True
        return False, error_msg

def _send_push_ntfy(body, subject="Fermenter Notification"):
    """Send push notification using ntfy (free, open-source)"""
    if not requests:
        error_msg = "requests library not installed. Run: pip install requests"
        print(f"[LOG] {error_msg}")
        temp_cfg["push_error"] = True
        return False, error_msg
    
    # Get ntfy configuration
    ntfy_server = system_cfg.get("ntfy_server", "https://ntfy.sh").strip()
    ntfy_topic = system_cfg.get("ntfy_topic", "").strip()
    
    # Validate configuration
    if not ntfy_topic:
        error_msg = "ntfy Topic must be configured. Choose a unique topic name and configure it in System Settings."
        print(f"[LOG] {error_msg}")
        temp_cfg["push_error"] = True
        return False, error_msg
    
    try:
        # ntfy API endpoint
        url = f"{ntfy_server}/{ntfy_topic}"
        
        # Prepare headers (ntfy uses headers for metadata)
        headers = {
            "Title": subject,
            "Priority": "default",
            "Tags": "beer,fermentation"
        }
        
        # Optional: Add auth token if configured
        auth_token = system_cfg.get("ntfy_auth_token", "").strip()
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        
        # Send push notification (body is sent as plain text in request body)
        response = requests.post(
            url,
            data=body.encode('utf-8'),
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"[LOG] ntfy push notification sent successfully")
            temp_cfg["push_error"] = False
            return True, "Success"
        else:
            error_msg = f"ntfy returned status {response.status_code}: {response.text[:200]}"
            print(f"[LOG] {error_msg}")
            temp_cfg["push_error"] = True
            return False, error_msg
            
    except Exception as e:
        error_msg = f"ntfy push notification failed: {str(e)}"
        print(f"[LOG] {error_msg}")
        temp_cfg["push_error"] = True
        return False, error_msg

def attempt_send_notifications(subject, body):
    # Use system_cfg for notification mode
    mode = (system_cfg.get('warning_mode') or 'NONE').upper()
    success_any = False
    temp_cfg['notifications_trigger'] = True
    # Propagate the in-progress state to all controller dicts so the UI
    # can show a "sending" indicator while the notification is being delivered.
    for ctrl in temp_cfg.get('controllers', []):
        ctrl['notifications_trigger'] = True
    
    # Reset error flags before attempting
    temp_cfg['push_error'] = False
    temp_cfg['email_error'] = False
    
    error_msg = None
    try:
        if mode == 'EMAIL':
            success_any, error_msg = send_email(subject, body)
            if not success_any:
                print(f"[LOG] Email notification failed: {error_msg}")
                # Try push as fallback to alert user about the email failure
                push_provider = system_cfg.get('push_provider', '')
                push_configured = (
                    (push_provider == 'pushover' and system_cfg.get('pushover_api_token') and system_cfg.get('pushover_user_key')) or
                    (push_provider == 'ntfy' and system_cfg.get('ntfy_topic'))
                )
                if push_configured:
                    fallback_body = f"⚠️ EMAIL ERROR: Could not send email notification.\nError: {error_msg}\n\nOriginal message:\n{body}"
                    send_push(fallback_body, f"Email Error - {subject}")
            # Log email notification attempt
            log_notification(
                notification_type='email',
                subject=subject,
                body=body,
                success=success_any,
                error=error_msg if not success_any else None
            )
        elif mode == 'PUSH':
            success_any, error_msg = send_push(body, subject)
            if not success_any:
                print(f"[LOG] Push notification failed: {error_msg}")
            # Log push notification attempt
            log_notification(
                notification_type='push',
                subject=subject,
                body=body,
                success=success_any,
                error=error_msg if not success_any else None
            )
        elif mode == 'BOTH':
            e, email_error = send_email(subject, body)
            p, push_error = send_push(body, subject)
            if not e:
                print(f"[LOG] Email notification failed: {email_error}")
            if not p:
                print(f"[LOG] Push notification failed: {push_error}")
            success_any = e or p
            # Log individual email and push attempts separately
            log_notification(
                notification_type='email',
                subject=subject,
                body=body,
                success=e,
                error=email_error if not e else None
            )
            log_notification(
                notification_type='push',
                subject=subject,
                body=body,
                success=p,
                error=push_error if not p else None
            )
        else:
            success_any = False
            error_msg = f"Invalid notification mode: {mode}"
            # Log invalid mode
            log_notification(
                notification_type='none',
                subject=subject,
                body=body,
                success=False,
                error=error_msg
            )
    except Exception as e:
        print(f"[LOG] Notification attempt exception: {e}")
        success_any = False
        error_msg = str(e)
        # Log exception (use 'unknown' if mode is somehow not set)
        log_notification(
            notification_type=mode.lower() if mode and mode != 'NONE' else 'none',
            subject=subject,
            body=body,
            success=False,
            error=error_msg
        )

    temp_cfg['notifications_trigger'] = False
    if success_any:
        temp_cfg['notification_last_sent'] = datetime.utcnow().isoformat()
        temp_cfg['notification_comm_failure'] = False
    else:
        temp_cfg['notification_comm_failure'] = True
        # Don't disable notifications on failure - just track the failure

    # Propagate system-wide notification state to all controller dicts for UI display.
    # push_error/email_error are set on temp_cfg by send_email()/send_push(); copy
    # the final values so each controller card reflects the actual notification health.
    for ctrl in temp_cfg.get('controllers', []):
        ctrl['push_error'] = temp_cfg.get('push_error', False)
        ctrl['email_error'] = temp_cfg.get('email_error', False)
        ctrl['notifications_trigger'] = temp_cfg.get('notifications_trigger', False)
        ctrl['notification_comm_failure'] = temp_cfg.get('notification_comm_failure', False)
        if temp_cfg.get('notification_last_sent'):
            ctrl['notification_last_sent'] = temp_cfg.get('notification_last_sent')

    return success_any

def send_warning(subject, body):
    mode = (system_cfg.get('warning_mode') or 'NONE').upper()
    if mode == 'NONE':
        return False
    try:
        rate_limit = int(system_cfg.get('notification_rate_limit_seconds', 3600))
    except Exception:
        rate_limit = 3600

    # Use the global notification_last_sent for rate limiting (system-wide)
    last = temp_cfg.get('notification_last_sent')
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            elapsed = (datetime.utcnow() - last_dt).total_seconds()
            if elapsed < rate_limit:
                return False
        except Exception:
            pass

    ok = attempt_send_notifications(subject, body)
    return ok

def send_temp_control_notification(event_type, temp, low_limit, high_limit, tilt_color):
    """
    Send notifications for temperature control events if enabled in settings.
    Uses the pending queue system with deduplication to prevent duplicate alerts.
    
    Handles all temperature control events: temp limits, heating/cooling on/off.
    Users can individually enable/disable each notification type.
    """
    # Get temp control notification settings
    temp_notif_cfg = system_cfg.get('temp_control_notifications', {})
    
    # Check if this specific event type is enabled
    if not temp_notif_cfg.get(f'enable_{event_type}', False):
        return
    
    brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
    now = datetime.utcnow()
    
    # Create caption based on event type
    caption_map = {
        'temp_below_low_limit': f'Temperature Below Low Limit - Current: {temp:.1f}°F, Low Limit: {low_limit:.1f}°F',
        'temp_above_high_limit': f'Temperature Above High Limit - Current: {temp:.1f}°F, High Limit: {high_limit:.1f}°F',
        'heating_on': f'Heating Turned On - Current: {temp:.1f}°F, Low Limit: {low_limit:.1f}°F',
        'heating_off': f'Heating Turned Off - Current: {temp:.1f}°F',
        'cooling_on': f'Cooling Turned On - Current: {temp:.1f}°F, High Limit: {high_limit:.1f}°F',
        'cooling_off': f'Cooling Turned Off - Current: {temp:.1f}°F',
    }
    
    caption = caption_map.get(event_type, f'Temperature Control Event: {event_type}')
    
    subject = f"{brewery_name} - Temperature Control Alert"
    body = f"""Brewery Name: {brewery_name}
Date: {now.strftime('%Y-%m-%d')}
Time: {now.strftime('%H:%M:%S')}
Tilt Color: {tilt_color}

{caption}"""
    
    # Queue notification with 10-second delay for deduplication
    # Use tilt_color as brewid since temp control is per-tilt
    queue_pending_notification(
        notification_type=event_type,
        subject=subject,
        body=body,
        brewid=tilt_color,  # Use tilt_color as identifier for temp control
        color=tilt_color
    )

def send_safety_shutdown_notification(tilt_color, low_limit, high_limit):
    """
    Send notification when control Tilt becomes inactive and safety shutdown is triggered.
    Uses the pending queue system with deduplication to prevent duplicate alerts.
    
    Args:
        tilt_color: The color of the Tilt that went offline
        low_limit: Current low temperature limit
        high_limit: Current high temperature limit
    """
    # Get temp control notification settings
    temp_notif_cfg = system_cfg.get('temp_control_notifications', {})
    
    # Check if safety shutdown notifications are enabled (default to True for safety)
    if not temp_notif_cfg.get('enable_safety_shutdown', True):
        return
    
    brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
    # Use the actual temp-control safety timeout (2 × update_interval), NOT the
    # batch-monitoring signal-loss timeout (tilt_inactivity_timeout_minutes = 30 min).
    # Reporting the wrong value caused users to believe the system hadn't seen a
    # reading in 30 minutes when the real threshold is much shorter.
    try:
        update_interval_minutes = int(system_cfg.get("update_interval", 2))
    except Exception:
        update_interval_minutes = 2
    timeout_minutes = update_interval_minutes * 2 + 1
    now = datetime.utcnow()
    
    subject = f"{brewery_name} - SAFETY SHUTDOWN: Control Tilt Offline"
    body = f"""CRITICAL SAFETY ALERT

Brewery Name: {brewery_name}
Date: {now.strftime('%Y-%m-%d')}
Time: {now.strftime('%H:%M:%S')}
Tilt Color: {tilt_color}

SAFETY SHUTDOWN TRIGGERED

The Tilt assigned to temperature control has not transmitted data within the
safety timeout of {timeout_minutes} minutes ({update_interval_minutes}-minute update interval × 2 + 1 min buffer).

All Kasa plugs have been automatically turned OFF to prevent runaway heating/cooling.

Current Settings:
- Low Limit: {low_limit:.1f}°F
- High Limit: {high_limit:.1f}°F

Action Required:
1. Check Tilt battery
2. Verify Tilt is in range
3. Confirm Bluetooth connectivity
4. Temperature control will resume automatically when Tilt starts transmitting"""
    
    # Queue notification with 10-second delay for deduplication
    queue_pending_notification(
        notification_type='safety_shutdown',
        subject=subject,
        body=body,
        brewid=tilt_color,
        color=tilt_color
    )

def send_plug_blocked_notification(mode, tilt_color):
    """
    Send notification when a plug ON command is blocked due to no Tilt connection.
    Uses the pending queue system with deduplication to prevent duplicate alerts.
    
    Args:
        mode: 'heating' or 'cooling'
        tilt_color: The color of the Tilt that is offline
    """
    # Get temp control notification settings
    temp_notif_cfg = system_cfg.get('temp_control_notifications', {})
    
    # Check if safety notifications are enabled (default to True for safety)
    if not temp_notif_cfg.get('enable_safety_shutdown', True):
        return
    
    brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
    now = datetime.utcnow()
    
    mode_name = "Heating" if mode == "heating" else "Cooling"
    
    subject = f"{brewery_name} - SAFETY: {mode_name} Blocked (No Tilt Connection)"
    body = f"""SAFETY ALERT

Brewery Name: {brewery_name}
Date: {now.strftime('%Y-%m-%d')}
Time: {now.strftime('%H:%M:%S')}
Tilt Color: {tilt_color}

{mode_name.upper()} BLOCKED - NO TILT CONNECTION

The system attempted to turn ON the {mode_name.lower()} plug, but the Tilt assigned 
to temperature control is not transmitting data.

Safety Rule: No connection = No plugs turn ON

The {mode_name.lower()} plug will remain OFF until the Tilt connection is restored.

Action Required:
1. Check Tilt battery
2. Verify Tilt is in range
3. Ensure Tilt is in liquid
4. Check Bluetooth connectivity

The {mode_name.lower()} plug will automatically resume normal operation once 
the Tilt starts transmitting again.

This is a safety feature to prevent uncontrolled temperature changes."""

    # Queue notification with deduplication
    queue_pending_notification(
        notification_type=f'plug_blocked_{mode}',
        subject=subject,
        body=body,
        brewid=tilt_color,
        color=tilt_color
    )

def send_plug_safety_off_notification(mode, tilt_color):
    """
    Send notification when a plug is turned OFF due to no Tilt connection.
    Uses the pending queue system with deduplication to prevent duplicate alerts.
    
    Args:
        mode: 'heating' or 'cooling'
        tilt_color: The color of the Tilt that is offline
    """
    # Get temp control notification settings
    temp_notif_cfg = system_cfg.get('temp_control_notifications', {})
    
    # Check if safety notifications are enabled (default to True for safety)
    if not temp_notif_cfg.get('enable_safety_shutdown', True):
        return
    
    brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
    now = datetime.utcnow()
    
    mode_name = "Heating" if mode == "heating" else "Cooling"
    
    subject = f"{brewery_name} - SAFETY: {mode_name} Turned OFF (No Tilt Connection)"
    body = f"""SAFETY ALERT

Brewery Name: {brewery_name}
Date: {now.strftime('%Y-%m-%d')}
Time: {now.strftime('%H:%M:%S')}
Tilt Color: {tilt_color}

{mode_name.upper()} TURNED OFF - NO TILT CONNECTION

The Tilt assigned to temperature control is not transmitting data.
The {mode_name.lower()} plug has been automatically turned OFF for safety.

Safety Rule: No connection = Plugs turn OFF

The {mode_name.lower()} plug was ON but is now being turned OFF because 
the system cannot read the current temperature.

Action Required:
1. Check Tilt battery
2. Verify Tilt is in range
3. Ensure Tilt is in liquid
4. Check Bluetooth connectivity

The {mode_name.lower()} plug will automatically resume normal operation once 
the Tilt starts transmitting again.

This is a safety feature to prevent uncontrolled temperature changes."""

    # Queue notification with deduplication
    queue_pending_notification(
        notification_type=f'plug_safety_off_{mode}',
        subject=subject,
        body=body,
        brewid=tilt_color,
        color=tilt_color
    )

def send_kasa_error_notification(mode, url, error_msg, controller=None):
    """
    Send notifications for Kasa plug connection failures if enabled in settings.
    Uses the pending queue system with deduplication to prevent duplicate alerts.

    When called with a controller dict, uses per-controller time-based tracking so
    that a persistent failure re-notifies the user every _KASA_ERROR_RENOTIFY_INTERVAL
    seconds (default 1 hour).  This ensures the user is kept informed if the
    connection is never restored.

    When called without a controller (legacy path), falls back to the old boolean
    flag on temp_cfg so as not to break callers that don't have a controller ref.
    
    Args:
        mode: 'heating' or 'cooling'
        url: IP address or hostname of the Kasa plug
        error_msg: Error message from the connection failure
        controller: Optional per-controller state dict; when provided gives
                    correct per-controller deduplication and periodic re-notification.
    """
    # Get temp control notification settings
    temp_notif_cfg = system_cfg.get('temp_control_notifications', {})
    
    # Check if Kasa error notifications are enabled
    if not temp_notif_cfg.get('enable_kasa_error', True):
        return
    
    if controller is not None:
        # Per-controller time-based tracking.
        # Re-notify after _KASA_ERROR_RENOTIFY_INTERVAL seconds so that a
        # permanent failure keeps alerting the user, not just the first occurrence.
        notified_at_key = f"{mode}_kasa_error_notified_at"
        last_notified_at = controller.get(notified_at_key, 0)
        if time.time() - last_notified_at < _KASA_ERROR_RENOTIFY_INTERVAL:
            return  # Notified recently enough; wait until the interval has passed
        controller[notified_at_key] = time.time()
        tilt_color = controller.get('tilt_color', '')
    else:
        # Legacy boolean-flag path for callers that don't pass a controller.
        notified_flag = f"{mode}_error_notified"
        if temp_cfg.get(notified_flag, False):
            # Already notified about this error, don't send again
            return
        # Set the notified flag to prevent duplicate notifications
        temp_cfg[notified_flag] = True
        tilt_color = temp_cfg.get('tilt_color', '')
    
    brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
    now = datetime.utcnow()
    
    mode_name = 'Heating' if mode == 'heating' else 'Cooling'

    # Build a human-readable duration string if the error has been ongoing
    duration_note = ""
    if controller is not None:
        error_since = controller.get(f"{mode}_kasa_error_since")
        if error_since:
            elapsed = int(time.time() - error_since)
            if elapsed >= 3600:
                hours = elapsed // 3600
                mins  = (elapsed % 3600) // 60
                duration_note = f"\nConnection has been lost for {hours}h {mins}m."
            elif elapsed >= 60:
                duration_note = f"\nConnection has been lost for {elapsed // 60}m."

    subject = f"{brewery_name} - Kasa Plug Connection Failure"
    body = f"""Brewery Name: {brewery_name}
Date: {now.strftime('%Y-%m-%d')}
Time: {now.strftime('%H:%M:%S')}
Tilt Color: {tilt_color}
Mode: {mode_name}
Plug URL: {url}

Failed to connect to Kasa plug.{duration_note}
Error: {error_msg}"""
    
    # Queue notification with 10-second delay for deduplication
    # Use combination of mode and url as unique identifier
    queue_pending_notification(
        notification_type='kasa_error',
        subject=subject,
        body=body,
        brewid=f"{mode}_{url}",  # Unique identifier for this specific plug and mode
        color=tilt_color
    )

def save_notification_state_to_config(color, brewid):
    """
    Save notification state flags to tilt_config.json for persistence across restarts.
    Only saves the notification flags, not transient data like gravity_history.
    Signal loss is NOT persisted - it resets on restart as requested.
    """
    if brewid not in batch_notification_state:
        return
    
    if not color:
        print(f"[LOG] save_notification_state_to_config: No color provided for brewid {brewid}")
        return
    
    state = batch_notification_state[brewid]
    if color not in tilt_cfg:
        print(f"[LOG] save_notification_state_to_config: Color {color} not in tilt_cfg")
        return
    
    # Only persist notification timestamps, not signal loss (resets on restart)
    tilt_cfg[color]['notification_state'] = {
        'fermentation_start_datetime': state.get('fermentation_start_datetime'),
        'fermentation_completion_datetime': state.get('fermentation_completion_datetime'),
        'last_daily_report': state.get('last_daily_report')
    }
    
    try:
        save_json(TILT_CONFIG_FILE, tilt_cfg)
    except Exception as e:
        print(f"[LOG] Could not save notification state for {color}: {e}")

def load_notification_state_from_config(color, brewid, cfg):
    """
    Load persisted notification state from tilt_config.json.
    Returns a dict with notification state flags.
    Signal loss flags are NOT loaded - they always start fresh on restart.
    """
    persisted_state = cfg.get('notification_state', {})
    
    # Load persisted datetime values for fermentation start/completion
    # Signal loss flags are intentionally NOT persisted (reset on restart)
    return {
        'last_reading_time': datetime.utcnow(),
        'signal_lost': False,          # Always start fresh on restart
        'signal_loss_notified': False, # Always start fresh on restart
        'signal_recovery_start': None, # Always start fresh on restart
        'fermentation_started': bool(persisted_state.get('fermentation_start_datetime')),
        'fermentation_start_datetime': persisted_state.get('fermentation_start_datetime'),
        'fermentation_completion_datetime': persisted_state.get('fermentation_completion_datetime'),
        'gravity_history': [],
        'last_daily_report': persisted_state.get('last_daily_report')
    }

def check_batch_notifications(color, gravity, temp_f, brewid, cfg):
    """
    Check and trigger batch-specific notifications:
    1. Loss of signal detection
    2. Fermentation starting detection
    3. Daily report scheduling (handled separately in periodic task)
    """
    if not brewid:
        return
    
    # Get notification settings from system config
    notif_cfg = system_cfg.get('batch_notifications', {})
    
    # Initialize state for this brewid if needed
    if brewid not in batch_notification_state:
        # Load persisted state from config file
        batch_notification_state[brewid] = load_notification_state_from_config(color, brewid, cfg)
    
    state = batch_notification_state[brewid]
    state['last_reading_time'] = datetime.utcnow()
    
    # When a reading arrives after signal loss, start recovery tracking instead of
    # immediately clearing the notification flag.  A single stray packet at the edge
    # of BLE range must not be enough to restart the full notification cycle.
    if state['signal_lost']:
        state['signal_lost'] = False
        if not state.get('signal_recovery_start'):
            state['signal_recovery_start'] = datetime.utcnow()
    
    # Only clear signal_loss_notified once the signal has been continuously
    # present for at least loss_timeout_minutes.  This ensures that brief
    # intermittent readings (Tilt at maximum range) do not reset the timers.
    if state.get('signal_loss_notified') and state.get('signal_recovery_start'):
        loss_timeout_minutes = int(notif_cfg.get('loss_of_signal_timeout_minutes', 30))
        recovery_elapsed = (datetime.utcnow() - state['signal_recovery_start']).total_seconds() / 60.0
        if recovery_elapsed >= loss_timeout_minutes:
            state['signal_loss_notified'] = False
            state['signal_recovery_start'] = None
    
    # Track gravity history for fermentation start detection
    if gravity is not None:
        state['gravity_history'].append({
            'gravity': gravity,
            'timestamp': datetime.utcnow()
        })
        # Keep only recent readings (last 10)
        if len(state['gravity_history']) > 10:
            state['gravity_history'].pop(0)
    
    # Check fermentation starting condition
    if notif_cfg.get('enable_fermentation_starting', True):
        check_fermentation_starting(color, brewid, cfg, state)
    
    # Check fermentation completion condition
    if notif_cfg.get('enable_fermentation_completion', True):
        check_fermentation_completion(color, brewid, cfg, state)

def check_fermentation_starting(color, brewid, cfg, state):
    """
    Detect fermentation start: 3 consecutive readings at least 0.010 below starting gravity.
    Saves the datetime when fermentation start notification is sent.
    """
    # Check if notification already sent (datetime will be present)
    if state.get('fermentation_start_datetime'):
        return
    
    # Add debounce protection: prevent re-checking too frequently (5 second minimum)
    last_check = state.get('last_fermentation_start_check')
    if last_check:
        elapsed = (datetime.utcnow() - last_check).total_seconds()
        if elapsed < 5:
            return
    
    actual_og = cfg.get('actual_og')
    if not actual_og:
        return
    
    try:
        starting_gravity = float(actual_og)
    except (ValueError, TypeError):
        return
    
    history = state.get('gravity_history', [])
    if len(history) < 3:
        return
    
    # Check last 3 readings
    last_three = history[-3:]
    all_below_threshold = all(
        reading['gravity'] is not None and reading['gravity'] <= (starting_gravity - 0.010)
        for reading in last_three
    )
    
    if all_below_threshold:
        # Update debounce timestamp only when we detect the condition
        # This ensures we don't delay legitimate notifications due to early returns
        state['last_fermentation_start_check'] = datetime.utcnow()
        
        current_gravity = last_three[-1]['gravity']
        brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
        beer_name = cfg.get('beer_name', 'Unknown Beer')
        
        # Get current datetime for the notification
        notification_time = datetime.utcnow()
        
        # Set flag BEFORE sending to prevent race condition with duplicate notifications
        # This is critical: setting the flag FIRST ensures that even if multiple BLE packets
        # arrive within milliseconds, only the first one will proceed to send notification
        state['fermentation_start_datetime'] = notification_time.isoformat()
        state['fermentation_started'] = True
        
        subject = f"{brewery_name} - Fermentation Started"
        body = f"""Brewery Name: {brewery_name}
Tilt Color: {color}
Brew Name: {beer_name}
Date/Time: {notification_time.strftime('%Y-%m-%d %H:%M:%S')}

Fermentation has started.
Gravity at start: {starting_gravity:.3f}
Gravity now: {current_gravity:.3f}"""
        
        # Always save state to config file, regardless of notification success
        # This prevents duplicate notifications even if retry fails
        # Users can check logs/UI to see if notifications failed
        save_notification_state_to_config(color, brewid)
        
        # Log the event to batch JSONL and send notification
        log_event('fermentation_starting', body, tilt_color=color)
        
        # Queue notification with 10-second delay for deduplication
        queue_pending_notification(
            notification_type='fermentation_start',
            subject=subject,
            body=body,
            brewid=brewid,
            color=color
        )

def check_fermentation_completion(color, brewid, cfg, state):
    """
    Detect fermentation completion: gravity stable for 24 hours after fermentation started.
    Saves the datetime when fermentation completion notification is sent.
    """
    # Check if notification already sent (datetime will be present)
    if state.get('fermentation_completion_datetime'):
        return
    
    # Add debounce protection: prevent re-checking too frequently (5 second minimum)
    last_check = state.get('last_fermentation_completion_check')
    if last_check:
        elapsed = (datetime.utcnow() - last_check).total_seconds()
        if elapsed < 5:
            return
    
    # Only check for completion if fermentation has started
    if not state.get('fermentation_started'):
        return
    
    history = state.get('gravity_history', [])
    if len(history) < 2:
        return
    
    # Check if gravity has been stable for 24 hours
    # Look at last reading and compare with readings from 24 hours ago
    current_time = datetime.utcnow()
    current_gravity = history[-1]['gravity']
    
    # Find readings from approximately 24 hours ago
    stable_for_24h = True
    readings_24h_ago = []
    
    for reading in history:
        time_diff = (current_time - reading['timestamp']).total_seconds() / 3600.0
        
        # Check readings between 23-25 hours ago
        if 23 <= time_diff <= 25:
            readings_24h_ago.append(reading)
            # If gravity has changed by more than 0.002, not stable
            if abs(reading['gravity'] - current_gravity) > 0.002:
                stable_for_24h = False
                break
    
    # Need at least one reading from 24 hours ago to confirm stability
    if not readings_24h_ago or not stable_for_24h:
        return
    
    # Fermentation completion detected
    # Update debounce timestamp only when we detect the condition
    # This ensures we don't delay legitimate notifications due to early returns
    state['last_fermentation_completion_check'] = datetime.utcnow()
    
    brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
    beer_name = cfg.get('beer_name', 'Unknown Beer')
    actual_og = cfg.get('actual_og')
    
    notification_time = datetime.utcnow()
    
    # Set flag BEFORE sending to prevent race condition with duplicate notifications
    # This is critical: setting the flag FIRST ensures that even if multiple BLE packets
    # arrive within milliseconds, only the first one will proceed to send notification
    state['fermentation_completion_datetime'] = notification_time.isoformat()
    
    subject = f"{brewery_name} - Fermentation Completion"
    body = f"""Brewery Name: {brewery_name}
Tilt Color: {color}
Brew Name: {beer_name}
Date/Time: {notification_time.strftime('%Y-%m-%d %H:%M:%S')}

Fermentation completion detected.
Gravity has been stable for 24 hours: {current_gravity:.3f}"""
    
    if actual_og:
        try:
            starting_gravity = float(actual_og)
            attenuation = ((starting_gravity - current_gravity) / (starting_gravity - 1.0)) * 100
            body += f"\nStarting Gravity: {starting_gravity:.3f}"
            body += f"\nApparent Attenuation: {attenuation:.1f}%"
        except (ValueError, TypeError):
            pass
    
    # Always save state to config file, regardless of notification success
    # This prevents duplicate notifications even if retry fails
    # Users can check logs/UI to see if notifications failed
    save_notification_state_to_config(color, brewid)
    
    # Log the event to batch JSONL and send notification
    log_event('fermentation_completion', body, tilt_color=color)
    
    # Queue notification with 10-second delay for deduplication
    queue_pending_notification(
        notification_type='fermentation_completion',
        subject=subject,
        body=body,
        brewid=brewid,
        color=color
    )

def check_signal_loss():
    """
    Periodic check for loss of signal on all active tilts.
    Run this in a separate thread or periodic task.
    """
    notif_cfg = system_cfg.get('batch_notifications', {})
    if not notif_cfg.get('enable_loss_of_signal', True):
        return
    
    loss_timeout_minutes = int(notif_cfg.get('loss_of_signal_timeout_minutes', 30))
    now = datetime.utcnow()
    
    for brewid, state in batch_notification_state.items():
        last_reading = state.get('last_reading_time')
        if not last_reading:
            continue
        
        elapsed_minutes = (now - last_reading).total_seconds() / 60.0
        
        if state.get('signal_loss_notified'):
            # Notification cycle already complete.  If readings have been absent
            # long enough to exceed the loss timeout again, discard any partial
            # recovery timer so that when the signal truly returns the recovery
            # clock starts fresh (preventing stray packets from being counted).
            if elapsed_minutes >= loss_timeout_minutes and state.get('signal_recovery_start'):
                state['signal_recovery_start'] = None
                state['signal_lost'] = True
            continue
        
        if elapsed_minutes >= loss_timeout_minutes:
            # Find the tilt color and config for this brewid
            color = None
            cfg = None
            for tilt_color, tilt_data in tilt_cfg.items():
                if tilt_data.get('brewid') == brewid:
                    color = tilt_color
                    cfg = tilt_data
                    break
            
            if color and cfg:
                # Set flags BEFORE queueing to prevent race condition with duplicate notifications
                # This ensures that even if check_signal_loss is called multiple times rapidly,
                # only the first call will proceed to queue the notification
                state['signal_lost'] = True
                state['signal_loss_notified'] = True
                
                brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
                beer_name = cfg.get('beer_name', 'Unknown Beer')
                
                subject = f"{brewery_name} - Loss of Signal"
                body = f"""Brewery Name: {brewery_name}
Tilt Color: {color}
Brew Name: {beer_name}
Date/Time: {now.strftime('%Y-%m-%d %H:%M:%S')}

Loss of Signal -- Receiving no tilt readings"""
                
                # Queue notification with 10-second delay for deduplication
                queue_pending_notification(
                    notification_type='signal_loss',
                    subject=subject,
                    body=body,
                    brewid=brewid,
                    color=color
                )

def queue_notification_retry(notification_type, subject, body, brewid, color):
    """
    Queue a failed notification for retry with exponential backoff.
    
    Args:
        notification_type: Type of notification (signal_loss, fermentation_start, fermentation_completion)
        subject: Email/PUSH subject
        body: Email/PUSH body
        brewid: Brew ID for deduplication
        color: Tilt color for deduplication
    """
    # Check if this notification is already queued (prevent duplicates in retry queue)
    for item in notification_retry_queue:
        if item['notification_type'] == notification_type and item['brewid'] == brewid:
            # Already queued, don't add again
            return
    
    notification_retry_queue.append({
        'notification_type': notification_type,
        'subject': subject,
        'body': body,
        'brewid': brewid,
        'color': color,
        'retry_count': 0,
        'last_retry_time': datetime.utcnow(),
        'created_time': datetime.utcnow()
    })

def process_notification_retries():
    """
    Process the notification retry queue with exponential backoff.
    Called periodically (every 5 minutes) by the batch monitoring thread.
    """
    now = datetime.utcnow()
    items_to_remove = []
    
    for item in notification_retry_queue:
        retry_count = item['retry_count']
        last_retry_time = item['last_retry_time']
        
        # Check if we've exceeded max retries
        if retry_count >= NOTIFICATION_MAX_RETRIES:
            print(f"[LOG] Notification retry limit reached for {item['notification_type']}/{item['brewid']}, giving up")
            items_to_remove.append(item)
            continue
        
        # Calculate time since last retry
        elapsed_seconds = (now - last_retry_time).total_seconds()
        
        # Get the retry interval for this attempt
        retry_interval = NOTIFICATION_RETRY_INTERVALS[retry_count] if retry_count < len(NOTIFICATION_RETRY_INTERVALS) else NOTIFICATION_RETRY_INTERVALS[-1]
        
        # Check if it's time to retry
        if elapsed_seconds >= retry_interval:
            print(f"[LOG] Retrying notification: {item['notification_type']}/{item['brewid']} (attempt {retry_count + 2})")
            
            # Attempt to send
            success = attempt_send_notifications(item['subject'], item['body'])
            
            if success:
                print(f"[LOG] Notification retry successful for {item['notification_type']}/{item['brewid']}")
                items_to_remove.append(item)
            else:
                # Update retry count and time
                item['retry_count'] += 1
                item['last_retry_time'] = now
                print(f"[LOG] Notification retry failed for {item['notification_type']}/{item['brewid']}, will retry again")
    
    # Remove successfully sent or expired items
    for item in items_to_remove:
        notification_retry_queue.remove(item)

def queue_pending_notification(notification_type, subject, body, brewid, color):
    """
    Queue a notification in the pending queue with deduplication.
    
    This implements a 10-second delay before sending notifications to prevent duplicates.
    If the same notification is already pending, it will not be added again.
    
    Args:
        notification_type: Type of notification (signal_loss, fermentation_start, fermentation_completion)
        subject: Email/PUSH subject
        body: Email/PUSH body
        brewid: Brew ID for deduplication
        color: Tilt color for deduplication
    """
    # Check if this notification is already pending (prevent duplicates)
    for item in pending_notifications:
        if item['notification_type'] == notification_type and item['brewid'] == brewid:
            # Already pending, don't add again
            print(f"[LOG] Notification {notification_type}/{brewid} already pending, skipping duplicate")
            return
    
    # Add to pending queue
    pending_notifications.append({
        'notification_type': notification_type,
        'subject': subject,
        'body': body,
        'brewid': brewid,
        'color': color,
        'queued_time': datetime.utcnow()
    })
    print(f"[LOG] Queued {notification_type}/{brewid} notification for sending in {NOTIFICATION_PENDING_DELAY_SECONDS} seconds")

def process_pending_notifications():
    """
    Process the pending notification queue.
    
    Sends notifications that have been pending for at least NOTIFICATION_PENDING_DELAY_SECONDS.
    This provides a window for deduplication to work - if multiple identical notifications
    are triggered within the delay window, only the first one will be sent.
    
    Called periodically (every 5 minutes) by the batch monitoring thread.
    """
    now = datetime.utcnow()
    items_to_remove = []
    
    for item in pending_notifications:
        queued_time = item['queued_time']
        
        # Calculate time since queued
        elapsed_seconds = (now - queued_time).total_seconds()
        
        # Check if it's time to send (after the delay period)
        if elapsed_seconds >= NOTIFICATION_PENDING_DELAY_SECONDS:
            print(f"[LOG] Sending pending notification: {item['notification_type']}/{item['brewid']}")
            
            # Attempt to send
            success = attempt_send_notifications(item['subject'], item['body'])
            
            if success:
                print(f"[LOG] Pending notification sent successfully for {item['notification_type']}/{item['brewid']}")
                items_to_remove.append(item)
            else:
                # Failed to send - queue for retry
                print(f"[LOG] Pending notification failed for {item['notification_type']}/{item['brewid']}, queuing for retry")
                queue_notification_retry(
                    notification_type=item['notification_type'],
                    subject=item['subject'],
                    body=item['body'],
                    brewid=item['brewid'],
                    color=item['color']
                )
                items_to_remove.append(item)
    
    # Remove sent or failed items
    for item in items_to_remove:
        pending_notifications.remove(item)

def send_daily_report():
    """
    Send daily progress report for all active tilts.
    Should be scheduled to run at user-specified time.
    """
    notif_cfg = system_cfg.get('batch_notifications', {})
    if not notif_cfg.get('enable_daily_report', True):
        return
    
    brewery_name = system_cfg.get('brewery_name', 'Unknown Brewery')
    
    for color, cfg in tilt_cfg.items():
        brewid = cfg.get('brewid')
        if not brewid:
            continue
        
        state = batch_notification_state.get(brewid, {})
        
        # Check if we already sent today's report (within last 23 hours to allow for timing variance)
        last_report = state.get('last_daily_report')
        if last_report:
            try:
                last_report_dt = datetime.fromisoformat(last_report)
                # Use DAILY_REPORT_COOLDOWN_HOURS to ensure daily (once per ~24h) but allow for timing variance
                if (datetime.utcnow() - last_report_dt).total_seconds() < DAILY_REPORT_COOLDOWN_HOURS * 3600:
                    continue
            except Exception:
                pass
        
        beer_name = cfg.get('beer_name', 'Unknown Beer')
        actual_og = cfg.get('actual_og')
        
        # Use live tilt data if available, otherwise skip
        current_data = live_tilts.get(color, {})
        current_gravity = current_data.get('gravity')
        current_temp = current_data.get('temp_f')
        
        if current_gravity is None and current_temp is None:
            # No live data at all - skip this tilt
            continue
        
        # Build report body
        lines = [
            f"Brewery Name: {brewery_name}",
            f"Tilt Color: {color}",
            f"Brew Name: {beer_name}",
            f"Date/Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        
        if current_temp is not None:
            lines.append(f"Current Temperature: {current_temp:.1f}\u00b0F")
        
        if current_gravity is not None:
            lines.append(f"Current Gravity: {current_gravity:.3f}")
            
            if actual_og:
                try:
                    starting_gravity = float(actual_og)
                    net_change = starting_gravity - current_gravity
                    lines.append(f"Starting Gravity (OG): {starting_gravity:.3f}")
                    lines.append(f"Net Gravity Change: {net_change:.3f}")
                    
                    # Calculate change since yesterday (24 hours ago)
                    history = state.get('gravity_history', [])
                    if history:
                        target_time = datetime.utcnow() - timedelta(hours=24)
                        closest_reading = None
                        min_diff = float('inf')
                        for reading in history:
                            time_diff = abs((reading['timestamp'] - target_time).total_seconds())
                            if time_diff < min_diff:
                                min_diff = time_diff
                                closest_reading = reading
                        if closest_reading and closest_reading['gravity'] is not None:
                            change_since_yesterday = closest_reading['gravity'] - current_gravity
                            lines.append(f"Change since yesterday: {change_since_yesterday:.3f}")
                except (ValueError, TypeError):
                    pass
        
        subject = f"{brewery_name} - Daily Report"
        body = "\n".join(lines)
        
        # Send notification directly using the proper subject line
        mode = (system_cfg.get('warning_mode') or 'NONE').upper()
        if mode in ('EMAIL', 'PUSH', 'BOTH'):
            attempt_send_notifications(subject, body)
        
        # Update last_daily_report timestamp to prevent duplicate reports
        if brewid not in batch_notification_state:
            batch_notification_state[brewid] = {}
        batch_notification_state[brewid]['last_daily_report'] = datetime.utcnow().isoformat()
        save_notification_state_to_config(color, brewid)

# --- Kasa command dedupe & rate limit -------------------------------------
_last_kasa_command = {}
_KASA_RATE_LIMIT_SECONDS = int(system_cfg.get("kasa_rate_limit_seconds", 10) or 10)
# Pending timeout must exceed the kasa_worker's worst-case completion time.
# Each attempt: plug.update (6s) + command + sleep (0.5s) + verify update (5s) ≈ 12s.
# With 3 attempts and inter-attempt delays of 0, 1, 2 s: worst case ≈ 38 s.
# Default of 90 s gives a safe margin and avoids false timeouts on slow networks.
_KASA_PENDING_TIMEOUT_SECONDS = int(system_cfg.get("kasa_pending_timeout_seconds", 90) or 90)
# How often (in seconds) to re-send a Kasa error notification while the failure persists.
# Default: 3600 s (1 hour). Keeps the user informed without flooding their inbox.
_KASA_ERROR_RENOTIFY_INTERVAL = int(system_cfg.get("kasa_error_renotify_interval", 3600) or 3600)

def _is_redundant_command(url, action, current_state):
    """
    Check if sending this command would be redundant based on current state.
    
    Returns True if command is redundant (should be skipped).
    
    SIMPLIFIED: Block commands that don't change state.
    The pending flag mechanism handles deduplication while commands are in-flight,
    so we don't need time-based logic here.
    """
    # If trying to send ON when already ON (or OFF when already OFF), it's redundant
    command_matches_state = (action == "on" and current_state) or (action == "off" and not current_state)
    
    # Return True (redundant) if command matches current state
    # Return False (not redundant) if state needs to change
    return command_matches_state

def _is_valid_controller_id(cid):
    """Return True if cid is a valid controller_id (0–2)."""
    return isinstance(cid, int) and 0 <= cid < _NUM_KASA_CONTROLLERS

def _should_send_kasa_command(url, action, controller):
    if not url:
        print(f"[TEMP_CONTROL] Blocking command (no URL configured)")
        return False
    if kasa_manager is None:
        print(f"[TEMP_CONTROL] Blocking command (kasa_manager not available)")
        return False
    if not kasa_manager.is_alive():
        print(f"[TEMP_CONTROL] Blocking command (kasa_manager worker not running)")
        return False
    cid = controller.get('controller_id', 0)
    if not _is_valid_controller_id(cid):
        print(f"[TEMP_CONTROL] Blocking command (invalid controller_id {cid})")
        return False

    # Check for timed-out pending flags and clear them
    if url == controller.get("heating_plug") and controller.get("heater_pending"):
        pending_action = controller.get("heater_pending_action")
        pending_since = controller.get("heater_pending_since")
        
        # If pending action is different from requested action, allow the new command
        # This prevents blocking opposite commands (e.g., don't block ON when OFF is pending)
        if pending_action != action:
            print(f"[TEMP_CONTROL] Allowing heating {action} command (different from pending {pending_action})")
            # Clear the old pending state since we're sending a different command
            controller["heater_pending"] = False
            controller["heater_pending_since"] = None
            controller["heater_pending_action"] = None
        elif pending_since is None:
            # Corrupted state: pending is True but timestamp is None
            # This can happen if pending_since is cleared but pending flag isn't
            # Clear all pending state to recover
            print(f"[TEMP_CONTROL] Clearing corrupted heater_pending state (no timestamp)")
            controller["heater_pending"] = False
            controller["heater_pending_action"] = None
        elif (time.time() - pending_since) > _KASA_PENDING_TIMEOUT_SECONDS:
            elapsed = time.time() - pending_since
            print(f"[TEMP_CONTROL] Clearing stuck heater_pending flag (pending for {elapsed:.1f}s)")
            # Timeout expired: we don't know if the command succeeded or failed.
            # Do NOT assume success (that caused heater_on=True while plug was physically OFF).
            # Mark a connection error so the dashboard shows the uncertain state, and
            # let the control loop retry the command on the next cycle.
            controller["heating_error"] = True
            controller["heating_error_msg"] = "Kasa command timed out — plug state unknown"
            controller["heater_pending"] = False
            controller["heater_pending_since"] = None
            controller["heater_pending_action"] = None
            # Log the timeout event
            append_control_log("kasa_command_timeout", {
                "mode": "heating",
                "action": action,
                "url": url,
                "timeout_seconds": _KASA_PENDING_TIMEOUT_SECONDS,
                "elapsed_seconds": round(elapsed, 1),
                "controller_id": controller.get("controller_id", 0),
                "tilt_color": controller.get("tilt_color", ""),
                "current_temp": controller.get("current_temp"),
                "low_limit": controller.get("low_limit"),
                "high_limit": controller.get("high_limit")
            })
        elif controller.get("heater_pending"):
            # Still pending and within timeout - block command
            elapsed = time.time() - pending_since if pending_since else 0
            print(f"[TEMP_CONTROL] Blocking heating {action} command (still pending {pending_action} for {elapsed:.1f}s)")
            return False
    
    if url == controller.get("cooling_plug") and controller.get("cooler_pending"):
        pending_action = controller.get("cooler_pending_action")
        pending_since = controller.get("cooler_pending_since")
        
        # If pending action is different from requested action, allow the new command
        # This prevents blocking opposite commands (e.g., don't block ON when OFF is pending)
        if pending_action != action:
            print(f"[TEMP_CONTROL] Allowing cooling {action} command (different from pending {pending_action})")
            # Clear the old pending state since we're sending a different command
            controller["cooler_pending"] = False
            controller["cooler_pending_since"] = None
            controller["cooler_pending_action"] = None
        elif pending_since is None:
            # Corrupted state: pending is True but timestamp is None
            # Clear all pending state to recover
            print(f"[TEMP_CONTROL] Clearing corrupted cooler_pending state (no timestamp)")
            controller["cooler_pending"] = False
            controller["cooler_pending_action"] = None
        elif (time.time() - pending_since) > _KASA_PENDING_TIMEOUT_SECONDS:
            elapsed = time.time() - pending_since
            print(f"[TEMP_CONTROL] Clearing stuck cooler_pending flag (pending for {elapsed:.1f}s)")
            # Timeout expired: we don't know if the command succeeded or failed.
            # Do NOT assume success (that caused cooler_on=True while plug was physically OFF).
            # Mark a connection error so the dashboard shows the uncertain state, and
            # let the control loop retry the command on the next cycle.
            controller["cooling_error"] = True
            controller["cooling_error_msg"] = "Kasa command timed out — plug state unknown"
            controller["cooler_pending"] = False
            controller["cooler_pending_since"] = None
            controller["cooler_pending_action"] = None
            # Log the timeout event
            append_control_log("kasa_command_timeout", {
                "mode": "cooling",
                "action": action,
                "url": url,
                "timeout_seconds": _KASA_PENDING_TIMEOUT_SECONDS,
                "elapsed_seconds": round(elapsed, 1),
                "controller_id": controller.get("controller_id", 0),
                "tilt_color": controller.get("tilt_color", ""),
                "current_temp": controller.get("current_temp"),
                "low_limit": controller.get("low_limit"),
                "high_limit": controller.get("high_limit")
            })
        elif controller.get("cooler_pending"):
            # Still pending and within timeout - block command
            elapsed = time.time() - pending_since if pending_since else 0
            print(f"[TEMP_CONTROL] Blocking cooling {action} command (still pending {pending_action} for {elapsed:.1f}s)")
            return False
    
    # Check for redundant commands based on current state
    # This prevents sending ON when already ON, or OFF when already OFF
    # Exception: Skip redundant check when there is an active error - the plug's physical
    # state is uncertain (e.g. the plug may have power-cycled and reset to OFF while the
    # system still thinks it is ON), so we must re-send the command to resync.
    if url == controller.get("heating_plug"):
        if controller.get("heating_error"):
            print(f"[TEMP_CONTROL] Allowing heating {action} command (error state - bypassing redundant check to resync plug)")
        else:
            heater_on = controller.get("heater_on", False)
            if _is_redundant_command(url, action, heater_on):
                print(f"[TEMP_CONTROL] Blocking heating {action} command (redundant - heater already {('ON' if heater_on else 'OFF')})")
                return False

    if url == controller.get("cooling_plug"):
        if controller.get("cooling_error"):
            print(f"[TEMP_CONTROL] Allowing cooling {action} command (error state - bypassing redundant check to resync plug)")
        else:
            cooler_on = controller.get("cooler_on", False)
            if _is_redundant_command(url, action, cooler_on):
                print(f"[TEMP_CONTROL] Blocking cooling {action} command (redundant - cooler already {('ON' if cooler_on else 'OFF')})")
                return False
    
    # Rate limiting: prevent the same command from being sent too frequently
    last = _last_kasa_command.get(url)
    if last and last.get("action") == action:
        time_since_last = time.time() - last.get("ts", 0.0)
        if time_since_last < _KASA_RATE_LIMIT_SECONDS:
            print(f"[TEMP_CONTROL] Blocking {action} command (rate limited - last sent {time_since_last:.1f}s ago)")
            return False
    return True

def _record_kasa_command(url, action):
    _last_kasa_command[url] = {"action": action, "ts": time.time()}

def _check_and_restart_kasa_proc():
    """Watchdog: verify the kasa_manager worker subprocess has not exited.

    If the process has died, restarts it and clears all pending plug flags
    for every controller so that new commands can be issued immediately.

    Called at the top of every periodic_temp_control cycle.
    """
    if kasa_manager is None:
        return
    if not kasa_manager.restart_if_dead():
        return  # process is healthy — nothing to do

    # Worker died and was restarted: clear all pending flags and mark errors
    # so the control loop can issue fresh commands on the next cycle.
    for ctrl in temp_cfg.get('controllers', []):
        ctrl['heater_pending']        = False
        ctrl['heater_pending_since']  = None
        ctrl['heater_pending_action'] = None
        ctrl['cooler_pending']        = False
        ctrl['cooler_pending_since']  = None
        ctrl['cooler_pending_action'] = None
        ctrl['heating_error']         = True
        ctrl['heating_error_msg']     = 'kasa_manager worker restarted — plug state unknown'
        ctrl['cooling_error']         = True
        ctrl['cooling_error_msg']     = 'kasa_manager worker restarted — plug state unknown'

# --- Control functions -----------------------------------------------------
def control_heating(state, controller):
    enabled = controller.get("enable_heating")
    url = controller.get("heating_plug", "")
    if not enabled or not url:
        controller["heater_pending"] = False
        controller["heater_pending_since"] = None
        controller["heater_pending_action"] = None
        controller["heater_on"] = False
        # Clear heating errors when heating is disabled
        controller["heating_error"] = False
        controller["heating_error_msg"] = ""
        controller["heating_error_notified"] = False
        return
    
    # Simple safety rule: No connection = no plugs turn ON
    # If Tilt is not active (no connection/signal) and we're trying to turn plug ON, block it
    if state == "on" and not is_control_tilt_active(controller):
        print(f"[TEMP_CONTROL] Blocking heating ON command - no Tilt connection/signal")
        print(f"[TEMP_CONTROL] Safety: Cannot turn plugs ON without active Tilt signal")
        
        # Use trigger pattern: Report once when issue detected, flip trigger
        # Only log and notify if trigger is not already flipped
        if not controller.get("heating_blocked_trigger"):
            tilt_color = controller.get("tilt_color", "")
            
            # Log the event
            append_control_log("temp_control_blocked_on", {
                "mode": "heating",
                "controller_id": controller.get("controller_id", 0),
                "tilt_color": tilt_color,
                "reason": "Tilt connection lost - cannot turn heating ON",
                "low_limit": controller.get("low_limit"),
                "high_limit": controller.get("high_limit")
            })
            
            # Send notification
            send_plug_blocked_notification("heating", tilt_color)
            
            # Flip the trigger - prevents repeated logging/notifications
            controller["heating_blocked_trigger"] = True
        
        # Don't send the ON command - plugs stay OFF for safety
        return
    
    # When issue is corrected (Tilt active or turning OFF), reset the trigger
    if state == "off" or is_control_tilt_active(controller):
        if controller.get("heating_blocked_trigger"):
            controller["heating_blocked_trigger"] = False
    
    # If turning OFF due to no Tilt connection, log it as a safety action
    if state == "off" and not is_control_tilt_active(controller) and controller.get("heater_on"):
        print(f"[TEMP_CONTROL] Allowing heating OFF command - safety shutdown (no Tilt connection)")
        
        # Use trigger pattern: Report once when issue detected, flip trigger
        if not controller.get("heating_safety_off_trigger"):
            tilt_color = controller.get("tilt_color", "")
            
            # Log the event
            append_control_log("temp_control_safety_off", {
                "mode": "heating",
                "controller_id": controller.get("controller_id", 0),
                "tilt_color": tilt_color,
                "reason": "Tilt connection lost - turning heating OFF for safety",
                "low_limit": controller.get("low_limit"),
                "high_limit": controller.get("high_limit")
            })
            
            # Send notification
            send_plug_safety_off_notification("heating", tilt_color)
            
            # Flip the trigger - prevents repeated logging/notifications
            controller["heating_safety_off_trigger"] = True
    
    # When issue is corrected (Tilt active), reset the trigger
    if is_control_tilt_active(controller):
        if controller.get("heating_safety_off_trigger"):
            controller["heating_safety_off_trigger"] = False
    
    if not _should_send_kasa_command(url, state, controller):
        print(f"[TEMP_CONTROL] Skipping heating {state} command (redundant or rate-limited)")
        return
    cid = controller.get('controller_id', 0)
    # In --plug mode all plugs use IOT/port 9999; ignore any per-controller port override.
    port = None if _FORCE_IOT_PORT else controller.get('heating_plug_port')
    print(f"[TEMP_CONTROL] Sending heating {state.upper()} command to {url}")
    # Log the command being sent
    log_kasa_command('heating', url, state)
    log_kasa_diag('info', f'Queuing heating {state.upper()} command',
                  url=url, controller_id=cid)
    kasa_manager.send(cid, 'heating', url, state, port=port)
    # NOTE: _record_kasa_command is now called in kasa_result_listener only on success
    # This allows failed commands to be retried without rate limiting
    controller["heater_pending"] = True
    controller["heater_pending_since"] = time.time()
    controller["heater_pending_action"] = state

def control_cooling(state, controller):
    enabled = controller.get("enable_cooling")
    url = controller.get("cooling_plug", "")
    if not enabled or not url:
        controller["cooler_pending"] = False
        controller["cooler_pending_since"] = None
        controller["cooler_pending_action"] = None
        controller["cooler_on"] = False
        # Clear cooling errors when cooling is disabled
        controller["cooling_error"] = False
        controller["cooling_error_msg"] = ""
        controller["cooling_error_notified"] = False
        return
    
    # Simple safety rule: No connection = no plugs turn ON
    # If Tilt is not active (no connection/signal) and we're trying to turn plug ON, block it
    if state == "on" and not is_control_tilt_active(controller):
        print(f"[TEMP_CONTROL] Blocking cooling ON command - no Tilt connection/signal")
        print(f"[TEMP_CONTROL] Safety: Cannot turn plugs ON without active Tilt signal")
        
        # Use trigger pattern: Report once when issue detected, flip trigger
        # Only log and notify if trigger is not already flipped
        if not controller.get("cooling_blocked_trigger"):
            tilt_color = controller.get("tilt_color", "")
            
            # Log the event
            append_control_log("temp_control_blocked_on", {
                "mode": "cooling",
                "controller_id": controller.get("controller_id", 0),
                "tilt_color": tilt_color,
                "reason": "Tilt connection lost - cannot turn cooling ON",
                "low_limit": controller.get("low_limit"),
                "high_limit": controller.get("high_limit")
            })
            
            # Send notification
            send_plug_blocked_notification("cooling", tilt_color)
            
            # Flip the trigger - prevents repeated logging/notifications
            controller["cooling_blocked_trigger"] = True
        
        # Don't send the ON command - plugs stay OFF for safety
        return
    
    # When issue is corrected (Tilt active or turning OFF), reset the trigger
    if state == "off" or is_control_tilt_active(controller):
        if controller.get("cooling_blocked_trigger"):
            controller["cooling_blocked_trigger"] = False
    
    # If turning OFF due to no Tilt connection, log it as a safety action
    if state == "off" and not is_control_tilt_active(controller) and controller.get("cooler_on"):
        print(f"[TEMP_CONTROL] Allowing cooling OFF command - safety shutdown (no Tilt connection)")
        
        # Use trigger pattern: Report once when issue detected, flip trigger
        if not controller.get("cooling_safety_off_trigger"):
            tilt_color = controller.get("tilt_color", "")
            
            # Log the event
            append_control_log("temp_control_safety_off", {
                "mode": "cooling",
                "controller_id": controller.get("controller_id", 0),
                "tilt_color": tilt_color,
                "reason": "Tilt connection lost - turning cooling OFF for safety",
                "low_limit": controller.get("low_limit"),
                "high_limit": controller.get("high_limit")
            })
            
            # Send notification
            send_plug_safety_off_notification("cooling", tilt_color)
            
            # Flip the trigger - prevents repeated logging/notifications
            controller["cooling_safety_off_trigger"] = True
    
    # When issue is corrected (Tilt active), reset the trigger
    if is_control_tilt_active(controller):
        if controller.get("cooling_safety_off_trigger"):
            controller["cooling_safety_off_trigger"] = False
    
    if not _should_send_kasa_command(url, state, controller):
        print(f"[TEMP_CONTROL] Skipping cooling {state} command (redundant or rate-limited)")
        return
    cid = controller.get('controller_id', 0)
    # In --plug mode all plugs use IOT/port 9999; ignore any per-controller port override.
    port = None if _FORCE_IOT_PORT else controller.get('cooling_plug_port')
    print(f"[TEMP_CONTROL] Sending cooling {state.upper()} command to {url}")
    # Log the command being sent
    log_kasa_command('cooling', url, state)
    log_kasa_diag('info', f'Queuing cooling {state.upper()} command',
                  url=url, controller_id=cid)
    kasa_manager.send(cid, 'cooling', url, state, port=port)
    # NOTE: _record_kasa_command is now called in kasa_result_listener only on success
    # This allows failed commands to be retried without rate limiting
    controller["cooler_pending"] = True
    controller["cooler_pending_since"] = time.time()
    controller["cooler_pending_action"] = state

# --- Temperature control logic (normalized + limited logging) -------------
def temperature_control_logic():
    """
    Main control loop for all 3 controllers.
    
    Per user requirement: Controllers that are OFF (temp_control_active == False) 
    are skipped in the operational loop. Only active controllers are processed.
    """
    # Loop over all 3 controllers
    if 'controllers' in temp_cfg:
        for controller in temp_cfg['controllers']:
            # SKIP controllers that are OFF (per user comment feedback)
            # Only process controllers where temp_control_active is True
            if not controller.get('temp_control_active', False):
                # Still set status to indicate monitor is off
                controller['status'] = "Monitor Off"
                # Turn off plugs if they were on
                if controller.get('heater_on') or controller.get('cooler_on'):
                    control_heating("off", controller)
                    control_cooling("off", controller)
                continue
            
            # Process this active controller
            temperature_control_logic_single(controller)

def temperature_control_logic_single(temp_cfg):
    """
    Control logic for a single temperature controller.

    Important behavior change:
    - If temp_control_enabled is False, the function will NOT modify or clear the stored
      temp_cfg fields (heater_on, cooler_on, pending flags, limits, plugs, etc.).
      It only sets a 'Disabled' status and returns. This preserves configuration so that
      when the controller is turned back on the previous settings are used as the
      starting point.
    - All control actions (control_heating/control_cooling) are skipped while disabled.
    
    Args:
        temp_cfg (dict): Controller configuration dictionary (one of the 3 controllers)
    """
    # If the overall temp control subsystem is disabled, do not perform any actions.
    # Preserve the saved configuration and active-state flags — don't clear them.
    if not temp_cfg.get("temp_control_enabled", True):
        temp_cfg['status'] = "Disabled"
        # Do NOT change heater_on/cooler_on/heater_pending/cooler_pending or limits here.
        # Returning early prevents any control commands from being issued.
        return

    enable_heat = bool(temp_cfg.get("enable_heating"))
    enable_cool = bool(temp_cfg.get("enable_cooling"))
    
    # If the monitoring switch is turned OFF, turn off all plugs immediately
    # but preserve configuration so settings remain when monitor is turned back ON
    if not temp_cfg.get("temp_control_active", False):
        control_heating("off", temp_cfg)
        control_cooling("off", temp_cfg)
        temp_cfg['status'] = "Monitor Off"
        # Preserve all settings and return early to prevent any control actions
        return
    if enable_heat and enable_cool:
        temp_cfg['mode'] = "Heating & Cooling"
    elif enable_heat:
        temp_cfg['mode'] = "Heating"
    elif enable_cool:
        temp_cfg['mode'] = "Cooling"
    else:
        temp_cfg['mode'] = "Off"

    # Always refresh temperature from the live tilt to keep temp control
    # card in sync with the tilt card display (fixes discrepancy between them).
    temp_from_tilt = get_current_temp_for_control_tilt(temp_cfg)
    if temp_from_tilt is not None:
        try:
            temp = float(temp_from_tilt)
            temp_cfg['current_temp'] = round(temp, 1)
            # Update timestamp when temperature is read
            temp_cfg['last_reading_time'] = datetime.utcnow().isoformat() + "Z"
        except Exception:
            temp = temp_cfg.get("current_temp")
    else:
        # Tilt unavailable - fall back to last cached value
        temp = temp_cfg.get("current_temp")

    low = temp_cfg.get("low_limit")
    high = temp_cfg.get("high_limit")
    
    # NOTE: Limits are guaranteed to be valid float values by ensure_temp_defaults()
    # and periodic_temp_control() validation, so no additional validation needed here.
    # This ensures SAMPLE events and control logic use the exact same values.
    
    # Check if temp control monitoring is active
    is_monitoring_active = bool(temp_cfg.get("temp_control_active"))

    if not temp_cfg.get("control_initialized"):
        if enable_heat or enable_cool:
            append_control_log("temp_control_mode", {
                "controller_id": temp_cfg.get("controller_id", 0),
                "low_limit": low,
                "current_temp": temp,
                "high_limit": high,
                "tilt_color": temp_cfg.get("tilt_color", "")
            })
        temp_cfg["control_initialized"] = True
        temp_cfg["last_logged_low_limit"] = low
        temp_cfg["last_logged_high_limit"] = high
        temp_cfg["last_logged_enable_heating"] = enable_heat
        temp_cfg["last_logged_enable_cooling"] = enable_cool

    if (temp_cfg.get("last_logged_low_limit") != low or
        temp_cfg.get("last_logged_high_limit") != high or
        temp_cfg.get("last_logged_enable_heating") != enable_heat or
        temp_cfg.get("last_logged_enable_cooling") != enable_cool):
        if enable_heat or enable_cool:
            append_control_log("temp_control_mode_changed", {
                "controller_id": temp_cfg.get("controller_id", 0),
                "low_limit": low,
                "current_temp": temp,
                "high_limit": high,
                "tilt_color": temp_cfg.get("tilt_color", "")
            })
        temp_cfg["last_logged_low_limit"] = low
        temp_cfg["last_logged_high_limit"] = high
        temp_cfg["last_logged_enable_heating"] = enable_heat
        temp_cfg["last_logged_enable_cooling"] = enable_cool

    # SAFETY: Check if control Tilt is active (within timeout)
    # If any Tilt being used for temp control is inactive, turn off all plugs immediately
    # This includes both explicitly assigned Tilts and fallback Tilts
    if not is_control_tilt_active(temp_cfg):
        control_heating("off", temp_cfg)
        control_cooling("off", temp_cfg)
        
        # Get the actual Tilt color being used (may be explicitly assigned or fallback)
        actual_tilt_color = get_control_tilt_color(temp_cfg)
        assigned_tilt_color = temp_cfg.get("tilt_color", "")
        
        # Set status message indicating which Tilt triggered shutdown
        if assigned_tilt_color:
            temp_cfg["status"] = f"Control Tilt Inactive - Safety Shutdown ({assigned_tilt_color})"
        elif actual_tilt_color:
            temp_cfg["status"] = f"Control Tilt Inactive - Safety Shutdown (using {actual_tilt_color} as fallback)"
        else:
            temp_cfg["status"] = "Control Tilt Inactive - Safety Shutdown"
        
        # Log this safety event and send notification
        if not temp_cfg.get("safety_shutdown_logged"):
            # Use the actual Tilt color for logging
            tilt_color = actual_tilt_color or assigned_tilt_color or "Unknown"
            append_control_log("temp_control_safety_shutdown", {
                "tilt_color": tilt_color,
                "assigned_tilt": assigned_tilt_color,
                "actual_tilt": actual_tilt_color or "None",
                "reason": "Control Tilt inactive beyond timeout",
                "low_limit": low,
                "high_limit": high
            })
            temp_cfg["safety_shutdown_logged"] = True
            
            # Send safety shutdown notification
            send_safety_shutdown_notification(tilt_color, low, high)
        return
    else:
        # Reset the safety shutdown flag when Tilt becomes active again
        if temp_cfg.get("safety_shutdown_logged"):
            temp_cfg["safety_shutdown_logged"] = False

    if temp is None:
        control_heating("off", temp_cfg)
        control_cooling("off", temp_cfg)
        temp_cfg["status"] = "Device Offline"
        return

    current_action = None
    
    # Safety check: Ensure low_limit is less than high_limit
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        if low >= high:
            temp_cfg["status"] = "Configuration Error: Low limit must be less than high limit"
            control_heating("off", temp_cfg)
            control_cooling("off", temp_cfg)
            return

    # When both heating and cooling are enabled, compute the midpoint once.
    # Heating turns off at midpoint; cooling turns off at midpoint.
    # This prevents the two systems from fighting each other.
    both_active = enable_heat and enable_cool
    if both_active and low is not None and high is not None:
        midpoint = (low + high) / 2.0
    else:
        midpoint = None

    # Heating control:
    # - Turn ON when temp <= low_limit
    # - Turn OFF at midpoint (both H+C) or high_limit (heating only)
    # Note: if the off-threshold is None (no high limit configured), heating will not
    # turn off between readings — this matches the original single-mode behaviour.
    if enable_heat:
        heat_off_threshold = midpoint if both_active else high

        if low is not None and temp <= low:
            # Temperature at or below low limit - turn heating ON
            control_heating("on", temp_cfg)
            current_action = "Heating"
            # Log with trigger when temp goes BELOW low limit (strictly less than)
            # Notification only when limit is EXCEEDED, not when equal
            if temp < low and temp_cfg.get("below_limit_trigger_armed") and is_monitoring_active:
                append_control_log("temp_below_low_limit", {"controller_id": temp_cfg.get("controller_id", 0), "low_limit": low, "current_temp": temp, "high_limit": high, "tilt_color": temp_cfg.get("tilt_color", "")})
                temp_cfg["below_limit_logged"] = True
                # Send notification if enabled
                send_temp_control_notification("temp_below_low_limit", temp, low, high, temp_cfg.get("tilt_color", ""))
                temp_cfg["below_limit_trigger_armed"] = False
                # Arm the above_limit trigger for when temp rises to high limit
                temp_cfg["above_limit_trigger_armed"] = True
        elif heat_off_threshold is not None and temp >= heat_off_threshold:
            # Temperature at or above turn-off threshold - turn heating OFF
            control_heating("off", temp_cfg)
            # Arm the below_limit trigger for when temp drops to low limit again
            temp_cfg["below_limit_trigger_armed"] = True
        # else: temperature is between low and turn-off threshold - maintain current state
        # (don't change heating state, let it continue)
    else:
        control_heating("off", temp_cfg)

    # Cooling control:
    # - Turn ON when temp >= high_limit
    # - Turn OFF at midpoint (both H+C) or low_limit (cooling only)
    # Note: if the off-threshold is None (no low limit configured), cooling will not
    # turn off between readings — this matches the original single-mode behaviour.
    if enable_cool:
        cool_off_threshold = midpoint if both_active else low

        if high is not None and temp >= high:
            # Temperature at or above high limit - turn cooling ON
            control_cooling("on", temp_cfg)
            current_action = "Cooling"
            # Log with trigger when temp goes ABOVE high limit (strictly greater than)
            # Notification only when limit is EXCEEDED, not when equal
            if temp > high and temp_cfg.get("above_limit_trigger_armed") and is_monitoring_active:
                append_control_log("temp_above_high_limit", {"controller_id": temp_cfg.get("controller_id", 0), "low_limit": low, "current_temp": temp, "high_limit": high, "tilt_color": temp_cfg.get("tilt_color", "")})
                temp_cfg["above_limit_logged"] = True
                # Send notification if enabled
                send_temp_control_notification("temp_above_high_limit", temp, low, high, temp_cfg.get("tilt_color", ""))
                temp_cfg["above_limit_trigger_armed"] = False
                # Arm the below_limit trigger for when temp drops to low limit
                temp_cfg["below_limit_trigger_armed"] = True
        elif cool_off_threshold is not None and temp <= cool_off_threshold:
            # Temperature at or below turn-off threshold - turn cooling OFF
            control_cooling("off", temp_cfg)
            # Arm the above_limit trigger for when temp rises to high limit again
            temp_cfg["above_limit_trigger_armed"] = True
        # else: temperature is between turn-off threshold and high - maintain current state
        # (don't change cooling state, let it continue)
    else:
        control_cooling("off", temp_cfg)
    
    # Update current_action based on actual plug state
    if temp_cfg.get("heater_on"):
        current_action = "Heating"
    elif temp_cfg.get("cooler_on"):
        current_action = "Cooling"
    
    # Safety check: Both heating and cooling should never be ON simultaneously
    if enable_heat and enable_cool:
        if temp_cfg.get("heater_on") and temp_cfg.get("cooler_on"):
            # This should never happen, but if it does, turn both off for safety
            control_heating("off", temp_cfg)
            control_cooling("off", temp_cfg)
            temp_cfg["status"] = "Safety Error: Both heating and cooling were ON"
            append_control_log("temp_control_safety_shutdown", {
                "controller_id": temp_cfg.get("controller_id", 0),
                "tilt_color": temp_cfg.get("tilt_color", ""),
                "reason": "Both heating and cooling active simultaneously",
                "low_limit": low,
                "high_limit": high,
                "current_temp": temp
            })
            return

    try:
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and (low <= temp <= high):
            # Temperature is in range
            # Log with trigger when entering range
            if temp_cfg.get("in_range_trigger_armed") and is_monitoring_active:
                append_control_log("temp_in_range", {"controller_id": temp_cfg.get("controller_id", 0), "low_limit": low, "current_temp": temp, "high_limit": high, "tilt_color": temp_cfg.get("tilt_color", "")})
                temp_cfg["in_range_trigger_armed"] = False
            # Do NOT rearm out-of-range triggers here - they are rearmed when opposite limit is reached
            temp_cfg["status"] = "In Range"
            return
        else:
            # Temperature is out of range - re-arm in_range trigger
            temp_cfg["in_range_trigger_armed"] = True
    except Exception:
        pass

    if current_action == "Heating":
        temp_cfg["status"] = "Heating"
    elif current_action == "Cooling":
        temp_cfg["status"] = "Cooling"
    else:
        temp_cfg["status"] = "Idle"

# --- Swapped plug detection ------------------------------------------------
def check_for_swapped_plugs():
    """
    Detect if heating and cooling plugs are swapped by monitoring temperature
    trends after a plug is activated. Checks all active controllers.
    
    Logic:
    - When heating turns ON, temperature should rise (or stabilize if at setpoint)
    - When cooling turns ON, temperature should drop (or stabilize if at setpoint)
    - If the opposite happens consistently, plugs are likely swapped
    
    Detection thresholds:
    - Monitor for at least 10 minutes after plug activation
    - Require temperature to move 1.5°F in wrong direction
    - Clear detection when plugs turn OFF or swap is acknowledged
    """
    # Check all controllers
    for controller in temp_cfg.get('controllers', []):
        # Only check if temp control is active for this controller
        if not controller.get("temp_control_active", False):
            continue
        
        current_temp = controller.get("current_temp")
        if current_temp is None:
            continue
        
        controller_id = controller.get("controller_id", 0)
        
        # Check heating plug (should cause temperature to RISE)
        if controller.get("heater_on"):
            baseline_temp = controller.get("heater_baseline_temp")
            baseline_time = controller.get("heater_baseline_time")
            
            if baseline_temp is not None and baseline_time is not None:
                # Check if enough time has passed (10 minutes = 600 seconds)
                time_elapsed = time.time() - baseline_time
                if time_elapsed >= 600:  # 10 minutes
                    # Temperature should have risen or stayed stable
                    # If it dropped significantly, heating plug may be swapped with cooling
                    temp_change = current_temp - baseline_temp
                    
                    if temp_change < -1.5:  # Temperature dropped 1.5°F or more
                        # Heating is ON but temperature is dropping - likely swapped!
                        if not controller.get("swapped_plugs_detected") or controller.get("swapped_plug_type") != "heating":
                            controller["swapped_plugs_detected"] = True
                            controller["swapped_plug_type"] = "heating"
                            controller["swapped_plugs_notified"] = False  # Allow new notification
                            print(f"[SWAPPED_PLUG] ⚠️  Controller {controller_id}: DETECTED: Heating ON but temp dropped {abs(temp_change):.1f}°F")
                            print(f"[SWAPPED_PLUG] Controller {controller_id}: Baseline: {baseline_temp:.1f}°F, Current: {current_temp:.1f}°F")
                            print(f"[SWAPPED_PLUG] Controller {controller_id}: Possible cause: Heating plug may be connected to cooling device")
                            
                            # Log the event
                            append_control_log("swapped_plugs_detected", {
                                "controller_id": controller_id,
                                "plug_type": "heating",
                                "expected_behavior": "temperature rise",
                                "actual_behavior": "temperature drop",
                                "temp_change": round(temp_change, 1),
                                "baseline_temp": round(baseline_temp, 1),
                                "current_temp": round(current_temp, 1),
                                "time_elapsed_minutes": round(time_elapsed / 60, 1),
                                "low_limit": controller.get("low_limit"),
                                "high_limit": controller.get("high_limit"),
                                "tilt_color": controller.get("tilt_color", "")
                            })
                            
                            # Send notification
                            send_swapped_plug_notification("heating", baseline_temp, current_temp, temp_change, time_elapsed, controller)
        
        # Check cooling plug (should cause temperature to DROP)
        if controller.get("cooler_on"):
            baseline_temp = controller.get("cooler_baseline_temp")
            baseline_time = controller.get("cooler_baseline_time")
            
            if baseline_temp is not None and baseline_time is not None:
                # Check if enough time has passed (10 minutes = 600 seconds)
                time_elapsed = time.time() - baseline_time
                if time_elapsed >= 600:  # 10 minutes
                    # Temperature should have dropped or stayed stable
                    # If it rose significantly, cooling plug may be swapped with heating
                    temp_change = current_temp - baseline_temp
                    
                    if temp_change > 1.5:  # Temperature rose 1.5°F or more
                        # Cooling is ON but temperature is rising - likely swapped!
                        if not controller.get("swapped_plugs_detected") or controller.get("swapped_plug_type") != "cooling":
                            controller["swapped_plugs_detected"] = True
                            controller["swapped_plug_type"] = "cooling"
                            controller["swapped_plugs_notified"] = False  # Allow new notification
                            print(f"[SWAPPED_PLUG] ⚠️  Controller {controller_id}: DETECTED: Cooling ON but temp rose {temp_change:.1f}°F")
                            print(f"[SWAPPED_PLUG] Controller {controller_id}: Baseline: {baseline_temp:.1f}°F, Current: {current_temp:.1f}°F")
                            print(f"[SWAPPED_PLUG] Controller {controller_id}: Possible cause: Cooling plug may be connected to heating device")
                            
                            # Log the event
                            append_control_log("swapped_plugs_detected", {
                                "controller_id": controller_id,
                                "plug_type": "cooling",
                                "expected_behavior": "temperature drop",
                                "actual_behavior": "temperature rise",
                                "temp_change": round(temp_change, 1),
                                "baseline_temp": round(baseline_temp, 1),
                                "current_temp": round(current_temp, 1),
                                "time_elapsed_minutes": round(time_elapsed / 60, 1),
                                "low_limit": controller.get("low_limit"),
                                "high_limit": controller.get("high_limit"),
                                "tilt_color": controller.get("tilt_color", "")
                            })
                            
                            # Send notification
                            send_swapped_plug_notification("cooling", baseline_temp, current_temp, temp_change, time_elapsed, controller)

def send_swapped_plug_notification(plug_type, baseline_temp, current_temp, temp_change, time_elapsed, controller):
    """
    Send notification when swapped plugs are detected.
    Only sends once per detection period to avoid spam.
    
    Args:
        plug_type: "heating" or "cooling"
        baseline_temp: Temperature when plug was turned on
        current_temp: Current temperature
        temp_change: Change in temperature
        time_elapsed: Seconds since plug was turned on
        controller: Controller dictionary
    """
    # Check if already notified for this detection
    if controller.get("swapped_plugs_notified"):
        return
    
    # Mark as notified
    controller["swapped_plugs_notified"] = True
    
    controller_id = controller.get("controller_id", 0)
    tilt_color = controller.get("tilt_color", "")
    
    # Build notification message
    if plug_type == "heating":
        subject = f"⚠️ Swapped Plug Detected: Controller {controller_id + 1} Heating Plug"
        body = (
            f"WARNING: Controller {controller_id + 1} heating plug may be connected to a COOLING device!\n\n"
            f"The heating plug was turned ON, but temperature DROPPED instead of rising.\n\n"
            f"Temperature Change:\n"
            f"  • Started at: {baseline_temp:.1f}°F\n"
            f"  • Now at: {current_temp:.1f}°F\n"
            f"  • Change: {temp_change:.1f}°F (DROPPED)\n"
            f"  • Time elapsed: {int(time_elapsed / 60)} minutes\n"
            f"  • Tilt: {tilt_color or 'Not assigned'}\n\n"
            f"ACTION REQUIRED:\n"
            f"1. Turn OFF temperature control immediately\n"
            f"2. Verify heating plug is connected to heating device (not cooler)\n"
            f"3. Check cooling plug is connected to cooling device (not heater)\n"
            f"4. Fix connections and restart temperature control\n\n"
            f"This usually happens when the heater and cooler are accidentally swapped."
        )
    else:  # cooling
        subject = f"⚠️ Swapped Plug Detected: Controller {controller_id + 1} Cooling Plug"
        body = (
            f"WARNING: Controller {controller_id + 1} cooling plug may be connected to a HEATING device!\n\n"
            f"The cooling plug was turned ON, but temperature ROSE instead of dropping.\n\n"
            f"Temperature Change:\n"
            f"  • Started at: {baseline_temp:.1f}°F\n"
            f"  • Now at: {current_temp:.1f}°F\n"
            f"  • Change: +{temp_change:.1f}°F (ROSE)\n"
            f"  • Time elapsed: {int(time_elapsed / 60)} minutes\n"
            f"  • Tilt: {tilt_color or 'Not assigned'}\n\n"
            f"ACTION REQUIRED:\n"
            f"1. Turn OFF temperature control immediately\n"
            f"2. Verify cooling plug is connected to cooling device (not heater)\n"
            f"3. Check heating plug is connected to heating device (not cooler)\n"
            f"4. Fix connections and restart temperature control\n\n"
            f"This usually happens when the heater and cooler are accidentally swapped."
        )
    
    # Use the pending notification queue to send alert
    attempt_send_notifications(
        subject=subject,
        body=body
    )

# --- kasa result listener (log confirmed ON/OFF events) --------------------
def kasa_result_listener():
    """Listen on the KasaManager result queue for on/off results from the worker.

    This is a single thread that handles results for all controllers.
    The result dict contains 'controller_id' and 'role' ('heating'|'cooling')
    to identify which controller and plug the result belongs to.
    """
    while True:
        try:
            rq = kasa_manager.result_queue if kasa_manager else None
            if rq is None:
                time.sleep(1)
                continue
            try:
                result = rq.get(timeout=5)
            except Exception:
                continue
            role    = result.get('role') or result.get('mode')  # support both keys
            action  = result.get('action')
            success = result.get('success', False)
            url     = result.get('url', '')
            error   = result.get('error', '')
            cid     = result.get('controller_id')

            print(f"[KASA_RESULT] Received result: role={role}, action={action}, success={success}, url={url}, error={error}")
            # Write to kasa_errors.log so the user can trace what the worker did
            if success:
                log_kasa_diag('info', f'Plug command result: {role} {action} OK',
                              url=url, role=role, action=action)
            else:
                log_kasa_diag('error', f'Plug command result: {role} {action} FAILED',
                              url=url, role=role, action=action, error=error)

            # Log the response to kasa_activity_monitoring.jsonl
            log_kasa_command(role, url, action, success=success, error=error if not success else None)

            # Find the controller that owns this plug
            controller = None
            # First try by controller_id if present
            if cid is not None and _is_valid_controller_id(cid):
                for ctrl in temp_cfg.get('controllers', []):
                    if ctrl.get('controller_id') == cid:
                        controller = ctrl
                        break
            # Fall back to URL-based lookup
            if controller is None:
                for ctrl in temp_cfg.get('controllers', []):
                    if role == 'heating' and url == ctrl.get("heating_plug"):
                        controller = ctrl
                        break
                    elif role == 'cooling' and url == ctrl.get("cooling_plug"):
                        controller = ctrl
                        break

            if controller is None:
                print(f"[KASA_RESULT] WARNING: Could not find controller for {role} plug at {url}")
                continue

            if role == 'heating':
                controller["heater_pending"] = False
                controller["heater_pending_since"] = None
                controller["heater_pending_action"] = None
                if success:
                    # Track previous state to detect actual state changes
                    previous_state = controller.get("heater_on", False)
                    new_state = (action == 'on')
                    controller["heater_on"] = new_state
                    controller["heating_error"] = False
                    controller["heating_error_msg"] = ""
                    # Reset notification tracking when plug starts working again
                    controller["heating_error_notified"] = False
                    controller["heating_kasa_error_since"] = 0
                    controller["heating_kasa_error_notified_at"] = 0
                    
                    # Track baseline temperature when heating turns ON
                    if new_state and not previous_state:
                        # Heating just turned ON - record baseline for swapped plug detection
                        controller["heater_baseline_temp"] = controller.get("current_temp")
                        controller["heater_baseline_time"] = time.time()
                        print(f"[SWAPPED_PLUG] Controller {controller.get('controller_id', 0)}: Heating activated - baseline temp: {controller.get('current_temp')}°F")
                    elif not new_state:
                        # Heating turned OFF - clear baseline and detection
                        controller["heater_baseline_temp"] = None
                        controller["heater_baseline_time"] = None
                        if controller.get("swapped_plug_type") == "heating":
                            controller["swapped_plugs_detected"] = False
                            controller["swapped_plugs_notified"] = False
                            controller["swapped_plug_type"] = ""
                    
                    # Only log and notify if state actually changed
                    if new_state != previous_state:
                        event = "heating_on" if action == 'on' else "heating_off"
                        print(f"[KASA_RESULT] ✓ Controller {controller.get('controller_id', 0)}: Heating plug {action.upper()} confirmed - state changed from {previous_state} to {new_state}")
                        append_control_log(event, {
                            "controller_id": controller.get("controller_id", 0),
                            "low_limit": controller.get("low_limit"), 
                            "current_temp": controller.get("current_temp"), 
                            "high_limit": controller.get("high_limit"), 
                            "tilt_color": controller.get("tilt_color", "")
                        })
                        # Send notification if enabled (user can choose to enable/disable)
                        send_temp_control_notification(event, controller.get("current_temp", 0), controller.get("low_limit", 0), controller.get("high_limit", 0), controller.get("tilt_color", ""))
                    else:
                        print(f"[KASA_RESULT] ✓ Controller {controller.get('controller_id', 0)}: Heating plug {action.upper()} confirmed - no state change (already {previous_state})")
                    # Record successful command for rate limiting
                    _record_kasa_command(url, action)
                    # Save temp_cfg to disk to persist state and temperature ranges
                    # This prevents loss of configuration if system crashes or restarts
                    save_json(TEMP_CFG_FILE, temp_cfg)
                else:
                    # When plug command fails, DO NOT change heater_on state
                    # The physical plug is still in its previous state since the command didn't reach it
                    # Changing the state here would create a mismatch that prevents future commands
                    # Also DO NOT record the command, allowing immediate retry
                    controller["heating_error"] = True
                    controller["heating_error_msg"] = error or ''
                    # Record when the error first started (preserve across repeated failures)
                    if controller.get("heating_kasa_error_since", 0) == 0:
                        controller["heating_kasa_error_since"] = time.time()
                    print(f"[KASA_RESULT] ✗ Controller {controller.get('controller_id', 0)}: Heating plug {action.upper()} FAILED - error: {error}")
                    # Log error to kasa_errors.log
                    log_error(f"Controller {controller.get('controller_id', 0)}: HEATING plug {action.upper()} failed at {url}: {error}")
                    # Send notification for Kasa connection failure if enabled.
                    # Passes controller for per-controller time-based deduplication and
                    # periodic re-notification while the failure persists.
                    send_kasa_error_notification('heating', url, error, controller=controller)
            elif role == 'cooling':
                controller["cooler_pending"] = False
                controller["cooler_pending_since"] = None
                controller["cooler_pending_action"] = None
                if success:
                    # Track previous state to detect actual state changes
                    previous_state = controller.get("cooler_on", False)
                    new_state = (action == 'on')
                    controller["cooler_on"] = new_state
                    controller["cooling_error"] = False
                    controller["cooling_error_msg"] = ""
                    # Reset notification tracking when plug starts working again
                    controller["cooling_error_notified"] = False
                    controller["cooling_kasa_error_since"] = 0
                    controller["cooling_kasa_error_notified_at"] = 0
                    
                    # Track baseline temperature when cooling turns ON
                    if new_state and not previous_state:
                        # Cooling just turned ON - record baseline for swapped plug detection
                        controller["cooler_baseline_temp"] = controller.get("current_temp")
                        controller["cooler_baseline_time"] = time.time()
                        print(f"[SWAPPED_PLUG] Controller {controller.get('controller_id', 0)}: Cooling activated - baseline temp: {controller.get('current_temp')}°F")
                    elif not new_state:
                        # Cooling turned OFF - clear baseline and detection
                        controller["cooler_baseline_temp"] = None
                        controller["cooler_baseline_time"] = None
                        if controller.get("swapped_plug_type") == "cooling":
                            controller["swapped_plugs_detected"] = False
                            controller["swapped_plugs_notified"] = False
                            controller["swapped_plug_type"] = ""
                    
                    # Only log and notify if state actually changed
                    if new_state != previous_state:
                        event = "cooling_on" if action == 'on' else "cooling_off"
                        print(f"[KASA_RESULT] ✓ Controller {controller.get('controller_id', 0)}: Cooling plug {action.upper()} confirmed - state changed from {previous_state} to {new_state}")
                        append_control_log(event, {
                            "controller_id": controller.get("controller_id", 0),
                            "low_limit": controller.get("low_limit"), 
                            "current_temp": controller.get("current_temp"), 
                            "high_limit": controller.get("high_limit"), 
                            "tilt_color": controller.get("tilt_color", "")
                        })
                        # Send notification if enabled (user can choose to enable/disable)
                        send_temp_control_notification(event, controller.get("current_temp", 0), controller.get("low_limit", 0), controller.get("high_limit", 0), controller.get("tilt_color", ""))
                    else:
                        print(f"[KASA_RESULT] ✓ Controller {controller.get('controller_id', 0)}: Cooling plug {action.upper()} confirmed - no state change (already {previous_state})")
                    # Record successful command for rate limiting
                    _record_kasa_command(url, action)
                    # Save temp_cfg to disk to persist state and temperature ranges
                    # This prevents loss of configuration if system crashes or restarts
                    save_json(TEMP_CFG_FILE, temp_cfg)
                else:
                    # When plug command fails, DO NOT change cooler_on state
                    # The physical plug is still in its previous state since the command didn't reach it
                    # Changing the state here would create a mismatch that prevents future commands
                    # Also DO NOT record the command, allowing immediate retry
                    controller["cooling_error"] = True
                    controller["cooling_error_msg"] = error or ''
                    # Record when the error first started (preserve across repeated failures)
                    if controller.get("cooling_kasa_error_since", 0) == 0:
                        controller["cooling_kasa_error_since"] = time.time()
                    print(f"[KASA_RESULT] ✗ Controller {controller.get('controller_id', 0)}: Cooling plug {action.upper()} FAILED - error: {error}")
                    # Log error to kasa_errors.log
                    log_error(f"Controller {controller.get('controller_id', 0)}: COOLING plug {action.upper()} failed at {url}: {error}")
                    # Send notification for Kasa connection failure if enabled.
                    # Passes controller for per-controller time-based deduplication and
                    # periodic re-notification while the failure persists.
                    send_kasa_error_notification('cooling', url, error, controller=controller)
        except queue.Empty:
            # Timeout waiting for result - this is normal, just continue
            continue
        except Exception as e:
            # Log unexpected exceptions to help with debugging
            print(f"[LOG] Exception in kasa_result_listener: {e}")
            continue

# NOTE: kasa_result_listener thread is started in if __name__ == '__main__' block

# --- Startup plug state synchronization -------------------------------------
def sync_plug_states_at_startup():
    """
    Synchronize stored plug states with actual plug states at startup.
    This prevents displaying stale state from the last shutdown.

    All controllers start with heater_on=False and cooler_on=False.  The
    function then queries each configured plug via the KasaManager worker and
    updates accordingly.  If a query fails or times out the state remains
    False (off), which is the safe default – the control loop will send the
    correct command once it evaluates the current temperature.
    """

    # Retry configuration for startup sync.  When app restarts, stop_other_app_py()
    # may close connections to kasa plugs; the plug firmware needs a few seconds
    # to fully close those connections before accepting new ones.
    _STARTUP_SYNC_MAX_RETRIES = 1
    _STARTUP_SYNC_RETRY_DELAY_S = 4
    _QUERY_TIMEOUT = 20  # seconds per query attempt

    def _query_with_retry(url, cid, mode, port=None):
        """Query a plug via kasa_manager.query_sync with retries.
        Returns (is_on, error_str, elapsed_ms) from the final attempt."""
        is_on, error, elapsed_ms = None, None, 0
        for attempt in range(_STARTUP_SYNC_MAX_RETRIES + 1):
            if attempt > 0:
                log_kasa_diag('info', f'Startup plug sync: retrying {mode} plug query',
                              controller_id=cid, url=url,
                              retry=attempt, max_retries=_STARTUP_SYNC_MAX_RETRIES)
                time.sleep(_STARTUP_SYNC_RETRY_DELAY_S)
            t0 = time.time()
            try:
                is_on, error = kasa_manager.query_sync(url, controller_id=cid,
                                                       role=mode, timeout=_QUERY_TIMEOUT,
                                                       port=port)
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                is_on = None
            elapsed_ms = round((time.time() - t0) * 1000)
            if error is None:
                break
            if attempt < _STARTUP_SYNC_MAX_RETRIES:
                log_kasa_diag('warn',
                              f'Startup plug sync: {mode} plug failed, will retry',
                              controller_id=cid, url=url, error=error, elapsed_ms=elapsed_ms,
                              attempt=attempt + 1, max_attempts=_STARTUP_SYNC_MAX_RETRIES + 1)
        return is_on, error, elapsed_ms

    controllers = temp_cfg.get('controllers', [])
    if not controllers:
        print("[LOG] sync_plug_states_at_startup: no controllers found, skipping")
        log_kasa_diag('info', 'Startup plug sync: no controllers configured, skipping')
        return

    print("[LOG] Syncing plug states at startup...")
    log_kasa_diag('info', 'Startup plug sync: begin', controller_count=len(controllers))

    for controller in controllers:
        cid = controller.get('controller_id', '?')

        # Always reset to off first – stale True values from previous sessions
        # must not be displayed or used to suppress real commands.
        controller["heater_on"] = False
        controller["cooler_on"] = False
        controller["heater_pending"] = False
        controller["cooler_pending"] = False
        controller["heater_pending_since"] = None
        controller["cooler_pending_since"] = None

        if kasa_manager is None or not kasa_manager.is_alive():
            print(f"[LOG] Controller {cid}: kasa_manager not available, plugs reset to OFF")
            log_kasa_diag('warn', 'Startup plug sync: kasa_manager unavailable',
                          controller_id=cid)
            continue

        # Query heating plug
        heating_url = controller.get("heating_plug", "")
        enable_heating = controller.get("enable_heating", False)
        if enable_heating and heating_url:
            log_kasa_diag('info', 'Startup plug sync: querying heating plug',
                          controller_id=cid, url=heating_url)
            # In --plug mode skip per-controller port; IotPlug defaults to 9999.
            heating_port = None if _FORCE_IOT_PORT else controller.get("heating_plug_port")
            is_on, error, elapsed_ms = _query_with_retry(heating_url, cid, 'heating',
                                                         port=heating_port)
            if error is None:
                controller["heater_on"] = is_on
                print(f"[LOG] Controller {cid}: Heating plug state synced: {'ON' if is_on else 'OFF'}")
                log_kasa_diag('info', 'Startup plug sync: heating plug OK',
                              controller_id=cid, url=heating_url,
                              state='ON' if is_on else 'OFF', elapsed_ms=elapsed_ms)
            else:
                print(f"[LOG] Controller {cid}: Failed to query heating plug ({error}), defaulting to OFF")
                log_kasa_diag('error', 'Startup plug sync: heating plug query failed',
                              controller_id=cid, url=heating_url,
                              error=error, elapsed_ms=elapsed_ms)

        # Query cooling plug
        cooling_url = controller.get("cooling_plug", "")
        enable_cooling = controller.get("enable_cooling", False)
        if enable_cooling and cooling_url:
            log_kasa_diag('info', 'Startup plug sync: querying cooling plug',
                          controller_id=cid, url=cooling_url)
            # In --plug mode skip per-controller port; IotPlug defaults to 9999.
            cooling_port = None if _FORCE_IOT_PORT else controller.get("cooling_plug_port")
            is_on, error, elapsed_ms = _query_with_retry(cooling_url, cid, 'cooling',
                                                         port=cooling_port)
            if error is None:
                controller["cooler_on"] = is_on
                print(f"[LOG] Controller {cid}: Cooling plug state synced: {'ON' if is_on else 'OFF'}")
                log_kasa_diag('info', 'Startup plug sync: cooling plug OK',
                              controller_id=cid, url=cooling_url,
                              state='ON' if is_on else 'OFF', elapsed_ms=elapsed_ms)
            else:
                print(f"[LOG] Controller {cid}: Failed to query cooling plug ({error}), defaulting to OFF")
                log_kasa_diag('error', 'Startup plug sync: cooling plug query failed',
                              controller_id=cid, url=cooling_url,
                              error=error, elapsed_ms=elapsed_ms)

    print("[LOG] Plug state synchronization complete")
    log_kasa_diag('info', 'Startup plug sync: complete')

    # Log the startup sync to the control log
    try:
        append_control_log("startup_plug_sync", {
            "controllers": [
                {
                    "controller_id": c.get("controller_id"),
                    "heater_on": c.get("heater_on"),
                    "cooler_on": c.get("cooler_on"),
                }
                for c in controllers
            ]
        })
    except Exception as e:
        print(f"[LOG] Failed to log startup sync: {e}")

# Start sync in background thread to prevent blocking Flask startup
# This allows the web server to start immediately even if plug queries are slow
def _background_startup_sync():
    """Run startup sync in background to avoid blocking Flask.

    Waits for the kasa_manager worker subprocess to be alive before issuing
    queries, so there is no direct asyncio loop in this thread.
    """
    try:
        # Wait for the worker subprocess to be fully ready.
        _WORKER_WAIT_S = 5          # maximum seconds to wait for worker to start
        _POLL_INTERVAL_S = 0.1      # polling interval in seconds
        _POLL_STEPS = int(_WORKER_WAIT_S / _POLL_INTERVAL_S)
        for _ in range(_POLL_STEPS):
            if kasa_manager and kasa_manager.is_alive():
                break
            time.sleep(_POLL_INTERVAL_S)
        sync_plug_states_at_startup()
    except Exception as e:
        print(f"[LOG] Exception in background startup sync: {e}")
    finally:
        # Always signal completion so periodic_temp_control is not blocked
        # indefinitely regardless of whether the sync succeeded or failed.
        _startup_sync_complete.set()
        print("[LOG] Startup sync event set — temperature control may now proceed")

# NOTE: _background_startup_sync thread is started in if __name__ == '__main__' block
# after kasa components are initialized

# --- Offsite push helpers (kept, forwarding enabled) -----------------------
def get_predefined_field_maps():
    """
    Returns a dictionary of predefined field map templates that users can select from.
    These are common field mappings for popular services.
    """
    return {
        "default": {
            "name": "Default",
            "description": "Standard field names",
            "map": {
                "timestamp": "timestamp",
                "tilt_color": "tilt_color",
                "gravity": "gravity",
                "temp_f": "temp",
                "brew_id": "brewid",
                "device": "device"
            }
        },
        "brewersfriend": {
            "name": "Brewers Friend",
            "description": "Optimized for Brewers Friend API",
            "map": {
                "timestamp": "timestamp",
                "tilt_color": "name",
                "gravity": "gravity",
                "temp_f": "temp",
                "brew_id": "beer",
                "device": "device"
            }
        },
        "custom": {
            "name": "Custom",
            "description": "User-defined field mapping",
            "map": {
                "timestamp": "timestamp",
                "tilt_color": "tilt_color",
                "gravity": "gravity",
                "temp_f": "temp",
                "brew_id": "brewid",
                "device": "device"
            }
        }
    }

def build_offsite_payload(field_map=None):
    default_map = {
        'timestamp': 'timestamp',
        'tilt_color': 'tilt_color',
        'gravity': 'gravity',
        'temp_f': 'temp',
        'brew_id': 'brewid',
        'device': 'device'
    }
    if not field_map:
        field_map = default_map
    # Build temp_control summary from all active controllers (multi-controller support).
    # For backward compatibility with single-value consumers, also expose the first
    # active controller's data at the top level.
    controllers_snapshot = []
    first_active = None
    for ctrl in temp_cfg.get('controllers', []):
        if ctrl.get('temp_control_active'):
            entry = {
                'controller_id': ctrl.get('controller_id', 0),
                'current_temp': ctrl.get('current_temp'),
                'low_limit': ctrl.get('low_limit'),
                'high_limit': ctrl.get('high_limit'),
                'status': ctrl.get('status'),
                'tilt_color': ctrl.get('tilt_color', ''),
            }
            controllers_snapshot.append(entry)
            if first_active is None:
                first_active = entry
    controllers_list = temp_cfg.get('controllers') or []
    if first_active is None and controllers_list:
        # No active controller – fall back to first configured controller
        ctrl = controllers_list[0]
        first_active = {
            'controller_id': ctrl.get('controller_id', 0),
            'current_temp': ctrl.get('current_temp'),
            'low_limit': ctrl.get('low_limit'),
            'high_limit': ctrl.get('high_limit'),
            'status': ctrl.get('status'),
            'tilt_color': ctrl.get('tilt_color', ''),
        }
    # first_active is None only when no controllers are configured at all.
    payload = {
        'timestamp': datetime.utcnow().isoformat(),
        'temp_control': first_active or {},
        'temp_controllers': controllers_snapshot,
        'tilts': []
    }
    for color, info in live_tilts.items():
        entry = {
            field_map.get('tilt_color', 'tilt_color'): color,
            field_map.get('gravity', 'gravity'): info.get('gravity'),
            field_map.get('temp_f', 'temp'): info.get('temp_f'),
            field_map.get('brew_id', 'brewid'): info.get('brewid'),
            field_map.get('device', 'device'): color
        }
        payload['tilts'].append(entry)
    return payload

def push_offsite_snapshot():
    return

# --- Periodic temp control thread -----------------------------------------
def periodic_temp_control():
    # Wait for the startup plug-state sync to finish before the first control
    # cycle.  This prevents sending kasa commands (which set heater_pending /
    # cooler_pending) before we know the actual physical plug states, which
    # would cause spurious "pending → failure" sequences visible on the UI.
    # Timeout of 120 s prevents a permanent block if startup sync never fires.
    if not _startup_sync_complete.wait(timeout=120):
        print("[LOG] WARNING: Startup sync did not complete within 120 s — proceeding with temperature control; plug states may be unknown and initial kasa commands could fail")
    while True:
        try:
            # Check whether the kasa_manager worker subprocess has died; restart if so.
            _check_and_restart_kasa_proc()

            file_cfg = load_json(TEMP_CFG_FILE, {})
            
            # Handle both old and new format
            if 'controllers' in file_cfg:
                # New format with controller array
                # Exclude runtime state variables from file reload to prevent state reset
                # These variables track the current operational state and should not be
                # overwritten by potentially stale values from the config file
                runtime_state_vars = [
                    'heater_on', 'cooler_on',           # Current plug states
                    'heater_pending', 'cooler_pending',  # Pending command flags
                    'heater_pending_since', 'cooler_pending_since',  # Pending timestamps
                    'heater_pending_action', 'cooler_pending_action',  # Pending actions
                    'heating_error', 'cooling_error',    # Error states
                    'heating_error_msg', 'cooling_error_msg',  # Error messages
                    'heating_error_notified', 'cooling_error_notified',  # Notification flags
                    'heating_kasa_error_since', 'cooling_kasa_error_since',  # Error start times
                    'heating_kasa_error_notified_at', 'cooling_kasa_error_notified_at',  # Last notification times
                    # Swapped plug detection runtime state
                    'heater_baseline_temp', 'heater_baseline_time',
                    'cooler_baseline_temp', 'cooler_baseline_time',
                    'swapped_plugs_detected', 'swapped_plugs_notified', 'swapped_plug_type',
                    # ALL 7 notification triggers (temperature + safety)
                    'heating_blocked_trigger', 'cooling_blocked_trigger',  # Safety triggers - heating/cooling blocked
                    'heating_safety_off_trigger', 'cooling_safety_off_trigger',  # Safety triggers - turned off for safety
                    'below_limit_logged', 'above_limit_logged',  # Limit trigger flags
                    'below_limit_trigger_armed', 'above_limit_trigger_armed',  # Temperature limit triggers
                    'in_range_trigger_armed',  # Range trigger
                    'safety_shutdown_logged',  # Safety shutdown flag
                    'status',  # Current status message
                    # Temperature reading state - updated live from BLE scanner
                    # Exclude to prevent overwriting live readings with stale file values
                    'current_temp', 'last_reading_time',
                    # Temperature limits should only change via web UI /update_temp_config
                    # Exclude from periodic reload to prevent corruption from stale/invalid file values
                    'low_limit', 'high_limit'
                ]
                
                # Update each controller, preserving runtime state
                for i, file_controller in enumerate(file_cfg.get('controllers', [])):
                    if i < len(temp_cfg.get('controllers', [])):
                        # Remove runtime state from file controller before updating
                        for var in runtime_state_vars:
                            file_controller.pop(var, None)
                        
                        # Remove None current_temp if memory has a value
                        if 'current_temp' in file_controller and file_controller['current_temp'] is None:
                            if temp_cfg['controllers'][i].get('current_temp') is not None:
                                file_controller.pop('current_temp')
                        
                        # Update the controller
                        temp_cfg['controllers'][i].update(file_controller)
                        
                        # Validate limits for this controller
                        controller = temp_cfg['controllers'][i]
                        low_val = controller.get("low_limit")
                        if low_val is None:
                            print(f"[LOG] WARNING: Controller {i} low_limit is None after config reload, resetting to 0.0")
                            controller["low_limit"] = 0.0
                        elif isinstance(low_val, (int, float)):
                            controller["low_limit"] = float(low_val)  # Ensure float type
                        else:
                            try:
                                controller["low_limit"] = float(low_val)
                            except (ValueError, TypeError):
                                print(f"[LOG] WARNING: Controller {i} low_limit cannot be converted to float after reload, resetting to 0.0")
                                controller["low_limit"] = 0.0
                        
                        high_val = controller.get("high_limit")
                        if high_val is None:
                            print(f"[LOG] WARNING: Controller {i} high_limit is None after config reload, resetting to 0.0")
                            controller["high_limit"] = 0.0
                        elif isinstance(high_val, (int, float)):
                            controller["high_limit"] = float(high_val)  # Ensure float type
                        else:
                            try:
                                controller["high_limit"] = float(high_val)
                            except (ValueError, TypeError):
                                print(f"[LOG] WARNING: Controller {i} high_limit cannot be converted to float after reload, resetting to 0.0")
                                controller["high_limit"] = 0.0
            
            temperature_control_logic()
            
            # Check for swapped plugs after temperature control logic runs
            check_for_swapped_plugs()
            
            # Log periodic temperature reading at update_interval frequency
            # This is separate from Tilt readings (logged at tilt_logging_interval_minutes)
            log_periodic_temp_reading()
        except Exception as e:
            # Log exception in periodic control loop
            print(f"[LOG] Exception in periodic_temp_control: {e}")
            import traceback
            traceback.print_exc()
            # Log error event (not controller-specific since it's a loop-level error)
            append_control_log("periodic_control_exception", {
                "error": str(e),
                "error_type": type(e).__name__
            })

        try:
            # Use system_cfg update_interval for temperature control loop frequency
            # This is separate from tilt_logging_interval_minutes which controls fermentation logging
            interval_minutes = int(system_cfg.get("update_interval", 2))
        except Exception:
            interval_minutes = 2  # Default to 2 minutes for responsive temperature control
        interval_seconds = max(1, interval_minutes * 60)
        time.sleep(interval_seconds)

# NOTE: periodic_temp_control thread is started in if __name__ == '__main__' block
# after kasa_manager is initialized

# --- Periodic batch monitoring thread -------------------------------------
def periodic_batch_monitoring():
    """Monitor for signal loss, schedule daily reports, and process notification retries."""
    last_daily_check = None
    
    while True:
        try:
            # Check for signal loss every 5 minutes
            check_signal_loss()
            
            # Process pending notifications (10-second delay for deduplication)
            process_pending_notifications()
            
            # Process notification retry queue with exponential backoff
            process_notification_retries()
            
            # Check if it's time for daily reports
            notif_cfg = system_cfg.get('batch_notifications', {})
            daily_report_time = notif_cfg.get('daily_report_time', '09:00')  # Default 9 AM
            
            now = datetime.now()  # Use local time to match user's configured time
            current_time_str = now.strftime('%H:%M')
            
            # Check if we should send daily report (within 5 minute window)
            if daily_report_time:
                try:
                    report_hour, report_min = map(int, daily_report_time.split(':'))
                    current_hour = now.hour
                    current_min = now.minute
                    
                    # Check if current time is within DAILY_REPORT_WINDOW_MINUTES of report time
                    # Convert both times to minutes since midnight for accurate comparison
                    current_minutes = current_hour * 60 + current_min
                    report_minutes = report_hour * 60 + report_min
                    
                    # Handle midnight boundary (e.g., report at 23:58, current 00:01)
                    minute_diff = abs(current_minutes - report_minutes)
                    # If difference is greater than 12 hours, we crossed midnight
                    if minute_diff > 720:  # 720 = 12 hours in minutes
                        minute_diff = 1440 - minute_diff  # 1440 = 24 hours in minutes
                    
                    time_match = minute_diff < DAILY_REPORT_WINDOW_MINUTES
                    
                    # Only send once per day
                    if time_match:
                        if not last_daily_check or (now - last_daily_check).total_seconds() > 3600:
                            send_daily_report()
                            last_daily_check = now
                except Exception as e:
                    print(f"[LOG] Error checking daily report time: {e}")
        
        except Exception as e:
            print(f"[LOG] Exception in periodic_batch_monitoring: {e}")
        
        # Sleep for BATCH_MONITORING_INTERVAL_SECONDS
        time.sleep(BATCH_MONITORING_INTERVAL_SECONDS)

# NOTE: periodic_batch_monitoring thread is started in if __name__ == '__main__' block

# --- BLE scanner thread ---------------------------------------------------
def ble_loop():
    # Restart the scanner periodically so that BlueZ's duplicate-device filter is
    # cleared and every Tilt (including those broadcasting stable values, such as the
    # Orange mini-pro) is re-discovered at regular intervals.
    _SCAN_RESTART_INTERVAL = 60  # seconds between scanner restarts

    async def run_scanner():
        if BleakScanner is None:
            print("[LOG] BleakScanner not available; BLE scanning disabled")
            return
        while True:
            try:
                scanner = BleakScanner(detection_callback)
                await scanner.start()
                await asyncio.sleep(_SCAN_RESTART_INTERVAL)
                await scanner.stop()
            except Exception as e:
                print(f"[LOG] BLE scanner cycle error: {e}")
                await asyncio.sleep(5)
    try:
        asyncio.run(run_scanner())
    except Exception as e:
        print(f"[LOG] BLE loop failed to start: {e}")

# NOTE: ble_loop thread is started in if __name__ == '__main__' block

# --- Flask routes ---------------------------------------------------------

@app.route('/')
def dashboard():
    # Only show active tilts on the main display
    active_tilts = get_active_tilts()

    # Pass controllers array to template
    controllers = temp_cfg.get('controllers', [])

    # now_ts is used in the template to append a cache-busting query string
    # to the stylesheet URL so the browser always fetches the latest CSS.
    return render_template('maindisplay.html',
        system_settings=system_cfg,
        tilt_cfg=tilt_cfg,
        COLOR_MAP=COLOR_MAP,
        tilts=active_tilts,
        tilt_status=tilt_status,
        controllers=controllers,
        live_tilts=active_tilts,
        now_ts=int(time.time())
    )

@app.route('/startup')
def startup():
    """Display startup splash screen"""
    return render_template('startup.html')

@app.route('/system_config')
def system_config():
    # Get the tab parameter from query string and validate it
    active_tab = request.args.get('tab', 'main-settings')
    if active_tab not in VALID_SYSTEM_CONFIG_TABS:
        active_tab = 'main-settings'
    
    # Migrate old format to new format if needed
    external_urls = system_cfg.get("external_urls", [])
    
    # If no external_urls but old format exists, migrate
    if not external_urls:
        for i in range(3):
            name = system_cfg.get(f"external_name_{i}", "").strip()
            url = system_cfg.get(f"external_url_{i}", "").strip()
            if url:
                url_config = {
                    "name": name or f"Service {i + 1}",
                    "url": url,
                    "method": system_cfg.get("external_method", "POST"),
                    "content_type": system_cfg.get("external_content_type", "form"),
                    "timeout_seconds": int(system_cfg.get("external_timeout_seconds", 8)),
                    "field_map_id": "default"
                }
                # If there's a custom field map, use it
                if system_cfg.get("external_field_map"):
                    url_config["field_map_id"] = "custom"
                    url_config["custom_field_map"] = system_cfg.get("external_field_map")
                external_urls.append(url_config)
    
    # Ensure we have exactly 3 slots (fill with empty ones if needed)
    while len(external_urls) < 3:
        external_urls.append({
            "name": "",
            "url": "",
            "method": "POST",
            "content_type": "form",
            "timeout_seconds": 8,
            "field_map_id": "default"
        })
    
    return render_template('system_config.html',
                         system_settings=system_cfg,
                         external_urls=external_urls,
                         predefined_field_maps=get_predefined_field_maps(),
                         active_tab=active_tab,
                         error_msg=request.args.get('error', ''),
                         force_iot_port=_FORCE_IOT_PORT)

@app.route('/update_system_config', methods=['POST'])
def update_system_config():
    data = request.form
    old_warn = system_cfg.get('warning_mode', 'NONE')
    
    # Capture the active tab to return to it after saving (validate against whitelist)
    active_tab = data.get('active_tab', 'main-settings')
    if active_tab not in VALID_SYSTEM_CONFIG_TABS:
        active_tab = 'main-settings'

    # Handle password field - only update if provided
    sending_email_password = data.get("sending_email_password", "")
    if sending_email_password:
        # Strip all whitespace (including non-breaking spaces that Google's App Password
        # page sometimes inserts) so copied passwords work regardless of formatting.
        # Use split/join rather than replace(" ", "") to catch all Unicode whitespace.
        # Store as smtp_password for SMTP authentication
        system_cfg["smtp_password"] = "".join(sending_email_password.split())
    
    # Handle Pushover API Token - only update if provided
    pushover_api_token = data.get("pushover_api_token", "")
    if pushover_api_token:
        system_cfg["pushover_api_token"] = pushover_api_token
    
    # Handle ntfy Auth Token - only update if provided
    ntfy_auth_token = data.get("ntfy_auth_token", "")
    if ntfy_auth_token:
        system_cfg["ntfy_auth_token"] = ntfy_auth_token

    # Handle Kasa credentials — always store username; only overwrite password
    # if a non-empty value is submitted (so the stored password is preserved
    # when the user saves other settings without re-entering it).
    old_kasa_username = system_cfg.get('kasa_username', '')
    old_kasa_password = system_cfg.get('kasa_password', '')
    new_kasa_username = data.get("kasa_username", "").strip()
    new_kasa_password = data.get("kasa_password", "").strip()
    system_cfg["kasa_username"] = new_kasa_username
    if new_kasa_password:
        system_cfg["kasa_password"] = new_kasa_password
    # Restart the kasa worker if credentials changed so it picks up new values.
    _kasa_creds_changed = (
        new_kasa_username != old_kasa_username or
        (new_kasa_password and new_kasa_password != old_kasa_password)
    )
    
    # Handle external URLs - support new per-URL configuration format
    external_urls = []
    for i in range(3):
        name = data.get(f"external_name_{i}", "").strip()
        url = data.get(f"external_url_{i}", "").strip()
        
        # Always create entry (even if URL is empty) to preserve settings
        url_config = {
            "name": name or f"Service {i + 1}",
            "url": url,
            "method": data.get(f"external_method_{i}", "POST"),
            "content_type": data.get(f"external_content_type_{i}", "form"),
            "timeout_seconds": int(data.get(f"external_timeout_seconds_{i}", 8)),
            "field_map_id": data.get(f"external_field_map_id_{i}", "default")
        }
        
        # If custom field map is selected, store the custom JSON
        if url_config["field_map_id"] == "custom":
            custom_map = data.get(f"external_custom_field_map_{i}", "").strip()
            if custom_map:
                url_config["custom_field_map"] = custom_map
        
        external_urls.append(url_config)
    
    system_cfg.update({
        "brewery_name": data.get("brewery_name", ""),
        "brewer_name": data.get("brewer_name", ""),
        "street": data.get("street", ""),
        "city": data.get("city", ""),
        "state": data.get("state", ""),
        "email": data.get("email", ""),
        "mobile": data.get("mobile", ""),
        "timezone": data.get("timezone", ""),
        "timestamp_format": data.get("timestamp_format", ""),
        "display_mode": data.get("display_mode", "4"),
        "update_interval": data.get("update_interval", "2"),
        "external_refresh_rate": data.get("external_refresh_rate", system_cfg.get("external_refresh_rate", "15")),
        "external_urls": external_urls,  # New format
        "warning_mode": data.get("warning_mode", "NONE"),
        "sending_email": data.get("sending_email", system_cfg.get('sending_email','')),
        "smtp_host": data.get("smtp_host", system_cfg.get('smtp_host', 'smtp.gmail.com')),
        "smtp_port": int(data.get("smtp_port", system_cfg.get('smtp_port', 587))),
        "smtp_starttls": 'smtp_starttls' in data,
        "kasa_rate_limit_seconds": data.get("kasa_rate_limit_seconds", system_cfg.get('kasa_rate_limit_seconds', 10)),
        "tilt_logging_interval_minutes": int(data.get("tilt_logging_interval_minutes", system_cfg.get("tilt_logging_interval_minutes", 15))),
        # Push notification provider settings
        "push_provider": data.get("push_provider", system_cfg.get("push_provider", "pushover")),
        "pushover_user_key": data.get("pushover_user_key", system_cfg.get("pushover_user_key", "")),
        "pushover_device": data.get("pushover_device", system_cfg.get("pushover_device", "")),
        "ntfy_server": data.get("ntfy_server", system_cfg.get("ntfy_server", "https://ntfy.sh")),
        "ntfy_topic": data.get("ntfy_topic", system_cfg.get("ntfy_topic", "")),
        "enable_kasa_activity_log": 'enable_kasa_activity_log' in data,
    })
    
    # Preserve old format fields for backward compatibility (only if external_urls is empty)
    if not external_urls:
        system_cfg.update({
            "external_name_0": data.get("external_name_0", system_cfg.get('external_name_0','')),
            "external_url_0": data.get("external_url_0", system_cfg.get('external_url_0','')),
            "external_name_1": data.get("external_name_1", system_cfg.get('external_name_1','')),
            "external_url_1": data.get("external_url_1", system_cfg.get('external_url_1','')),
            "external_name_2": data.get("external_name_2", system_cfg.get('external_name_2','')),
            "external_url_2": data.get("external_url_2", system_cfg.get('external_url_2','')),
            "external_method": data.get("external_method", system_cfg.get('external_method','POST')),
            "external_content_type": data.get("external_content_type", system_cfg.get('external_content_type','form')),
            "external_timeout_seconds": data.get("external_timeout_seconds", system_cfg.get('external_timeout_seconds',8)),
            "external_field_map": data.get("external_field_map", system_cfg.get('external_field_map','')),
        })
    
    # Update temperature control notifications settings
    temp_control_notif = {
        'enable_temp_below_low_limit': 'enable_temp_below_low_limit' in data,
        'enable_temp_above_high_limit': 'enable_temp_above_high_limit' in data,
        'enable_heating_on': 'enable_heating_on' in data,
        'enable_heating_off': 'enable_heating_off' in data,
        'enable_cooling_on': 'enable_cooling_on' in data,
        'enable_cooling_off': 'enable_cooling_off' in data,
        'enable_kasa_error': 'enable_kasa_error' in data,
    }
    system_cfg['temp_control_notifications'] = temp_control_notif
    
    # Update batch notifications settings
    batch_notif = {
        'enable_loss_of_signal': 'enable_loss_of_signal' in data,
        'loss_of_signal_timeout_minutes': int(data.get('loss_of_signal_timeout_minutes', 30)),
        'enable_fermentation_starting': 'enable_fermentation_starting' in data,
        'enable_fermentation_completion': 'enable_fermentation_completion' in data,
        'enable_daily_report': 'enable_daily_report' in data,
        'daily_report_time': data.get('daily_report_time', '09:00'),
    }
    system_cfg['batch_notifications'] = batch_notif
    
    save_json(SYSTEM_CFG_FILE, system_cfg)

    # Restart the kasa worker if credentials changed so it picks up new values.
    # In --plug mode credentials are never used, so restart is not needed.
    if _kasa_creds_changed and kasa_manager is not None and kasa_manager.is_alive() and not _FORCE_IOT_PORT:
        try:
            kasa_manager.restart(
                kasa_username=system_cfg.get('kasa_username', ''),
                kasa_password=system_cfg.get('kasa_password', '')
            )
            log_kasa_diag('info', 'KasaManager restarted after credential change')
        except Exception as _km_restart_exc:
            log_kasa_diag('error', f'KasaManager restart after credential change failed: {_km_restart_exc}')

    new_warn = system_cfg.get('warning_mode','NONE')
    # Reset notification state when warning mode changes
    if old_warn.upper() == 'NONE' and new_warn.upper() in ('EMAIL','PUSH','BOTH'):
        temp_cfg['notifications_trigger'] = False
        temp_cfg['notification_comm_failure'] = False
        for ctrl in temp_cfg.get('controllers', []):
            ctrl['notifications_trigger'] = False
            ctrl['notification_comm_failure'] = False
    elif new_warn.upper() == 'NONE':
        temp_cfg['notifications_trigger'] = False
        temp_cfg['notification_comm_failure'] = False
        for ctrl in temp_cfg.get('controllers', []):
            ctrl['notifications_trigger'] = False
            ctrl['notification_comm_failure'] = False

    return redirect(f'/system_config?tab={active_tab}')

@app.route('/test_email', methods=['POST'])
def test_email():
    """Test email notification with current settings"""
    brewery_name = system_cfg.get('brewery_name', 'The Tilt Fermentatorium')
    subject = f"TEST - {brewery_name}"
    body = f"*** TEST MESSAGE ***\n\nThis is a TEST email from {brewery_name}.\n\nIf you received this, your email settings are configured correctly!\n\n*** TEST MESSAGE ***"
    
    success = False
    error_msg = None
    
    try:
        success, error_msg = send_email(subject, body)
    except Exception as e:
        error_msg = str(e)
    
    # Log the test notification attempt (success or failure)
    log_notification(
        notification_type='email',
        subject=subject,
        body=body,
        success=success,
        error=error_msg if not success else None
    )
    
    if success:
        return jsonify({
            'success': True,
            'message': 'Test email sent successfully! Check your inbox.'
        })
    else:
        return jsonify({
            'success': False,
            'message': f'Failed to send test email: {error_msg}'
        })

@app.route('/test_push', methods=['POST'])
def test_push():
    """Test push notification with current settings"""
    # Determine which provider is configured
    push_provider = system_cfg.get("push_provider", "pushover").lower()
    provider_name = "Pushover" if push_provider == "pushover" else "ntfy"
    
    brewery_name = system_cfg.get('brewery_name', 'The Tilt Fermentatorium')
    subject = f"TEST - {brewery_name}"
    body = f"*** TEST MESSAGE *** This is a TEST push notification from {brewery_name}. If you received this, your {provider_name} settings are configured correctly! *** TEST MESSAGE ***"
    
    success = False
    error_msg = None
    
    try:
        success, error_msg = send_push(body, subject=subject)
    except Exception as e:
        error_msg = str(e)
    
    # Log the test notification attempt (success or failure)
    log_notification(
        notification_type='push',
        subject=subject,
        body=body,
        success=success,
        error=error_msg if not success else None
    )
    
    if success:
        return jsonify({
            'success': True,
            'message': f'Test push notification sent successfully via {provider_name}! Check your device.'
        })
    else:
        return jsonify({
            'success': False,
            'message': f'Failed to send test push notification: {error_msg}'
        })

@app.route('/test_external_logging', methods=['POST'])
def test_external_logging():
    """
    Test external logging connection with a test payload.
    
    Security Note: This endpoint intentionally makes requests to user-provided URLs
    to test external logging integrations. This is expected behavior for an admin
    configuration feature. Risk is mitigated by:
    - Admin-only access (system config page)
    - Timeout limits
    - No sensitive data in test payload
    - Controlled environment (Raspberry Pi)
    """
    try:
        data = request.get_json()
        index = data.get('index', 0)
        url = data.get('url', '').strip()
        
        if not url:
            return jsonify({
                'success': False,
                'message': 'No URL provided'
            })
        
        # Basic URL validation
        if not (url.startswith('http://') or url.startswith('https://')):
            return jsonify({
                'success': False,
                'message': 'URL must start with http:// or https://'
            })
        
        # Create a test payload
        test_payload = {
            "tilt_color": "TEST",
            "temp_f": 68.5,
            "gravity": 1.050,
            "brewid": "test_batch",
            "batch_name": "Test Connection",
            "beer_name": "Test Beer",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "test": True
        }
        
        # Get configuration settings from request (per-URL settings) or fall back to system config
        method = data.get('method', system_cfg.get("external_method", "POST")).upper()
        content_type = data.get('content_type', system_cfg.get("external_content_type", "form"))
        send_json = (content_type == "json")
        timeout = int(data.get('timeout_seconds', system_cfg.get("external_timeout_seconds", 8)) or 8)
        
        # Get field map settings
        field_map_id = data.get('field_map_id', 'default')
        custom_field_map = data.get('custom_field_map', '')
        
        # Helper function to apply field mapping
        def apply_field_mapping(payload, field_map):
            """Apply field mapping transformation to payload."""
            transformed = {}
            for logical_field, remote_field in field_map.items():
                if logical_field in payload:
                    transformed[remote_field] = payload[logical_field]
            return transformed
        
        # Transform for Brewers Friend if needed
        # Check if the URL contains brewersfriend.com as the domain (not in query params)
        # Note: Brewers Friend transformation takes precedence over custom field maps
        # to ensure compatibility with their API requirements
        try:
            parsed = urlparse(url)
            is_brewersfriend = 'brewersfriend.com' in parsed.netloc.lower()
        except Exception:
            # Fallback to simple string check if urlparse fails
            url_lower = url.lower()
            is_brewersfriend = url_lower.startswith('https://brewersfriend.com') or url_lower.startswith('http://brewersfriend.com')
        
        if is_brewersfriend:
            test_payload = {
                "name": "TEST",
                "temp": 68.5,
                "temp_unit": "F",
                "gravity": 1.050,
                "gravity_unit": "G",
                "beer": "Test Connection",
                "comment": "Test from Fermenter Controller"
            }
            send_json = True
        elif field_map_id and field_map_id != 'default':
            # Apply field map transformation if specified
            if field_map_id == 'custom' and custom_field_map:
                try:
                    field_map = json.loads(custom_field_map)
                    test_payload = apply_field_mapping(test_payload, field_map)
                except (json.JSONDecodeError, ValueError, TypeError) as e:
                    # If custom field map is invalid, use original payload
                    print(f"[WARNING] Invalid custom field map JSON for test connection: {e}")
                    # Continue with original payload instead of failing the test
            else:
                # Use predefined field map
                predefined_maps = get_predefined_field_maps()
                field_map = predefined_maps.get(field_map_id, {}).get("map")
                if field_map:
                    test_payload = apply_field_mapping(test_payload, field_map)
        
        # Attempt to send test data
        if requests is None:
            return jsonify({
                'success': False,
                'message': 'Requests library not available'
            })
        
        headers = {}
        
        try:
            if send_json:
                headers["Content-Type"] = "application/json"
                resp = requests.request(method, url, json=test_payload, headers=headers, timeout=timeout)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                # Build form data, converting None to empty string and filtering to simple types
                formdata = {}
                for k, v in test_payload.items():
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        formdata[k] = "" if v is None else v
                resp = requests.request(method, url, data=formdata, headers=headers, timeout=timeout)
            
            # Check response
            if resp.status_code >= 200 and resp.status_code < 300:
                return jsonify({
                    'success': True,
                    'message': f'Connection successful! Status: {resp.status_code}'
                })
            else:
                # Don't expose response content in error messages for security
                return jsonify({
                    'success': False,
                    'message': f'Connection failed with HTTP status {resp.status_code}'
                })
        except requests.exceptions.Timeout:
            return jsonify({
                'success': False,
                'message': f'Connection timeout after {timeout} seconds'
            })
        except requests.exceptions.ConnectionError:
            return jsonify({
                'success': False,
                'message': 'Unable to connect to the specified URL. Please check the URL and network connection.'
            })
        except Exception:
            return jsonify({
                'success': False,
                'message': 'Request failed. Please check the URL and try again.'
            })
            
    except Exception:
        return jsonify({
            'success': False,
            'message': 'An error occurred while testing the connection. Please verify your settings and try again.'
        })

@app.route('/tilt_config', methods=['GET', 'POST'])
def tilt_config():
    selected = request.args.get('tilt_color') or request.form.get('tilt_color')
    batch_history = []
    if selected:
        try:
            with open(f'batches/batch_history_{selected}.json', 'r') as f:
                batch_history = json.load(f)
        except Exception:
            batch_history = []
    if request.method == 'POST':
        color = request.form.get('tilt_color')
        action = request.form.get('action')
        # --- PATCH: Capture quick OG/recipe/metadata changes as batch_metadata ----
        actual_og = request.form.get("actual_og")
        recipe_og = request.form.get("recipe_og")
        # update tilt_cfg fields from the form (for quick-edit path)
        changed = False
        if color in tilt_cfg:
            batch_entry = tilt_cfg[color].copy()
            if actual_og is not None:
                batch_entry['actual_og'] = actual_og
                tilt_cfg[color]['actual_og'] = actual_og
                changed = True
            if recipe_og is not None:
                batch_entry['recipe_og'] = recipe_og
                tilt_cfg[color]['recipe_og'] = recipe_og
                changed = True
            # Keep og_confirmed in data structure for backward compatibility (always False)
            batch_entry['og_confirmed'] = False
            tilt_cfg[color]['og_confirmed'] = False
            batch_entry['brewid'] = tilt_cfg[color].get("brewid")
            if changed:
                try:
                    save_json(TILT_CONFIG_FILE, tilt_cfg)
                except Exception:
                    pass
                # Append batch_metadata to batch file
                append_batch_metadata_to_batch_jsonl(color, batch_entry)
        if color and action:
            if action == "cancel":
                return redirect("/")
            return redirect(f"/batch_settings?tilt_color={color}&action={action}")
    config = tilt_cfg.get(selected, {}) if selected else {}
    return render_template('tilt_config.html',
        tilt_cfg=tilt_cfg,
        tilt_colors=list(TILT_UUIDS.values()),
        selected_tilt=selected,
        selected_config=config,
        system_settings=system_cfg,
        batch_history=batch_history
    )

@app.route('/batch_settings', methods=['GET', 'POST'])
def batch_settings():
    if request.method == 'POST':
        data = request.form
        color = data.get('tilt_color')
        if not color:
            return "No Tilt color selected", 400
        beer_name = data.get('beer_name', '').strip()
        batch_name = data.get('batch_name', '').strip()
        start_date = data.get('ferm_start_date', '').strip()
        existing = tilt_cfg.get(color, {})
        old_brew_id = existing.get('brewid')
        brew_id = existing.get('brewid')
        if not brew_id:
            brew_id = generate_brewid(beer_name, batch_name, start_date)
        if old_brew_id and old_brew_id != brew_id:
            rotate_and_archive_old_history(color, old_brew_id, existing)
            tilt_cfg[color] = {
                "beer_name": "",
                "batch_name": "",
                "ferm_start_date": "",
                "recipe_og": "",
                "recipe_fg": "",
                "recipe_abv": "",
                "actual_og": None,
                "brewid": "",
                "og_confirmed": False,
                "notification_state": {
                    "fermentation_start_datetime": None,
                    "fermentation_completion_datetime": None,
                    "last_daily_report": None
                }
            }
            save_json(TILT_CONFIG_FILE, tilt_cfg)

        og_confirmed = False  # No longer using og_confirmed checkbox

        # Calibration variances — default to existing values if not submitted.
        # Prefer the MAC-keyed tilt_table record (follows the physical device),
        # fall back to the color-keyed tilt_cfg entry.
        def _parse_variance(raw, existing_key, cfg_dict):
            """Return float variance or preserve existing value if raw is empty."""
            s = (raw or '').strip()
            if s == '':
                return float(cfg_dict.get(existing_key, 0) or 0)
            try:
                return float(s)
            except ValueError:
                return 0.0

        tilt_mac = live_tilts.get(color, {}).get("mac_address", "")
        normalized_tilt_mac = normalize_mac(tilt_mac)
        if normalized_tilt_mac and normalized_tilt_mac in tilt_table:
            mac_tv, mac_gv = get_device_variances(tilt_table, mac=tilt_mac)
            existing_tv = {"temp_variance": mac_tv}
            existing_gv = {"gravity_variance": mac_gv}
        else:
            existing_tv = existing_gv = tilt_cfg.get(color, {})
        temp_variance    = _parse_variance(data.get('temp_variance'),    'temp_variance',    existing_tv)
        gravity_variance = _parse_variance(data.get('gravity_variance'), 'gravity_variance', existing_gv)

        batch_entry = {
            "beer_name": beer_name,
            "batch_name": batch_name,
            "ferm_start_date": start_date,
            "recipe_og": data.get('recipe_og', '') or '',
            "recipe_fg": data.get('recipe_fg', '') or '',
            "recipe_abv": data.get('recipe_abv', '') or '',
            "actual_og": (data.get('actual_og', '') or None),
            "og_confirmed": False,  # Keep field for backward compatibility
            "brewid": brew_id,
            "is_active": True,  # New field to track active vs closed batches
            "closed_date": None,  # Track when batch was closed
            "temp_variance": temp_variance,
            "gravity_variance": gravity_variance,
        }
        
        # Preserve existing notification_state when editing a batch
        if color in tilt_cfg and 'notification_state' in tilt_cfg[color]:
            # Create a copy to avoid modifying the original
            batch_entry['notification_state'] = dict(tilt_cfg[color]['notification_state'])
        else:
            # Initialize notification_state for new batches
            batch_entry['notification_state'] = {
                "fermentation_start_datetime": None,
                "fermentation_completion_datetime": None,
                "last_daily_report": None
            }

        # Load existing batches
        try:
            with open(f'batches/batch_history_{color}.json', 'r') as f:
                batches = json.load(f)
        except Exception:
            batches = []
        
        # UPSERT: Update existing batch entry or append new one
        # This prevents duplicate entries when editing the same batch
        batch_found = False
        for i, batch in enumerate(batches):
            if batch.get('brewid') == brew_id:
                # Update existing batch entry instead of creating duplicate
                batches[i] = batch_entry
                batch_found = True
                break
        
        if not batch_found:
            # New batch - append it
            batches.append(batch_entry)
        
        try:
            with open(f'batches/batch_history_{color}.json', 'w') as f:
                json.dump(batches, f, indent=2)
        except Exception as e:
            print(f"[LOG] Could not save batch history for {color}: {e}")
        tilt_cfg[color] = batch_entry
        try:
            save_json(TILT_CONFIG_FILE, tilt_cfg)
        except Exception as e:
            print(f"[LOG] Could not save tilt_config in batch_settings: {e}")
        # Persist variances to tilt_table keyed by MAC so calibration follows the
        # physical device even if it is later reassigned to a different color slot.
        tilt_mac = live_tilts.get(color, {}).get("mac_address", "")
        if tilt_mac:
            set_device_variances(tilt_table, mac=tilt_mac,
                                 temp_variance=temp_variance,
                                 gravity_variance=gravity_variance)
            try:
                save_tilt_table(tilt_table)
            except Exception as e:
                print(f"[LOG] Could not save tilt_table in batch_settings: {e}")
        # --- PATCH: Append batch_metadata to .jsonl whenever batch is edited
        append_batch_metadata_to_batch_jsonl(color, batch_entry)
        return redirect(f"/batch_settings?tilt_color={color}")

    selected = request.args.get('tilt_color')
    action = request.args.get('action')
    config = tilt_cfg.get(selected, {}) if selected else {}
    batch_history = []
    if selected:
        try:
            with open(f'batches/batch_history_{selected}.json', 'r') as f:
                batch_history = json.load(f)
        except Exception:
            batch_history = []
    all_colors = list(TILT_UUIDS.values())
    active_tilts = get_active_tilts()
    active_colors = list(active_tilts.keys())
    # Live reading for the selected tilt (used in calibration section)
    selected_live = live_tilts.get(selected, {}) if selected else {}

    # Prefer MAC-keyed variances over color-keyed when the tilt is active and its MAC is known.
    # The tilt_table is the authoritative source for calibration, since it follows the physical
    # device even when the device is reassigned to a different color slot.
    if selected and selected_live.get("mac_address"):
        mac_tv, mac_gv = get_device_variances(tilt_table, mac=selected_live["mac_address"])
        config = dict(config)  # avoid mutating the shared tilt_cfg dict
        config["temp_variance"]    = mac_tv
        config["gravity_variance"] = mac_gv

    return render_template('batch_settings.html',
        tilt_cfg=tilt_cfg,
        tilt_colors=all_colors,
        active_colors=active_colors,
        live_tilts=active_tilts,
        selected_tilt=selected,
        selected_config=config,
        selected_live=selected_live,
        system_settings=system_cfg,
        action=action,
        batch_history=batch_history,
        color_map=COLOR_MAP
    )

def get_last_activity(activity_type):
    """
    Get the last heating or cooling activity across all per-color temp control logs.
    
    Args:
        activity_type: Either "heating" or "cooling"
    
    Returns:
        Dictionary with 'timestamp', 'date', 'time', and 'action' (On/Off) or None if not found
    """
    # Events to look for based on activity type
    if activity_type == "heating":
        events = ["heating_on", "heating_off",
                  "HEATING-PLUG TURNED ON", "HEATING-PLUG TURNED OFF"]
    elif activity_type == "cooling":
        events = ["cooling_on", "cooling_off",
                  "COOLING-PLUG TURNED ON", "COOLING-PLUG TURNED OFF"]
    else:
        return None

    last_activity = None
    try:
        for log_path in _list_all_control_log_files():
            if not os.path.exists(log_path):
                continue
            with open(log_path, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        event_type = entry.get("event")

                        if event_type in events:
                            timestamp = entry.get("timestamp", "")
                            date = entry.get("date", "")
                            time_str = entry.get("time", "")
                            action = "On" if (event_type.endswith("_on") or "TURNED ON" in event_type) else "Off"
                            last_activity = {
                                "timestamp": timestamp,
                                "date": date,
                                "time": time_str,
                                "action": action
                            }
                    except (json.JSONDecodeError, ValueError):
                        continue
    except Exception as e:
        print(f"[LOG] Error reading last {activity_type} activity: {e}")
        return None

    return last_activity

@app.route('/temp_config')
def temp_config():
    report_colors = list(tilt_cfg.keys())
    active_tilts = get_active_tilts()
    
    # Get controller_id from query parameter (default to 0)
    try:
        controller_id = int(request.args.get('controller_id', 0))
        if controller_id < 0 or controller_id > 2:
            controller_id = 0
    except (ValueError, TypeError):
        controller_id = 0
    
    # Get the specific controller
    controllers = temp_cfg.get('controllers', [])
    
    # Ensure we always have exactly 3 controllers (migration for old configs)
    original_count = len(controllers)
    while len(controllers) < 3:
        new_id = len(controllers)
        controllers.append({
            "controller_id": new_id,
            "low_limit": 50,
            "high_limit": 54,
            "tilt_color": "",
            "enable_heating": False,
            "enable_cooling": False,
            "heating_plug": "",
            "cooling_plug": "",
            "compressor_delay": 5,
            "current_temp": None,
            "heater_on": False,
            "cooler_on": False,
            "heater_pending": False,
            "cooler_pending": False,
            "heating_error": False,
            "cooling_error": False,
            "notifications_trigger": False,
            "notification_last_sent": None,
            "notification_comm_failure": False,
            "email_error": False,
            "control_initialized": True,
            "last_logged_low_limit": 50,
            "last_logged_high_limit": 54,
            "last_logged_enable_heating": False,
            "last_logged_enable_cooling": False,
            "warnings_mode": "NONE",
            "temp_control_enabled": True,
            "temp_control_active": False,
            "in_range_trigger_armed": True,
            "above_limit_trigger_armed": True,
            "below_limit_trigger_armed": True,
            "log_temp_control_tilt": True,
            "mode": "Off",
            "status": "Not Configured"
        })
    
    # Update the config if we added controllers
    if original_count < 3:
        temp_cfg['controllers'] = controllers
        save_json(TEMP_CFG_FILE, temp_cfg)
    
    if controller_id < len(controllers):
        current_controller = controllers[controller_id]
    else:
        # Fallback: create a default controller
        current_controller = {
            "controller_id": controller_id,
            "low_limit": 50,
            "high_limit": 54,
            "tilt_color": "",
            "enable_heating": False,
            "enable_cooling": False,
            "heating_plug": "",
            "cooling_plug": "",
            "temp_control_enabled": True,
            "temp_control_active": False
        }
    
    # Get last heating and cooling activity for this controller
    heating_url = current_controller.get("heating_plug", "")
    cooling_url = current_controller.get("cooling_plug", "")
    heating_last_activity = get_last_activity("heating") if heating_url else None
    cooling_last_activity = get_last_activity("cooling") if cooling_url else None

    # Build the list of MAC-specific device options for the tilt-selector dropdown.
    # These supplement the plain-color options with entries for specific physical
    # devices when more than one device of the same color is in range.
    active_tilt_devices = []
    seen_keys = set()
    for tilt_key, info in live_tilts.items():
        if ':' not in tilt_key:
            continue  # skip plain-color entries — they're covered by report_colors
        mac = tilt_key_mac(tilt_key)
        if not mac or mac in seen_keys:
            continue
        seen_keys.add(mac)
        base_color = tilt_key_base(tilt_key)
        tilt_type = 'Pro/Mini-Pro' if info.get('is_pro') else 'Standard'
        label = f"{base_color} ({tilt_type}) \u2014 {mac}"
        active_tilt_devices.append({
            'value': tilt_key,
            'label': label,
            'base_color': base_color,
        })

    # Human-readable label for the currently assigned tilt (shown next to the toggle)
    assigned_tilt_key = current_controller.get("tilt_color", "")
    if assigned_tilt_key:
        assigned_info = live_tilts.get(assigned_tilt_key)
        assigned_tilt_label = tilt_display_label(assigned_tilt_key, live_info=assigned_info)
    else:
        assigned_tilt_label = ''

    return render_template('temp_control_config.html',
        temp_control=current_controller,
        controller_id=controller_id,
        controllers=controllers,
        tilt_cfg=tilt_cfg,
        system_settings=system_cfg,
        batch_cfg=tilt_cfg,
        report_colors=report_colors,
        live_tilts=active_tilts,
        active_tilt_devices=active_tilt_devices,
        assigned_tilt_label=assigned_tilt_label,
        heating_last_activity=heating_last_activity,
        cooling_last_activity=cooling_last_activity,
        force_iot_port=_FORCE_IOT_PORT
    )


@app.route('/update_temp_config', methods=['POST'])
def update_temp_config():
    data = request.form
    try:
        # Get controller_id from form (default to 0)
        try:
            controller_id = int(data.get('controller_id', 0))
            if controller_id < 0 or controller_id > 2:
                controller_id = 0
        except (ValueError, TypeError):
            controller_id = 0
        
        # Get the controller
        controllers = temp_cfg.get('controllers', [])
        # Ensure we always have exactly 3 controllers
        while len(controllers) < 3:
            new_id = len(controllers)
            controllers.append({
                "controller_id": new_id, "low_limit": 50, "high_limit": 54,
                "tilt_color": "", "enable_heating": False, "enable_cooling": False,
                "heating_plug": "", "cooling_plug": "", "temp_control_enabled": True,
                "temp_control_active": False, "mode": "Off", "status": "Not Configured"
            })
        temp_cfg['controllers'] = controllers
        if controller_id >= len(controllers):
            print(f"[LOG] ERROR: controller_id {controller_id} out of range")
            return redirect('/temp_config?controller_id=0')
        
        controller = controllers[controller_id]
        
        # Get the current and new tilt assignments
        old_tilt_color = controller.get("tilt_color", "")
        new_tilt_color = data.get('tilt_color', '')
        
        # Parse and validate temperature limits
        # Preserve existing values if form fields are empty or invalid
        low_limit_value = data.get('low_limit', '').strip()
        high_limit_value = data.get('high_limit', '').strip()
        
        # Only update low_limit if a valid value is provided
        if low_limit_value:
            try:
                low_limit = float(low_limit_value)
            except (ValueError, TypeError):
                # Invalid value - keep existing
                low_limit = controller.get("low_limit", 0.0)
                print(f"[LOG] Invalid low_limit value '{low_limit_value}', keeping existing value {low_limit}")
        else:
            # Empty field - keep existing value
            low_limit = controller.get("low_limit", 0.0)
        
        # Only update high_limit if a valid value is provided
        if high_limit_value:
            try:
                high_limit = float(high_limit_value)
            except (ValueError, TypeError):
                # Invalid value - keep existing
                high_limit = controller.get("high_limit", 100.0)
                print(f"[LOG] Invalid high_limit value '{high_limit_value}', keeping existing value {high_limit}")
        else:
            # Empty field - keep existing value
            high_limit = controller.get("high_limit", 100.0)
        
        # Validate that high_limit > low_limit
        if high_limit <= low_limit:
            print(f"[LOG] ERROR: high_limit ({high_limit}) must be greater than low_limit ({low_limit})")
            # Don't update - keep existing values
            low_limit = controller.get("low_limit", 0.0)
            high_limit = controller.get("high_limit", 100.0)
        
        # Never wipe an existing tilt assignment with an empty submission.
        # This can happen when the temp-config page is visited while the mini-pro
        # is offline: the "Active Tilt Devices" group is empty, no option matches
        # the saved composite key, the browser leaves the <select> blank, and the
        # form submits with tilt_color="".  Preserve the existing value instead.
        if not new_tilt_color:
            new_tilt_color = controller.get("tilt_color", "")

        controller.update({
            "tilt_color": new_tilt_color,
            "low_limit": low_limit,
            "high_limit": high_limit,
            "enable_heating": 'enable_heating' in data,
            "enable_cooling": 'enable_cooling' in data,
            "heating_plug": data.get("heating_plug", ""),
            "cooling_plug": data.get("cooling_plug", ""),
            "heating_plug_port": _parse_plug_port(data.get("heating_plug_port")),
            "cooling_plug_port": _parse_plug_port(data.get("cooling_plug_port")),
            "mode": data.get("mode", controller.get('mode','')),
            "status": data.get("status", controller.get('status',''))
        })

        # Remove duplicate plug assignments: if a plug URL now assigned to this controller
        # was previously assigned to another controller (or to both roles on this controller),
        # clear it from every other location to prevent cross-assignment.
        new_plugs = {
            "heating_plug": controller.get("heating_plug", "").strip(),
            "cooling_plug": controller.get("cooling_plug", "").strip(),
        }
        for i, other_ctrl in enumerate(controllers):
            if i == controller_id:
                continue
            for role in ("heating_plug", "cooling_plug"):
                for new_url in new_plugs.values():
                    if new_url and other_ctrl.get(role, "").strip() == new_url:
                        other_ctrl[role] = ""
                        print(f"[LOG] Removed duplicate {role} assignment from controller {i}")

        # If a new Tilt is being assigned (or changed), record the assignment time
        # This starts the grace period for the newly assigned Tilt
        if new_tilt_color and new_tilt_color != old_tilt_color:
            from datetime import datetime
            controller["tilt_assignment_time"] = datetime.utcnow().isoformat()
            print(f"[TEMP_CONTROL] Controller {controller_id}: Tilt '{new_tilt_color}' assigned - starting 15-minute grace period")
        elif not new_tilt_color and old_tilt_color:
            # Tilt was unassigned - clear the assignment time
            controller.pop("tilt_assignment_time", None)
            print(f"[TEMP_CONTROL] Controller {controller_id}: Tilt unassigned")
            
    except Exception as e:
        print(f"[LOG] Error parsing temp config form: {e}")
    try:
        save_json(TEMP_CFG_FILE, temp_cfg)
    except Exception as e:
        print(f"[LOG] Error saving config in update_temp_config: {e}")


    # Run control logic immediately (it will normalize mode/status and log selection change if any)
    temperature_control_logic()

    return redirect(f'/temp_config?controller_id={controller_id}')


@app.route('/toggle_temp_control', methods=['POST'])
def toggle_temp_control():
    """Toggle the temp_control_active state (ON/OFF switch on temp control card).
    
    When turning ON, if 'new_session' is True in the request, archive the existing log.
    """
    try:
        data = request.get_json() if request.is_json else request.form
        
        # Get controller_id (default to 0)
        try:
            controller_id = int(data.get('controller_id', 0))
            if controller_id < 0 or controller_id > 2:
                controller_id = 0
        except (ValueError, TypeError):
            controller_id = 0
        
        # Get the controller
        controllers = temp_cfg.get('controllers', [])
        if controller_id >= len(controllers):
            return jsonify({'error': f'Controller {controller_id} not found'}), 400
        
        controller = controllers[controller_id]
        
        # Standardize on boolean JSON values
        active_value = data.get('active')
        if isinstance(active_value, bool):
            new_state = active_value
        elif isinstance(active_value, str):
            new_state = active_value.lower() in ('true', '1')
        else:
            new_state = bool(active_value)
        
        # Check if this is a new session request (archive existing log)
        new_session = data.get('new_session', False)
        if isinstance(new_session, str):
            new_session = new_session.lower() in ('true', '1')
        
        # If turning ON and new_session is requested, archive the existing log
        if new_state and new_session:
            try:
                tilt_color = controller.get("tilt_color", "unknown")
                color_log_path = _get_control_log_path(tilt_color)
                if os.path.exists(color_log_path):
                    # Create logs directory if it doesn't exist
                    logs_dir = 'logs'
                    os.makedirs(logs_dir, exist_ok=True)
                    
                    # Generate archive filename with timestamp
                    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                    archive_name = f"temp_control_controller{controller_id}_{tilt_color}_{timestamp}.jsonl"
                    archive_path = os.path.join(logs_dir, archive_name)
                    
                    # Move the existing per-color log to archive
                    shutil.move(color_log_path, archive_path)
                    print(f"[LOG] Controller {controller_id}: Archived temp control log to {archive_path}")
            except Exception as e:
                print(f"[LOG] Error archiving temp control log: {e}")
                return jsonify({"success": False, "error": f"Failed to archive log: {str(e)}"}), 500
        
        controller['temp_control_active'] = new_state
        
        if new_state:
            # When turning ON, arm all triggers and log the start event
            controller['in_range_trigger_armed'] = True
            controller['above_limit_trigger_armed'] = True
            controller['below_limit_trigger_armed'] = True
            append_control_log("temp_control_started", {
                "controller_id": controller_id,
                "low_limit": controller.get("low_limit"),
                "current_temp": controller.get("current_temp"),
                "high_limit": controller.get("high_limit"),
                "tilt_color": controller.get("tilt_color", "")
            })
        else:
            # When turning OFF, turn off both heating and cooling plugs
            control_heating("off", controller)
            control_cooling("off", controller)
            append_control_log("temp_control_stopped", {
                "controller_id": controller_id,
                "low_limit": controller.get("low_limit"),
                "current_temp": controller.get("current_temp"),
                "high_limit": controller.get("high_limit"),
                "tilt_color": controller.get("tilt_color", "")
            })
        
        # Save the state
        save_json(TEMP_CFG_FILE, temp_cfg)
        
        # If this was a new session, signal that we should redirect to temp_config
        redirect_url = f"/temp_config?controller_id={controller_id}" if (new_state and new_session) else None
        return jsonify({"success": True, "active": new_state, "redirect": redirect_url})
    except Exception as e:
        print(f"[LOG] Error toggling temp control: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/temp_report', methods=['GET', 'POST'])
def temp_report():
    if request.method == 'POST':
        color = request.form.get('tilt_color')
        if not color:
            return redirect('/temp_report')
        return redirect(f"/temp_report?tilt_color={color}&page=1")


    tilt_color = request.args.get('tilt_color')
    try:
        page = int(request.args.get('page', '1'))
    except Exception:
        page = 1


    if not tilt_color:
        colors = list(tilt_cfg.keys())
        default_color = colors[0] if colors else None
        return render_template('temp_report_select.html', colors=colors, default_color=default_color)


    entries = []
    try:
        color_log_path = _get_control_log_path(tilt_color)
        if os.path.exists(color_log_path):
            with open(color_log_path, 'r') as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get('event') != 'tilt_reading' and obj.get('event') != 'SAMPLE':
                        continue
                    payload = obj.get('payload') or obj if isinstance(obj, dict) else {}
                    entries.append(payload)
    except Exception as e:
        print(f"[LOG] Could not read log for temp_report: {e}")


    filtered = []
    _tilt_base_color = tilt_key_base(tilt_color)
    brewid = tilt_cfg.get(_tilt_base_color, {}).get('brewid')
    tc = tilt_cfg.get(_tilt_base_color, {}) or {}
    for p in entries:
        if brewid:
            if p.get('brewid') == brewid:
                filtered.append(p)
        else:
            if p.get('batch_name') == tc.get('batch_name') or p.get('beer_name') == tc.get('beer_name'):
                filtered.append(p)


    lines = []
    filtered = list(reversed(filtered))
    for p in filtered:
        ts = p.get('timestamp', '')
        bn = p.get('beer_name') or ''
        batch = p.get('batch_name') or ''
        tempf = p.get('temp_f', '')
        grav = p.get('gravity', '')
        bid = p.get('brewid') or '--'
        lines.append(f"{ts} — {bn or batch} — Temp: {tempf}°F — Gravity: {grav} — Brew ID: {bid}")


    total_pages = max(1, ceil(len(lines) / PER_PAGE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    page_data = lines[start:end]
    at_end = page >= total_pages


    return render_template('temp_report_display.html',
                           color=tilt_color,
                           page=page,
                           total_pages=total_pages,
                           page_data=page_data,
                           at_end=at_end)


@app.route('/batch_history')
def batch_history():
    """
    Display batch history selection page.
    
    Shows two sections:
    1. Current Activity - all active batches (is_active=True), sorted by user criteria
    2. Batch History - all closed batches (is_active=False), sorted by user criteria
    
    Archive location: batches/archive/ directory
    """
    # Get sort order from query parameter (default: newest first)
    sort_order = request.args.get('sort', 'newest')
    
    active_batches = []
    closed_batches = []
    
    # Check each color for batch history
    for color in TILT_UUIDS.values():
        batch_history_file = f'batches/batch_history_{color}.json'
        if os.path.exists(batch_history_file):
            try:
                with open(batch_history_file, 'r') as f:
                    batches = json.load(f)
                    for batch in batches:
                        # Add color to batch for display
                        batch['color'] = color
                        # Migrate old batches without is_active field (default to active)
                        if 'is_active' not in batch:
                            batch['is_active'] = True
                        
                        if batch.get('is_active', True):
                            active_batches.append(batch)
                        else:
                            closed_batches.append(batch)
            except Exception as e:
                print(f"[LOG] Error loading batch history for {color}: {e}")
    
    # Define sorting function
    def apply_sort(batches, sort_order):
        if sort_order == 'newest':
            return sorted(batches, key=lambda x: x.get('ferm_start_date', ''), reverse=True)
        elif sort_order == 'oldest':
            return sorted(batches, key=lambda x: x.get('ferm_start_date', ''))
        elif sort_order == 'beer_name':
            return sorted(batches, key=lambda x: (x.get('beer_name', '').lower(), x.get('ferm_start_date', '')))
        elif sort_order == 'color':
            return sorted(batches, key=lambda x: (x.get('color', ''), x.get('ferm_start_date', '')), reverse=True)
        return batches
    
    # Apply sorting to both sections
    active_batches = apply_sort(active_batches, sort_order)
    closed_batches = apply_sort(closed_batches, sort_order)
    
    return render_template('batch_history_select.html',
                         active_batches=active_batches,
                         closed_batches=closed_batches,
                         color_map=COLOR_MAP,
                         sort_order=sort_order)


@app.route('/batch_review/<brewid>')
def batch_review(brewid):
    """Display detailed review of a specific batch by brewid."""
    # Sanitize brewid to prevent directory traversal attacks
    # Only allow alphanumeric characters and hyphens
    import re
    if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
        return "Invalid batch ID", 400
    
    # Find the batch in batch_history files
    batch_info = None
    color = None
    
    for c in TILT_UUIDS.values():
        batch_history_file = f'batches/batch_history_{c}.json'
        if os.path.exists(batch_history_file):
            try:
                with open(batch_history_file, 'r') as f:
                    batches = json.load(f)
                    for b in batches:
                        if b.get('brewid') == brewid:
                            batch_info = b
                            color = c
                            break
            except Exception:
                pass
        if batch_info:
            break
    
    if not batch_info:
        return "Batch not found", 404
    
    # Load batch data from JSONL file
    batch_data = []
    batch_file = None
    
    # Check with glob pattern for files matching the brewid (now sanitized)
    batch_files = glob_func(f'batches/*{brewid}*.jsonl')
    
    if batch_files:
        batch_file = batch_files[0]
        try:
            with open(batch_file, 'r') as f:
                for line in f:
                    if line.strip():
                        try:
                            entry = json.loads(line)
                            batch_data.append(entry)
                        except Exception:
                            pass
        except Exception:
            pass
    
    # Calculate statistics from batch data
    stats = calculate_batch_statistics(batch_data, batch_info)
    
    return render_template('batch_review.html',
                         batch=batch_info,
                         color=color,
                         batch_data=batch_data,
                         stats=stats,
                         color_map=COLOR_MAP)


def calculate_batch_statistics(batch_data, batch_info):
    """
    Calculate statistics from batch data.
    
    Issue 5 fix: Ensures statistics use the FULL data point set from batch_data.
    """
    # Initialize stats - total_readings counts ALL entries, not just samples
    all_samples = []
    for entry in batch_data:
        if entry.get('event') in ['sample', 'SAMPLE', 'tilt_reading']:
            payload = entry.get('payload', entry)
            all_samples.append(payload)
    
    stats = {
        'total_readings': len(all_samples),  # Count actual sample entries
        'duration_days': None,
        'start_gravity': None,
        'end_gravity': None,
        'gravity_change': None,
        'start_temp': None,
        'end_temp': None,
        'avg_temp': None,
        'min_temp': None,
        'max_temp': None,
        'estimated_abv': None
    }
    
    if not all_samples:
        return stats
    
    # Calculate temperature statistics from ALL samples
    temps = [s.get('temp_f') for s in all_samples if s.get('temp_f') is not None]
    if temps:
        stats['avg_temp'] = round(sum(temps) / len(temps), 1)
        stats['min_temp'] = min(temps)
        stats['max_temp'] = max(temps)
        stats['start_temp'] = temps[0]
        stats['end_temp'] = temps[-1]
    
    # Calculate gravity statistics from ALL samples
    gravities = [s.get('gravity') for s in all_samples if s.get('gravity') is not None]
    if gravities:
        stats['start_gravity'] = gravities[0]
        stats['end_gravity'] = gravities[-1]
        stats['gravity_change'] = round(gravities[0] - gravities[-1], 3)
    
    # Calculate ABV if we have actual_og
    actual_og = batch_info.get('actual_og')
    if actual_og and gravities:
        try:
            og_float = float(actual_og)
            final_gravity = gravities[-1]
            # ABV = (OG - FG) * 131.25
            stats['estimated_abv'] = round((og_float - final_gravity) * 131.25, 2)
        except (ValueError, TypeError):
            pass
    
    # Calculate duration from ALL timestamps
    timestamps = [s.get('timestamp') for s in all_samples if s.get('timestamp')]
    if len(timestamps) >= 2:
        try:
            start_time = datetime.fromisoformat(timestamps[0].replace('Z', '+00:00'))
            end_time = datetime.fromisoformat(timestamps[-1].replace('Z', '+00:00'))
            duration = end_time - start_time
            stats['duration_days'] = duration.days
        except Exception:
            pass
    
    return stats


@app.route('/batch_data_view/<brewid>')
def batch_data_view(brewid):
    """
    View all batch data with optional range selection.
    Allows viewing a subset of data points by start/end parameters.
    """
    import re
    if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
        return "Invalid batch ID", 400
    
    # Find the batch in batch_history files
    batch_info = None
    color = None
    
    for c in TILT_UUIDS.values():
        batch_history_file = f'batches/batch_history_{c}.json'
        if os.path.exists(batch_history_file):
            try:
                with open(batch_history_file, 'r') as f:
                    batches = json.load(f)
                    for b in batches:
                        if b.get('brewid') == brewid:
                            batch_info = b
                            color = c
                            break
            except Exception:
                pass
        if batch_info:
            break
    
    if not batch_info:
        return "Batch not found", 404
    
    # Load batch data from JSONL file
    batch_data = []
    batch_files = glob_func(f'batches/*{brewid}*.jsonl')
    
    if batch_files:
        batch_file = batch_files[0]
        try:
            with open(batch_file, 'r') as f:
                for line in f:
                    if line.strip():
                        try:
                            entry = json.loads(line)
                            batch_data.append(entry)
                        except Exception:
                            pass
        except Exception:
            pass
    
    # Extract samples
    all_samples = []
    for entry in batch_data:
        if entry.get('event') in ['sample', 'SAMPLE', 'tilt_reading']:
            payload = entry.get('payload', entry)
            all_samples.append(payload)
    
    # Apply range filter if specified
    start_idx = request.args.get('start', type=int)
    end_idx = request.args.get('end', type=int)
    
    if start_idx is not None or end_idx is not None:
        # Convert to 0-based index
        start = (start_idx - 1) if start_idx else 0
        end = end_idx if end_idx else len(all_samples)
        all_samples = all_samples[start:end]
    
    # Calculate statistics
    stats = calculate_batch_statistics(batch_data, batch_info)
    
    return render_template('batch_data_view.html',
                         batch=batch_info,
                         color=color,
                         samples=all_samples,
                         stats=stats,
                         color_map=COLOR_MAP,
                         start_idx=start_idx,
                         end_idx=end_idx)


@app.route('/export_batch_data_csv/<brewid>')
def export_batch_data_csv(brewid):
    """
    Export batch data to CSV with optional range selection.
    """
    import csv
    import re
    
    if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
        return "Invalid batch ID", 400
    
    # Find the batch
    batch_info = None
    color = None
    
    for c in TILT_UUIDS.values():
        batch_history_file = f'batches/batch_history_{c}.json'
        if os.path.exists(batch_history_file):
            try:
                with open(batch_history_file, 'r') as f:
                    batches = json.load(f)
                    for b in batches:
                        if b.get('brewid') == brewid:
                            batch_info = b
                            color = c
                            break
            except Exception:
                pass
        if batch_info:
            break
    
    if not batch_info:
        return "Batch not found", 404
    
    # Load batch data
    batch_data = []
    batch_files = glob_func(f'batches/*{brewid}*.jsonl')
    
    if batch_files:
        batch_file = batch_files[0]
        try:
            with open(batch_file, 'r') as f:
                for line in f:
                    if line.strip():
                        try:
                            entry = json.loads(line)
                            batch_data.append(entry)
                        except Exception:
                            pass
        except Exception:
            pass
    
    # Extract samples
    all_samples = []
    for entry in batch_data:
        if entry.get('event') in ['sample', 'SAMPLE', 'tilt_reading']:
            payload = entry.get('payload', entry)
            all_samples.append(payload)
    
    # Apply range filter if specified
    start_idx = request.args.get('start', type=int)
    end_idx = request.args.get('end', type=int)
    
    if start_idx is not None or end_idx is not None:
        start = (start_idx - 1) if start_idx else 0
        end = end_idx if end_idx else len(all_samples)
        all_samples = all_samples[start:end]
    
    # Create CSV export
    export_dir = 'export'
    os.makedirs(export_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    beer_name = batch_info.get('beer_name', 'batch').replace(' ', '_')
    filename = f'{beer_name}_{brewid[:8]}_{timestamp}.csv'
    filepath = os.path.join(export_dir, filename)
    
    try:
        with open(filepath, 'w', newline='') as csvfile:
            fieldnames = ['timestamp', 'tilt_color', 'gravity', 'temp_f', 'rssi', 'beer_name', 'batch_name']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
            
            writer.writeheader()
            for sample in all_samples:
                writer.writerow(sample)
        
        # Send file for download
        from flask import send_file
        return send_file(filepath, as_attachment=True, download_name=filename)
    except Exception as e:
        return f"Error exporting CSV: {str(e)}", 500


@app.route('/export_temp_log', methods=['GET', 'POST'])
def export_temp_log():
    return redirect('/temp_config')


@app.route('/export_temp_csv', methods=['GET', 'POST'])
def export_temp_csv():
    return redirect('/temp_config')


@app.route('/export_temp_control_csv', methods=['POST'])
def export_temp_control_csv():
    """Export temperature control log data to CSV in the /export directory."""
    try:
        import csv
        from datetime import datetime

        # Create export directory if it doesn't exist
        export_dir = 'export'
        os.makedirs(export_dir, exist_ok=True)

        # Determine which log file(s) to export.
        # When a specific log_file is posted, export just that one; otherwise export all.
        requested_file = os.path.basename(request.form.get('log_file', '').strip())
        if requested_file:
            if not _TEMP_CONTROL_LOG_RE.match(requested_file):
                return jsonify({'success': False, 'error': 'Invalid log file name'}), 400
            log_paths = [os.path.join(TEMP_CONTROL_DIR, requested_file)]
            csv_label = requested_file.replace('.jsonl', '')
        else:
            log_paths = _list_all_control_log_files()
            csv_label = 'temp_control_all'

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'{csv_label}_{timestamp}.csv'
        filepath = os.path.join(export_dir, filename)

        # Read data from the selected log file(s)
        data_rows = []
        for log_path in log_paths:
            if not os.path.exists(log_path):
                continue
            with open(log_path, 'r') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get('event') in ALLOWED_EVENT_VALUES:
                            data_rows.append(obj)
                    except Exception as e:
                        print(f"[LOG] Error parsing line in export: {e}")
                        continue

        # Write to CSV
        if data_rows:
            with open(filepath, 'w', newline='') as csvfile:
                fieldnames = ['timestamp', 'date', 'time', 'tilt_color', 'brewid',
                              'low_limit', 'current_temp', 'temp_f', 'gravity',
                              'high_limit', 'event']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                for row in data_rows:
                    writer.writerow(row)

            return jsonify({'success': True, 'filename': filename, 'rows': len(data_rows)})
        else:
            return jsonify({'success': False, 'error': 'No data to export'})

    except Exception as e:
        print(f"[LOG] Error exporting temp control CSV: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/scan_kasa_plugs')
def scan_kasa_plugs():
    try:
        controller_id = int(request.args.get('controller_id', 0))
        if controller_id < 0 or controller_id > 2:
            controller_id = 0
    except (ValueError, TypeError):
        controller_id = 0
    try:
        from kasa import Discover
        _discover_kwargs = {}
        if not _FORCE_IOT_PORT:
            _kasa_user = system_cfg.get('kasa_username', '').strip()
            _kasa_pass = system_cfg.get('kasa_password', '').strip()
            if _kasa_user and _kasa_pass:
                try:
                    from kasa import Credentials as _KasaCredentials
                    _discover_kwargs['credentials'] = _KasaCredentials(
                        username=_kasa_user, password=_kasa_pass)
                except Exception:
                    pass
        found_devices = _run_async_in_thread(
            asyncio.wait_for(Discover.discover(**_discover_kwargs), timeout=15), timeout=15
        )
        devices = {str(addr): dev.alias for addr, dev in found_devices.items()}
    except Exception as e:
        devices = {}
        print(f"[LOG] Kasa scan failed: {e}")

    # Build assignment map: ip -> list of {controller_id, role, tilt_color, color_code}
    assignments = {}
    for ctrl in temp_cfg.get('controllers', []):
        heating_plug = ctrl.get('heating_plug', '').strip()
        cooling_plug = ctrl.get('cooling_plug', '').strip()
        tilt_color = ctrl.get('tilt_color', '')
        color_code = get_tilt_color_hex(tilt_color)
        cid = ctrl.get('controller_id', 0)
        if heating_plug:
            assignments.setdefault(heating_plug, []).append({
                'controller_id': cid,
                'role': 'Heating',
                'tilt_color': tilt_color,
                'color_code': color_code
            })
        if cooling_plug:
            assignments.setdefault(cooling_plug, []).append({
                'controller_id': cid,
                'role': 'Cooling',
                'tilt_color': tilt_color,
                'color_code': color_code
            })

    return render_template("kasa_scan_results.html", devices=devices, error=None,
                           controller_id=controller_id, assignments=assignments)


@app.route('/temp_summary/<int:controller_id>')
def temp_summary(controller_id):
    """Display a summary of temperature control settings and chart for a controller."""
    if controller_id < 0 or controller_id > 2:
        controller_id = 0
    controllers = temp_cfg.get('controllers', [])
    if controller_id < len(controllers):
        controller = controllers[controller_id]
    else:
        controller = {"controller_id": controller_id, "mode": "Off", "status": "Not Configured"}
    tilt_color = controller.get('tilt_color', '')
    color_code = get_tilt_color_hex(tilt_color) if tilt_color else '#8B4513'
    return render_template('temp_summary.html',
                           controller=controller,
                           controller_id=controller_id,
                           tilt_color=tilt_color,
                           color_code=color_code,
                           system_settings=system_cfg)


def format_kasa_error(error_msg, device_url):
    """Format KASA error messages to be more user-friendly"""
    error_str = str(error_msg)
    
    # Check if this is a localhost address - this is a common configuration mistake
    if device_url.startswith('127.') or device_url == 'localhost':
        return f"❌ Invalid IP address: {device_url} is a localhost address. KASA plugs require a real network IP address (typically 192.168.x.x or 10.0.x.x). Check your router's DHCP client list or use the Kasa mobile app to find the plug's actual IP address."
    
    # Connection refused errors (port closed, device not listening)
    if 'Errno 111' in error_str or 'Connect call failed' in error_str or 'Connection refused' in error_str:
        return f"Cannot connect to device. Please check: (1) the device is powered on, (2) the IP address {device_url} is correct, (3) the device is on the same network"
    
    # Timeout errors
    if 'TimeoutError' in error_str or 'timed out' in error_str.lower():
        return f"Connection timed out. The device may be unreachable or turned off"
    
    # Host unreachable
    if 'Errno 113' in error_str or 'No route to host' in error_str:
        return f"Network error: No route to {device_url}. Check network configuration"
    
    # Name resolution errors
    if 'Name or service not known' in error_str or 'getaddrinfo failed' in error_str:
        return f"Cannot resolve hostname: {device_url}. Use an IP address instead"
    
    # Permission errors
    if 'Errno 13' in error_str or 'Permission denied' in error_str:
        return "Permission denied. Network configuration issue"
    
    # Default: return a simplified version
    # Try to extract the most relevant part
    if 'Unable to connect' in error_str:
        # Already formatted nicely by kasa library
        return error_str.split('\n')[0]  # Just first line
    
    return error_str


def _run_async_in_thread(coro, timeout=10):
    """
    Run an async coroutine in a dedicated thread with its own event loop.

    Using a dedicated thread guarantees that asyncio.run() never encounters an
    already-running event loop (which can happen in some hosting environments)
    and prevents the Flask request thread from being blocked while also
    providing a clean shutdown path for the event loop.

    Returns the coroutine result on success, raises the caught exception on
    failure, or raises TimeoutError if the thread itself hangs past the
    hard deadline.
    """
    result_holder = [None]
    exc_holder = [None]

    def _thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_holder[0] = loop.run_until_complete(coro)
        except Exception as e:
            exc_holder[0] = e
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_thread_target, daemon=True)
    t.start()
    # Allow a small buffer beyond the coroutine's own timeout so the
    # thread has time to clean up before we declare it stuck.
    t.join(timeout=timeout + 5)

    if t.is_alive():
        raise TimeoutError(f"Async operation did not complete within {timeout} seconds")
    if exc_holder[0] is not None:
        raise exc_holder[0]
    return result_holder[0]


def _clear_kasa_error_for_plug(mode, url):
    """
    Clear all error-tracking fields for every controller whose plug of the
    given mode (``'heating'`` or ``'cooling'``) matches *url*.

    Called after a direct connectivity test succeeds so that the TC card no
    longer shows "Kasa Connection Lost" while the settings test shows
    "Connected".  A successful ``plug.update()`` is the authoritative proof
    that the plug is currently reachable; any prior error flag was set by a
    past transient failure and is now stale.
    """
    plug_key = f'{mode}_plug'
    for ctrl in temp_cfg.get('controllers', []):
        if ctrl.get(plug_key, '') == url:
            ctrl[f'{mode}_error'] = False
            ctrl[f'{mode}_error_msg'] = ''
            ctrl[f'{mode}_error_notified'] = False
            ctrl[f'{mode}_kasa_error_since'] = 0
            ctrl[f'{mode}_kasa_error_notified_at'] = 0


@app.route('/test_kasa_plugs', methods=['POST'])
def test_kasa_plugs():
    """Test connectivity to the configured Kasa heating/cooling plugs.

    Delegates to kasa_manager.query_sync() so the same credential-aware,
    timeout-safe code path used for real plug control is exercised here.
    Works for both old unauthenticated plugs (IotPlug) and new KLAP/auth
    plugs (EP25 v2.6+).
    """
    data = request.get_json(silent=True) or {}
    heating_url = (data.get('heating_url') or '').strip()
    cooling_url = (data.get('cooling_url') or '').strip()

    if not heating_url and not cooling_url:
        return jsonify({'none_configured': True})

    # Determine the controller_id from the request, defaulting to 0.
    try:
        controller_id = int(data.get('controller_id', 0))
    except (TypeError, ValueError):
        controller_id = 0

    heating_port = _parse_plug_port(data.get('heating_port'))
    cooling_port = _parse_plug_port(data.get('cooling_port'))

    # In --plug mode all plugs use IOT/port 9999; ignore per-request port values.
    if _FORCE_IOT_PORT:
        heating_port = None
        cooling_port = None

    # Check whether TP-Link credentials are configured.  Newer Kasa devices
    # (EP25 v2.6+) use the KLAP protocol which requires authentication; without
    # credentials the library falls back to the legacy IotPlug path which only
    # works with older devices (HS100, HS105, …) on port 9999.
    # In --plug mode credentials are intentionally not used, so skip this warning.
    has_credentials = bool(
        system_cfg.get('kasa_username', '').strip() and
        system_cfg.get('kasa_password', '').strip()
    )

    results = {}
    if not has_credentials and not _FORCE_IOT_PORT:
        results['credentials_warning'] = (
            'No TP-Link credentials configured. '
            'Newer Kasa plugs (EP25 v2.6+ / KLAP) require your TP-Link account '
            'email and password — add them in System Settings. '
            'Legacy plugs (HS100, HS105, …) do not need credentials.'
        )

    TEST_TIMEOUT = 15  # seconds — enough for KLAP handshake + update

    if heating_url:
        try:
            if kasa_manager is not None and kasa_manager.is_alive():
                _, error = kasa_manager.query_sync(
                    url=heating_url,
                    controller_id=controller_id,
                    role='heating',
                    timeout=TEST_TIMEOUT,
                    port=heating_port,
                )
                if error is None:
                    results['heating'] = {'success': True, 'error': None}
                    _clear_kasa_error_for_plug('heating', heating_url)
                else:
                    results['heating'] = {'success': False, 'error': error}
            else:
                results['heating'] = {'success': False, 'error': 'Kasa manager not running — restart the application'}
        except Exception as e:
            results['heating'] = {'success': False, 'error': str(e)}

    if cooling_url:
        try:
            if kasa_manager is not None and kasa_manager.is_alive():
                _, error = kasa_manager.query_sync(
                    url=cooling_url,
                    controller_id=controller_id,
                    role='cooling',
                    timeout=TEST_TIMEOUT,
                    port=cooling_port,
                )
                if error is None:
                    results['cooling'] = {'success': True, 'error': None}
                    _clear_kasa_error_for_plug('cooling', cooling_url)
                else:
                    results['cooling'] = {'success': False, 'error': error}
            else:
                results['cooling'] = {'success': False, 'error': 'Kasa manager not running — restart the application'}
        except Exception as e:
            results['cooling'] = {'success': False, 'error': str(e)}

    return jsonify(results)


@app.route('/live_snapshot')
def live_snapshot():
    # Build controller data for all active controllers
    controllers_data = []
    for controller in temp_cfg.get('controllers', []):
        tilt_color = controller.get("tilt_color", "")
        controllers_data.append({
            "controller_id": controller.get("controller_id", 0),
            "current_temp": controller.get("current_temp"),
            "low_limit": controller.get("low_limit"),
            "high_limit": controller.get("high_limit"),
            "tilt_color": tilt_color,
            "tilt_color_code": get_tilt_color_hex(tilt_color),
            "heater_on": controller.get("heater_on"),
            "cooler_on": controller.get("cooler_on"),
            "heater_pending": controller.get("heater_pending"),
            "cooler_pending": controller.get("cooler_pending"),
            "enable_heating": controller.get("enable_heating"),
            "enable_cooling": controller.get("enable_cooling"),
            "status": controller.get("status"),
            "mode": controller.get("mode", 'Off'),
            "temp_control_active": controller.get('temp_control_active', False),
            "heating_error": controller.get('heating_error', False),
            "cooling_error": controller.get('cooling_error', False),
            "push_error": controller.get('push_error', False),
            "email_error": controller.get('email_error', False),
            "swapped_plugs_detected": controller.get('swapped_plugs_detected', False),
            "swapped_plug_type": controller.get('swapped_plug_type', ''),
            "notifications_trigger": controller.get('notifications_trigger'),
            "notification_comm_failure": controller.get('notification_comm_failure'),
            "last_reading_time": controller.get('last_reading_time')
        })
    
    snapshot = {
        "live_tilts": {},
        "controllers": controllers_data,
        "warning_mode": system_cfg.get('warning_mode')
    }
    # Only include active tilts (those that have sent data recently)
    active_tilts = get_active_tilts()
    for color, info in active_tilts.items():
        snapshot["live_tilts"][color] = {
            "gravity": info.get("gravity"),
            "temp_f": info.get("temp_f"),
            "timestamp": info.get("timestamp"),
            "beer_name": info.get("beer_name"),
            "batch_name": info.get("batch_name"),
            "brewid": info.get("brewid"),
            "recipe_og": info.get("recipe_og"),
            "recipe_fg": info.get("recipe_fg"),
            "recipe_abv": info.get("recipe_abv"),
            "actual_og": info.get("actual_og"),
            "og_confirmed": info.get("og_confirmed", False),
            "original_gravity": info.get("original_gravity"),
            "color_code": info.get("color_code"),
            "mac_address": info.get("mac_address", ""),
            "is_pro": info.get("is_pro", False),
            "temp_variance": info.get("temp_variance", 0),
            "gravity_variance": info.get("gravity_variance", 0),
            "adj_temp_f": info.get("adj_temp_f"),
            "adj_gravity": info.get("adj_gravity"),
        }
    return jsonify(snapshot)


@app.route('/server_info')
def server_info():
    """Diagnostic endpoint — returns a JSON object identifying which server
    binary, directory, and OS process is handling the request.

    Use this from a terminal to confirm the correct server is answering:

        curl -s http://127.0.0.1:5001/server_info | python3 -m json.tool

    Or in the browser console:

        fetch('/server_info').then(r=>r.json()).then(d=>console.table(d))
    """
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = 'unknown'
    return jsonify({
        'server_file':    __file__,
        'server_dir':     _HERE,
        'template_folder': os.path.join(_HERE, 'templates'),
        'static_folder':   os.path.join(_HERE, 'static'),
        'pid':            os.getpid(),
        'hostname':       hostname,
        'python':         sys.executable,
        'flask_port':     request.host,
    })


@app.route('/client_log', methods=['POST'])
def client_log():
    """Receive browser console warnings/errors and persist them to a log file.

    The browser-side JS interceptor (injected into every page template) calls
    this endpoint with a JSON body like::

        { "entries": [
            { "ts": "2026-…", "level": "warn", "url": "http://…/",
              "msg": "…", "src": "file.js", "line": 42, "col": 7,
              "stack": "Error: …\\n    at …" }
        ] }

    Each entry is appended as a single JSON line to logs/browser_warnings.log
    so it appears in the Log Management page and can be viewed/archived there.
    Input is sanitised and capped so the endpoint cannot be used to flood disk.
    """
    try:
        data = request.get_json(silent=True) or {}
        entries = data.get('entries', [])
        if not isinstance(entries, list):
            return '', 400

        os.makedirs(os.path.dirname(BROWSER_WARN_LOG), exist_ok=True)

        with open(BROWSER_WARN_LOG, 'a', encoding='utf-8') as f:
            for entry in entries[:10]:          # accept at most 10 per POST
                if not isinstance(entry, dict):
                    continue
                # Sanitise: keep only known fields and truncate long strings.
                record = {
                    'ts':    str(entry.get('ts',    ''))[:32],
                    'level': str(entry.get('level', 'warn'))[:10],
                    'url':   str(entry.get('url',   ''))[:300],
                    'msg':   str(entry.get('msg',   ''))[:2000],
                }
                for field in ('src', 'line', 'col', 'stack'):
                    if field in entry:
                        record[field] = str(entry[field])[:500]
                f.write(json.dumps(record) + '\n')

        return '', 204
    except Exception as e:
        print(f'[LOG] client_log error: {e}')
        return '', 500


# --- Chart routes and data endpoint ---------------------------------------
@app.route('/chart_plotly')
def chart_plotly_index():
    colors = list(tilt_cfg.keys())
    if colors:
        return redirect(f'/chart_plotly/{colors[0]}')
    return render_template('chart_plotly.html', tilt_color=None, system_settings=system_cfg)


@app.route('/chart_plotly/<tilt_color>')
def chart_plotly_for(tilt_color):
    # Allow "TempControl" as a special identifier for temperature control.
    # Also allow composite keys ("Color:MAC") — resolve to the base color for config lookup.
    if tilt_color and tilt_color != "TempControl" and tilt_key_base(tilt_color) not in tilt_cfg:
        abort(404)
    return render_template(
        'chart_plotly.html',
        tilt_color=tilt_color,
        tilt_cfg=tilt_cfg,
        system_settings=system_cfg
    )

@app.route('/chart_data/<tilt_color>')
def chart_data_for(tilt_color):
    """
    Retrieve chart data for a specific tilt color or temperature control.
    
    Args:
        tilt_color: Tilt color name (e.g., 'Black', 'Blue') or 'Fermenter' for temperature control
        
    Query Parameters:
        all: If '1', 'true', 'yes', or 'on', return all available data
        limit: Maximum number of data points to return (default: DEFAULT_CHART_LIMIT)
        
    Returns:
        JSON object with:
            - points: Array of data points with timestamp, temp_f, and gravity (for tilts) or event (for temp control)
            - truncated: Boolean indicating if data was truncated due to limit
            - matched: Total number of matching entries found
            
    Data Sources:
        - Tilt colors: Read from batch-specific JSONL files in batches/ directory
        - 'TempControl': Read from temp_control_log.jsonl
        
    Supported Formats:
        - Event/payload format: {"event": "sample", "payload": {...}}
        - Legacy direct object format: {"timestamp": ..., "gravity": ..., "temp_f": ...}
    """
    all_flag = str(request.args.get('all', '')).lower() in ('1', 'true', 'yes', 'on')
    limit_param = request.args.get('limit', None)
    limit = None
    if limit_param:
        try:
            limit = int(limit_param)
        except Exception:
            limit = None
    if not all_flag and (limit is None or limit <= 0):
        limit = DEFAULT_CHART_LIMIT
    if not all_flag and limit is not None:
        limit = max(10, min(limit, MAX_CHART_LIMIT))

    # Handle "TempControl" as temperature control monitor data
    if tilt_color == "TempControl":
        # Collect all data points (file events + in-memory readings)
        all_points = []
        matched = 0
        
        # First, read event-based entries from all per-color log files
        for _log_path in _list_all_control_log_files():
            if not os.path.exists(_log_path):
                continue
            try:
                with open(_log_path, 'r') as f:
                    for line in f:
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        # Include all temp control events (but not TEMP CONTROL READING from old logs)
                        event = obj.get('event', '')
                        if event not in ALLOWED_EVENT_VALUES:
                            continue
                        # Skip old TEMP CONTROL READING entries from file if they exist
                        # We'll use in-memory readings instead
                        if event == "TEMP CONTROL READING":
                            continue
                        # Skip SAMPLE (tilt_reading) events - they're logged at tilt_logging_interval_minutes (15 min)
                        # We only want periodic readings from temp_reading_buffer (logged at update_interval)
                        if event == "SAMPLE":
                            continue
                        
                        matched += 1
                        ts = obj.get('timestamp')
                        tf = obj.get('temp_f') if obj.get('temp_f') is not None else obj.get('current_temp')
                        g = obj.get('gravity')
                        
                        try:
                            ts_str = str(ts) if ts is not None else None
                        except Exception:
                            ts_str = None
                        try:
                            temp_num = float(tf) if (tf is not None and tf != '') else None
                            # Filter out 999 readings (battery/connection issues)
                            if temp_num == 999:
                                temp_num = None
                        except Exception:
                            temp_num = None
                        try:
                            grav_num = float(g) if (g is not None and g != '') else None
                        except Exception:
                            grav_num = None
                        
                        entry = {
                            "timestamp": ts_str, 
                            "temp_f": temp_num, 
                            "gravity": grav_num, 
                            "event": event, 
                            "tilt_color": obj.get('tilt_color', ''),
                            "low_limit": obj.get('low_limit'),
                            "high_limit": obj.get('high_limit')
                        }
                        all_points.append(entry)
            except Exception as e:
                print(f"[LOG] Error reading temp control log {_log_path} for chart_data: {e}")
        
        # Add in-memory periodic readings
        for reading in temp_reading_buffer:
            matched += 1
            all_points.append(reading)
        
        # Sort all points by timestamp
        try:
            all_points.sort(key=lambda x: x.get('timestamp', ''))
        except Exception:
            pass  # If sorting fails, just use unsorted
        
        # Apply limit if needed
        if not all_flag and limit is not None:
            # Take the most recent entries
            if len(all_points) > limit:
                truncated = True
                all_points = all_points[-limit:]
            else:
                truncated = False
        else:
            truncated = False
            # For 'all' requests, still enforce MAX_ALL_LIMIT
            if len(all_points) > MAX_ALL_LIMIT:
                truncated = True
                all_points = all_points[-MAX_ALL_LIMIT:]
        
        return jsonify({"tilt_color": tilt_color, "points": all_points, "truncated": truncated, "matched": matched})

    # Original tilt color logic
    # Support composite keys ("Color:MAC") by resolving to the base color for tilt_cfg lookups.
    _tc_base = tilt_key_base(tilt_color)
    if tilt_color and _tc_base not in tilt_cfg:
        return jsonify({"tilt_color": tilt_color, "points": [], "truncated": False, "matched": 0})

    brewid = tilt_cfg.get(_tc_base, {}).get('brewid')
    
    # Sanitize brewid to prevent directory traversal attacks
    # Only allow alphanumeric characters, hyphens, and underscores
    import re
    if brewid and not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
        print(f"[LOG] Invalid brewid format for {tilt_color}: {brewid}")
        return jsonify({"tilt_color": tilt_color, "points": [], "truncated": False, "matched": 0})
    
    points = deque(maxlen=limit) if (not all_flag and limit is not None) else []
    matched = 0
    
    # Find batch file(s) for this brewid
    batch_files = []
    if brewid:
        batch_files = glob_func(f'batches/*{brewid}*.jsonl')
        # Sort batch files by name for consistent ordering
        batch_files.sort()
    
    if batch_files:
        # Read from batch file(s)
        for batch_file in batch_files:
            try:
                with open(batch_file, 'r') as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        
                        # Handle both event/payload format and direct object format
                        # New format: {"event": "sample", "payload": {...}}
                        # Legacy format: {"timestamp": ..., "gravity": ..., "temp_f": ...}
                        if obj.get('event') == 'sample':
                            payload = obj.get('payload', {})
                        elif obj.get('event') == 'batch_metadata':
                            # Skip metadata entries
                            continue
                        elif 'timestamp' in obj or 'gravity' in obj or 'temp_f' in obj:
                            # Legacy format - direct object
                            payload = obj
                        else:
                            # Unknown format, skip
                            continue
                        
                        matched += 1
                        ts = payload.get('timestamp')
                        tf = payload.get('temp_f') if payload.get('temp_f') is not None else payload.get('current_temp')
                        g = payload.get('gravity')
                        
                        try:
                            ts_str = str(ts) if ts is not None else None
                        except Exception:
                            ts_str = None
                        try:
                            temp_num = float(tf) if (tf is not None and tf != '') else None
                            # Filter out 999 readings (battery/connection issues)
                            if temp_num == 999:
                                temp_num = None
                        except Exception:
                            temp_num = None
                        try:
                            grav_num = float(g) if (g is not None and g != '') else None
                        except Exception:
                            grav_num = None
                        
                        entry = {"timestamp": ts_str, "temp_f": temp_num, "gravity": grav_num}
                        if isinstance(points, deque):
                            points.append(entry)
                        else:
                            points.append(entry)
                            if len(points) > MAX_ALL_LIMIT:
                                points.pop(0)
            except Exception as e:
                print(f"[LOG] Error reading batch file {batch_file} for chart_data: {e}")
    
    if isinstance(points, deque):
        pts = list(points)
        truncated = (matched > len(pts))
    else:
        pts = list(points)
        truncated = (matched > len(pts))
    return jsonify({"tilt_color": tilt_color, "points": pts, "truncated": truncated, "matched": matched})


# --- Reset logs endpoint ---------------------------------------------------
@app.route('/reset_logs', methods=['POST'])
def reset_logs():
    """
    Reset (clear) all per-color temp control log files after backing them up.
    """
    try:
        ts_suffix = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        for log_path in _list_all_control_log_files():
            if os.path.exists(log_path):
                backup_name = f"{log_path}.{ts_suffix}.bak"
                try:
                    os.rename(log_path, backup_name)
                except Exception as e:
                    print(f"[LOG] Could not backup log {log_path}: {e}")
        # Log a mode-changed marker for every configured controller so the
        # new logs start with accurate state entries (multi-controller aware).
        for ctrl in temp_cfg.get('controllers', []):
            if ctrl.get('enable_heating') or ctrl.get('enable_cooling'):
                append_control_log("temp_control_mode_changed", {
                    "controller_id": ctrl.get("controller_id", 0),
                    "low_limit": ctrl.get("low_limit"),
                    "current_temp": ctrl.get("current_temp"),
                    "high_limit": ctrl.get("high_limit"),
                    "tilt_color": ctrl.get("tilt_color", "")
                })
        return redirect('/temp_config')
    except Exception as e:
        print(f"[LOG] reset_logs error: {e}")
        return "Error resetting logs", 500


# --- Misc UI routes -------------------------------------------------------
# Note: /export_temp_csv is already defined at line 3798


# --- Log Management Routes ------------------------------------------------
@app.route('/log_management')
def log_management():
    """Display the log and data management page."""
    try:
        # Get per-color temp control log files
        temp_logs = []
        for log_path in _list_all_control_log_files():
            fname = os.path.basename(log_path)
            size_bytes = os.path.getsize(log_path)
            # Derive a human-readable color label from the filename.
            # "temp_control_log_orange.jsonl" → "Orange"
            # "temp_control_log.jsonl"        → "General"
            m = re.match(r'^temp_control_log_([a-z_]+)\.jsonl$', fname)
            label = m.group(1).replace('_', ' ').title() if m else 'General'
            temp_logs.append({
                'filename': fname,
                'label': label,
                'size': _format_file_size(size_bytes),
            })
        
        # Get Kasa activity log info
        kasa_log_size = "0 bytes"
        kasa_log_path = 'logs/kasa_activity_monitoring.jsonl'
        if os.path.exists(kasa_log_path):
            size_bytes = os.path.getsize(kasa_log_path)
            kasa_log_size = _format_file_size(size_bytes)
        
        # Get notifications log info
        notifications_log_size = "0 bytes"
        notifications_log_path = 'logs/notifications_log.jsonl'
        if os.path.exists(notifications_log_path):
            size_bytes = os.path.getsize(notifications_log_path)
            notifications_log_size = _format_file_size(size_bytes)
        
        # Get application logs
        app_logs = []
        log_dir = 'logs'
        if os.path.exists(log_dir):
            for filename in os.listdir(log_dir):
                if filename.endswith('.log') and filename != '.gitkeep':
                    filepath = os.path.join(log_dir, filename)
                    size_bytes = os.path.getsize(filepath)
                    app_logs.append({
                        'name': filename,
                        'size': _format_file_size(size_bytes),
                        'path': filepath
                    })
        
        # Get batch files
        batches = []
        if os.path.exists(BATCHES_DIR):
            for filename in os.listdir(BATCHES_DIR):
                if filename.endswith('.jsonl') and not filename.endswith('.backup'):
                    filepath = os.path.join(BATCHES_DIR, filename)
                    size_bytes = os.path.getsize(filepath)
                    
                    # Extract brewid from filename (format: {beer_name}_{YYYYMMDD}_{brewid}.jsonl)
                    # The brewid is the last underscore-separated token; for legacy files
                    # with no underscores (e.g. {brewid}.jsonl) the whole name is the brewid.
                    name_without_ext = filename.replace('.jsonl', '')
                    brewid = name_without_ext.rsplit('_', 1)[-1]
                    
                    # Try to get batch info from tilt_cfg
                    beer_name = None
                    batch_name = None
                    ferm_start_date = None
                    for color, cfg in tilt_cfg.items():
                        if cfg.get('brewid') == brewid:
                            beer_name = cfg.get('beer_name')
                            batch_name = cfg.get('batch_name')
                            ferm_start_date = cfg.get('ferm_start_date')
                            break
                    
                    # Fall back to reading from the JSONL file for historical batches
                    if not beer_name or not batch_name:
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        obj = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue
                                    if obj.get('event') == 'batch_metadata':
                                        payload = obj.get('payload', {})
                                        meta = payload.get('meta', {})
                                        if not beer_name:
                                            beer_name = payload.get('beer_name') or meta.get('beer_name')
                                        if not batch_name:
                                            batch_name = payload.get('batch_name') or meta.get('batch_name')
                                        if not ferm_start_date:
                                            ferm_start_date = payload.get('ferm_start_date') or meta.get('ferm_start_date')
                                        if beer_name and batch_name:
                                            break
                                    elif obj.get('event') == 'sample':
                                        payload = obj.get('payload', {})
                                        if not beer_name:
                                            beer_name = payload.get('beer_name')
                                        if not batch_name:
                                            batch_name = payload.get('batch_name')
                                        break
                        except Exception as e:
                            print(f"[LOG] Could not read batch metadata from {filepath}: {e}")

                    batches.append({
                        'filename': filename,
                        'brewid': brewid,
                        'beer_name': beer_name,
                        'batch_name': batch_name,
                        'ferm_start_date': ferm_start_date,
                        'size': _format_file_size(size_bytes)
                    })
        
        # Sort batches by fermentation start date (most recent first)
        # Batches without a date or invalid date format appear at the end
        def sort_key(batch):
            date_str = batch.get('ferm_start_date')
            if date_str:
                try:
                    # Parse date string (format: YYYY-MM-DD)
                    return datetime.strptime(date_str, '%Y-%m-%d')
                except (ValueError, TypeError) as e:
                    # Log invalid date format for debugging
                    print(f"[LOG] Invalid date format for batch {batch.get('brewid')}: {date_str}")
            # Return a very old date for batches without dates or invalid dates
            return datetime(1900, 1, 1)
        
        batches.sort(key=sort_key, reverse=True)
        
        return render_template('log_management.html',
                             temp_logs=temp_logs,
                             kasa_log_size=kasa_log_size,
                             notifications_log_size=notifications_log_size,
                             app_logs=app_logs,
                             batches=batches,
                             success_message=request.args.get('success'),
                             error_message=request.args.get('error'))
    except Exception as e:
        print(f"[LOG] Error in log_management: {e}")
        return "Error loading log management page", 500


def _format_file_size(size_bytes):
    """Format file size in human-readable format."""
    for unit in ['bytes', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


@app.route('/view_log')
def view_log():
    """Display the content of a log file with pagination."""
    try:
        log_file = request.args.get('file')
        log_type = request.args.get('type', 'app')  # 'app', 'temp', 'kasa', 'temp_tilt', or 'notifications'
        page = request.args.get('page', 1, type=int)
        lines_per_page = 50  # Show 50 lines per page
        
        if not log_file:
            return "No log file specified", 400
        
        # Security: validate log file path
        if log_type == 'temp':
            # Allow any per-color temp control log (temp_control_log[_color].jsonl)
            log_file = os.path.basename(log_file)
            if not _TEMP_CONTROL_LOG_RE.match(log_file):
                return "Invalid log file", 400
            filepath = os.path.join(TEMP_CONTROL_DIR, log_file)
        elif log_type == 'kasa':
            # Kasa activity log
            if log_file != 'kasa_activity_monitoring.jsonl':
                return "Invalid log file", 400
            filepath = 'logs/kasa_activity_monitoring.jsonl'
        elif log_type == 'notifications':
            # Notifications log
            if log_file != 'notifications_log.jsonl':
                return "Invalid log file", 400
            filepath = 'logs/notifications_log.jsonl'
        else:
            # Application log - restrict to alphanumeric, dash, underscore, single dot before .log
            if not re.match(r'^[a-zA-Z0-9\-_]+\.log$', log_file):
                return "Invalid log file name", 400
            filepath = os.path.join('logs', log_file)
        
        if not os.path.exists(filepath):
            return f"Log file not found: {log_file}", 404
        
        # For large files, use a memory-efficient approach
        # First, count total lines efficiently
        try:
            with open(filepath, 'rb') as f:
                total_lines = sum(1 for _ in f)
        except Exception as e:
            return f"Error reading log file: {str(e)}", 500
        
        total_pages = max(1, (total_lines + lines_per_page - 1) // lines_per_page)
        
        # Validate page number
        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages
        
        # Calculate pagination indices (show most recent first, so reverse indexing)
        # Most recent lines are at the end of the file
        start_idx = total_lines - (page * lines_per_page)
        end_idx = total_lines - ((page - 1) * lines_per_page)
        
        if start_idx < 0:
            start_idx = 0
        
        # Read only the lines we need using itertools.islice for efficiency
        # This is memory-efficient for large files - skips directly to start_idx
        page_lines = []  # Initialize to ensure it's always defined
        content = ""
        
        try:
            lines_to_read = end_idx - start_idx
            
            # Handle edge case where there are no lines to read
            if lines_to_read > 0:
                with open(filepath, 'r') as f:
                    # Use islice to skip directly to start_idx and read only needed lines
                    page_lines = list(reversed(list(itertools.islice(f, start_idx, end_idx))))
                content = ''.join(page_lines)
        except Exception as e:
            return f"Error reading log file: {str(e)}", 500
        
        return render_template('view_log.html',
                             log_file=log_file,
                             log_type=log_type,
                             content=content,
                             line_count=len(page_lines),
                             current_page=page,
                             total_pages=total_pages,
                             total_lines=total_lines)
    except Exception as e:
        print(f"[LOG] Error viewing log: {e}")
        return f"Error viewing log: {str(e)}", 500


@app.route('/archive_temp_log', methods=['POST'])
def archive_temp_log():
    """Archive and reset a per-color temperature control log file."""
    try:
        log_file = os.path.basename(request.form.get('log_file', '').strip())
        if not log_file:
            return redirect(url_for('log_management', error='No log file specified'))
        # Security: allow only valid per-color log filenames
        if not _TEMP_CONTROL_LOG_RE.match(log_file):
            return redirect(url_for('log_management', error='Invalid log file name'))

        log_path = os.path.join(TEMP_CONTROL_DIR, log_file)
        if not os.path.exists(log_path):
            return redirect(url_for('log_management', error=f'Log file not found: {log_file}'))

        # Create backup with timestamp
        backup_name = f"{log_path}.{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.bak"
        shutil.copy2(log_path, backup_name)
        print(f"[LOG] Archived temp control log to: {backup_name}")

        # Reset the log file
        open(log_path, 'w').close()

        return redirect(url_for('log_management', success=f'{log_file} archived and reset'))
    except Exception as e:
        print(f"[LOG] Error archiving temp log: {e}")
        return redirect(url_for('log_management', error=f'Error archiving log: {str(e)}'))

@app.route('/archive_log', methods=['POST'])
def archive_log():
    """Archive an application log file."""
    try:
        log_file = request.form.get('log_file')
        if not log_file:
            return redirect(url_for('log_management', error='No log file specified'))
        
        # Security: ensure log_file is just a filename, not a path
        if '/' in log_file or '\\' in log_file:
            return redirect(url_for('log_management', error='Invalid log file name'))
        
        log_path = os.path.join('logs', log_file)
        if not os.path.exists(log_path):
            return redirect(url_for('log_management', error=f'Log file not found: {log_file}'))
        
        # Create archive filename with timestamp
        archive_name = f"{log_path}.{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.archive"
        shutil.move(log_path, archive_name)
        
        # Create empty file to replace it
        open(log_path, 'w').close()
        
        print(f"[LOG] Archived {log_file} to {archive_name}")
        return redirect(url_for('log_management', success=f'Log file {log_file} archived'))
    except Exception as e:
        print(f"[LOG] Error archiving log: {e}")
        return redirect(url_for('log_management', error=f'Error archiving log: {str(e)}'))


@app.route('/delete_log', methods=['POST'])
def delete_log():
    """Delete an application log file."""
    try:
        log_file = request.form.get('log_file')
        if not log_file:
            return redirect(url_for('log_management', error='No log file specified'))
        
        # Security: ensure log_file is just a filename, not a path
        if '/' in log_file or '\\' in log_file:
            return redirect(url_for('log_management', error='Invalid log file name'))
        
        log_path = os.path.join('logs', log_file)
        if not os.path.exists(log_path):
            return redirect(url_for('log_management', error=f'Log file not found: {log_file}'))
        
        os.remove(log_path)
        print(f"[LOG] Deleted log file: {log_file}")
        return redirect(url_for('log_management', success=f'Log file {log_file} deleted'))
    except Exception as e:
        print(f"[LOG] Error deleting log: {e}")
        return redirect(url_for('log_management', error=f'Error deleting log: {str(e)}'))


@app.route('/archive_kasa_log', methods=['POST'])
def archive_kasa_log():
    """Archive and reset the Kasa activity log."""
    try:
        kasa_log_path = 'logs/kasa_activity_monitoring.jsonl'
        if os.path.exists(kasa_log_path):
            # Create backup with timestamp
            backup_name = f"{kasa_log_path}.{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.bak"
            shutil.copy2(kasa_log_path, backup_name)
            print(f"[LOG] Archived Kasa activity log to: {backup_name}")
            
            # Reset the log file
            open(kasa_log_path, 'w').close()
            
            return redirect(url_for('log_management', success='Kasa activity log archived and reset'))
        else:
            return redirect(url_for('log_management', error='Kasa activity log not found'))
    except Exception as e:
        print(f"[LOG] Error archiving Kasa log: {e}")
        return redirect(url_for('log_management', error=f'Error archiving log: {str(e)}'))




@app.route('/archive_notifications_log', methods=['POST'])
def archive_notifications_log():
    """Archive and reset the notifications log."""
    try:
        notifications_log_path = 'logs/notifications_log.jsonl'
        if os.path.exists(notifications_log_path):
            # Create backup with timestamp
            backup_name = f"{notifications_log_path}.{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.bak"
            shutil.copy2(notifications_log_path, backup_name)
            print(f"[LOG] Archived notifications log to: {backup_name}")
            
            # Reset the log file
            open(notifications_log_path, 'w').close()
            
            return redirect(url_for('log_management', success='Notifications log archived and reset'))
        else:
            return redirect(url_for('log_management', error='Notifications log not found'))
    except Exception as e:
        print(f"[LOG] Error archiving notifications log: {e}")
        return redirect(url_for('log_management', error=f'Error archiving log: {str(e)}'))


@app.route('/export_batch_csv', methods=['POST'])
def export_batch_csv():
    """Export a batch's data to CSV."""
    try:
        import csv
        
        brewid = request.form.get('brewid')
        if not brewid:
            return redirect(url_for('log_management', error='No batch ID specified'))
        
        # Security: validate brewid format
        import re
        if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
            return redirect(url_for('log_management', error='Invalid batch ID format'))
        
        # Find batch file
        batch_file = os.path.join(BATCHES_DIR, f'{brewid}.jsonl')
        if not os.path.exists(batch_file):
            # Try legacy format
            legacy_files = glob_func(f'{BATCHES_DIR}/*{brewid}*.jsonl')
            if legacy_files:
                batch_file = legacy_files[0]
            else:
                return redirect(url_for('log_management', error=f'Batch file not found for ID: {brewid}'))
        
        # Create export directory
        export_dir = 'export'
        os.makedirs(export_dir, exist_ok=True)
        
        # Generate filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_filename = f'batch_{brewid}_{timestamp}.csv'
        csv_path = os.path.join(export_dir, csv_filename)
        
        # Read batch data
        data_rows = []
        with open(batch_file, 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    # Handle both event/payload format and direct object format
                    if obj.get('event') == 'sample':
                        payload = obj.get('payload', {})
                        data_rows.append(payload)
                    elif obj.get('event') == 'batch_metadata':
                        # Skip metadata
                        continue
                    elif 'timestamp' in obj or 'gravity' in obj or 'temp_f' in obj:
                        # Legacy direct format
                        data_rows.append(obj)
                except Exception as e:
                    print(f"[LOG] Error parsing line in batch export: {e}")
                    continue
        
        # Write CSV
        if data_rows:
            with open(csv_path, 'w', newline='') as csvfile:
                # Define fieldnames
                fieldnames = ['timestamp', 'temp_f', 'gravity', 'color', 'brewid', 'beer_name', 'batch_name']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                for row in data_rows:
                    writer.writerow(row)
            
            return redirect(url_for('log_management', success=f'Batch exported to {csv_filename} ({len(data_rows)} records)'))
        else:
            return redirect(url_for('log_management', error='No data found in batch file'))
    except Exception as e:
        print(f"[LOG] Error exporting batch CSV: {e}")
        return redirect(url_for('log_management', error=f'Error exporting batch: {str(e)}'))


@app.route('/archive_batch', methods=['POST'])
def archive_batch():
    """Archive a batch file."""
    try:
        brewid = request.form.get('brewid')
        if not brewid:
            return redirect(url_for('log_management', error='No batch ID specified'))
        
        # Security: validate brewid format
        import re
        if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
            return redirect(url_for('log_management', error='Invalid batch ID format'))
        
        # Find batch file
        batch_file = os.path.join(BATCHES_DIR, f'{brewid}.jsonl')
        if not os.path.exists(batch_file):
            # Try legacy format
            legacy_files = glob_func(f'{BATCHES_DIR}/*{brewid}*.jsonl')
            if legacy_files:
                batch_file = legacy_files[0]
            else:
                return redirect(url_for('log_management', error=f'Batch file not found for ID: {brewid}'))
        
        # Create archive directory
        archive_dir = os.path.join(BATCHES_DIR, 'archive')
        os.makedirs(archive_dir, exist_ok=True)
        
        # Move file to archive with timestamp
        timestamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        filename = os.path.basename(batch_file)
        archive_filename = f"{filename}.{timestamp}.archive"
        archive_path = os.path.join(archive_dir, archive_filename)
        
        shutil.move(batch_file, archive_path)
        print(f"[LOG] Archived batch {brewid} to {archive_path}")
        
        return redirect(url_for('log_management', success=f'Batch {brewid} archived'))
    except Exception as e:
        print(f"[LOG] Error archiving batch: {e}")
        return redirect(url_for('log_management', error=f'Error archiving batch: {str(e)}'))


@app.route('/close_batch', methods=['POST'])
def close_batch():
    """
    Close a batch by marking it as inactive in batch_history.
    This moves the batch from Current Activity to Batch History section.
    """
    try:
        brewid = request.form.get('brewid')
        color = request.form.get('color')
        
        if not brewid or not color:
            return jsonify({'success': False, 'error': 'Missing brewid or color'}), 400
        
        # Security: validate brewid format
        import re
        if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
            return jsonify({'success': False, 'error': 'Invalid batch ID format'}), 400
        
        # Load batch history for this color
        batch_history_file = f'batches/batch_history_{color}.json'
        batches = []
        if os.path.exists(batch_history_file):
            try:
                with open(batch_history_file, 'r') as f:
                    batches = json.load(f)
            except Exception as e:
                return jsonify({'success': False, 'error': f'Error reading batch history: {e}'}), 500
        
        # Find and update all batches with this brewid
        # (handles duplicates that may have been created before the fix)
        batch_found = False
        for batch in batches:
            if batch.get('brewid') == brewid:
                batch['is_active'] = False
                batch['closed_date'] = datetime.utcnow().strftime('%Y-%m-%d')
                batch_found = True
                # Continue to close ALL matching batches (don't break)
        
        if not batch_found:
            return jsonify({'success': False, 'error': 'Batch not found in history'}), 404
        
        # Save updated batch history
        try:
            with open(batch_history_file, 'w') as f:
                json.dump(batches, f, indent=2)
        except Exception as e:
            return jsonify({'success': False, 'error': f'Error saving batch history: {e}'}), 500
        
        # Clear this batch from tilt_cfg if it's currently assigned
        if color in tilt_cfg and tilt_cfg[color].get('brewid') == brewid:
            tilt_cfg[color] = {
                "beer_name": "",
                "batch_name": "",
                "ferm_start_date": "",
                "recipe_og": "",
                "recipe_fg": "",
                "recipe_abv": "",
                "actual_og": None,
                "brewid": "",
                "og_confirmed": False,
                "is_active": True,
                "closed_date": None,
                "notification_state": {
                    "fermentation_start_datetime": None,
                    "fermentation_completion_datetime": None,
                    "last_daily_report": None
                }
            }
            try:
                save_json(TILT_CONFIG_FILE, tilt_cfg)
            except Exception as e:
                print(f"[LOG] Error clearing tilt config: {e}")
        
        return jsonify({'success': True, 'message': f'Batch {brewid} closed successfully'})
    except Exception as e:
        print(f"[LOG] Error closing batch: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/reopen_batch', methods=['POST'])
def reopen_batch():
    """
    Reopen a closed batch by marking it as active again.
    This moves the batch from Batch History to Current Activity section.
    """
    try:
        brewid = request.form.get('brewid')
        color = request.form.get('color')
        
        if not brewid or not color:
            return jsonify({'success': False, 'error': 'Missing brewid or color'}), 400
        
        # Security: validate brewid format
        import re
        if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
            return jsonify({'success': False, 'error': 'Invalid batch ID format'}), 400
        
        # Load batch history for this color
        batch_history_file = f'batches/batch_history_{color}.json'
        batches = []
        if os.path.exists(batch_history_file):
            try:
                with open(batch_history_file, 'r') as f:
                    batches = json.load(f)
            except Exception as e:
                return jsonify({'success': False, 'error': f'Error reading batch history: {e}'}), 500
        
        # Find and update all batches with this brewid
        # (handles duplicates that may have been created before the fix)
        batch_found = False
        for batch in batches:
            if batch.get('brewid') == brewid:
                batch['is_active'] = True
                batch['closed_date'] = None
                batch_found = True
                # Continue to reopen ALL matching batches (don't break)
        
        if not batch_found:
            return jsonify({'success': False, 'error': 'Batch not found in history'}), 404
        
        # Save updated batch history
        try:
            with open(batch_history_file, 'w') as f:
                json.dump(batches, f, indent=2)
        except Exception as e:
            return jsonify({'success': False, 'error': f'Error saving batch history: {e}'}), 500
        
        return jsonify({'success': True, 'message': f'Batch {brewid} reopened successfully'})
    except Exception as e:
        print(f"[LOG] Error reopening batch: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/cleanup_batch_duplicates', methods=['POST'])
def cleanup_batch_duplicates():
    """
    Remove duplicate batch entries from batch_history files.
    Keeps only the most recent entry for each brewid.
    This is a cleanup utility for batches that were duplicated before the fix.
    """
    try:
        cleaned_colors = []
        total_duplicates_removed = 0
        
        # Check each color for batch history
        for color in TILT_UUIDS.values():
            batch_history_file = f'batches/batch_history_{color}.json'
            if not os.path.exists(batch_history_file):
                continue
            
            try:
                with open(batch_history_file, 'r') as f:
                    batches = json.load(f)
            except Exception as e:
                print(f"[LOG] Error reading batch history for {color}: {e}")
                continue
            
            if not batches:
                continue
            
            # Group batches by brewid, keeping only the last occurrence
            # Note: Later occurrences intentionally overwrite earlier ones in the dict
            # to keep the most recent version of each batch (handles edits).
            # This assumes batches are in chronological order in the list (append-only history).
            unique_batches = {}
            for batch in batches:
                brewid = batch.get('brewid')
                if brewid:
                    # Store the batch (later occurrences will overwrite earlier ones)
                    unique_batches[brewid] = batch
            
            # Count duplicates removed
            duplicates_count = len(batches) - len(unique_batches)
            if duplicates_count > 0:
                total_duplicates_removed += duplicates_count
                cleaned_colors.append(f"{color} ({duplicates_count} duplicates)")
                
                # Save deduplicated batches
                try:
                    with open(batch_history_file, 'w') as f:
                        json.dump(list(unique_batches.values()), f, indent=2)
                    print(f"[LOG] Removed {duplicates_count} duplicate batches from {color}")
                except Exception as e:
                    print(f"[LOG] Error saving cleaned batch history for {color}: {e}")
        
        if total_duplicates_removed > 0:
            message = f"Cleanup complete: Removed {total_duplicates_removed} duplicate batch entries from {', '.join(cleaned_colors)}"
            return jsonify({'success': True, 'message': message, 'duplicates_removed': total_duplicates_removed})
        else:
            return jsonify({'success': True, 'message': 'No duplicate batch entries found', 'duplicates_removed': 0})
    
    except Exception as e:
        print(f"[LOG] Error during batch cleanup: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/delete_batch', methods=['POST'])
def delete_batch():
    """Delete a batch file."""
    try:
        brewid = request.form.get('brewid')
        if not brewid:
            return redirect(url_for('log_management', error='No batch ID specified'))
        
        # Security: validate brewid format
        import re
        if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
            return redirect(url_for('log_management', error='Invalid batch ID format'))
        
        # Find batch file
        batch_file = os.path.join(BATCHES_DIR, f'{brewid}.jsonl')
        if not os.path.exists(batch_file):
            # Try legacy format
            legacy_files = glob_func(f'{BATCHES_DIR}/*{brewid}*.jsonl')
            if legacy_files:
                batch_file = legacy_files[0]
            else:
                return redirect(url_for('log_management', error=f'Batch file not found for ID: {brewid}'))
        
        os.remove(batch_file)
        print(f"[LOG] Deleted batch file: {brewid}")
        
        return redirect(url_for('log_management', success=f'Batch {brewid} deleted'))
    except Exception as e:
        print(f"[LOG] Error deleting batch: {e}")
        return redirect(url_for('log_management', error=f'Error deleting batch: {str(e)}'))


@app.route('/backup_system', methods=['POST'])
def backup_system():
    """Create a backup of all system files to the specified USB device."""
    import tarfile
    import shutil
    
    backup_path = request.form.get('backup_path', '/media/usb')
    
    # Validate that the backup path exists
    if not os.path.exists(backup_path):
        return jsonify({
            'success': False,
            'message': f'Backup path does not exist: {backup_path}. Please ensure USB device is mounted.'
        }), 400
    
    # Check if the path is writable
    if not os.access(backup_path, os.W_OK):
        return jsonify({
            'success': False,
            'message': f'Backup path is not writable: {backup_path}. Check permissions.'
        }), 400
    
    try:
        # Create timestamped backup filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f'fermenter_backup_{timestamp}.tar.gz'
        backup_full_path = os.path.join(backup_path, backup_filename)
        
        # Files and directories to backup
        items_to_backup = [
            # Python program files
            'app.py',
            'kasa_worker.py',
            'kasa_manager',
            'logger.py',
            'batch_history.py',
            'batch_storage.py',
            'fermentation_monitor.py',
            'tilt_static.py',
            'archive_compact_logs.py',
            'backfill_temp_control_jsonl.py',
            # Configuration files
            'config/',
            # Data files
            'batches/',
            'temp_control/',
            'temp_control_log.jsonl',
            # Web interface
            'templates/',
            'static/',
            # Documentation
            'requirements.txt',
            'start.sh',
            'README.md',
        ]
        
        # Create the tar.gz archive
        with tarfile.open(backup_full_path, 'w:gz') as tar:
            for item in items_to_backup:
                if os.path.exists(item):
                    tar.add(item)
        
        # Get the size of the backup file
        backup_size = os.path.getsize(backup_full_path)
        backup_size_mb = backup_size / (1024 * 1024)
        
        return jsonify({
            'success': True,
            'message': f'Backup created successfully: {backup_filename}',
            'filename': backup_filename,
            'size_mb': f'{backup_size_mb:.2f}',
            'path': backup_full_path
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Backup failed: {str(e)}'
        }), 500


@app.route('/restore_system', methods=['POST'])
def restore_system():
    """Restore system from a backup file."""
    import tarfile
    import shutil
    
    backup_path = request.form.get('backup_path', '/media/usb')
    backup_filename = request.form.get('backup_filename', '')
    
    if not backup_filename:
        return jsonify({
            'success': False,
            'message': 'No backup file specified.'
        }), 400
    
    # Security: Validate filename to prevent directory traversal
    if '..' in backup_filename or '/' in backup_filename or '\\' in backup_filename:
        return jsonify({
            'success': False,
            'message': 'Invalid backup filename.'
        }), 400
    
    # Security: Ensure filename has expected format
    if not backup_filename.startswith('fermenter_backup_') or not backup_filename.endswith('.tar.gz'):
        return jsonify({
            'success': False,
            'message': 'Invalid backup file format.'
        }), 400
    
    backup_full_path = os.path.join(backup_path, backup_filename)
    
    # Validate that the backup file exists
    if not os.path.exists(backup_full_path):
        return jsonify({
            'success': False,
            'message': f'Backup file does not exist: {backup_full_path}'
        }), 400
    
    try:
        # Create a secure temporary directory for extraction validation
        import tempfile
        temp_dir = tempfile.mkdtemp(prefix='fermenter_restore_')
        
        try:
            # Extract the backup to temporary directory first
            with tarfile.open(backup_full_path, 'r:gz') as tar:
                # Security check: ensure no absolute paths or parent directory references
                safe_members = []
                for member in tar.getmembers():
                    if member.name.startswith('/') or '..' in member.name:
                        shutil.rmtree(temp_dir)
                        return jsonify({
                            'success': False,
                            'message': f'Invalid backup file: contains unsafe paths'
                        }), 400
                    safe_members.append(member)
                
                # Extract only validated members to temp directory
                tar.extractall(temp_dir, members=safe_members)
            
            # Now copy files from temp to current directory
            # This allows us to validate before overwriting
            current_dir = os.getcwd()
            
            for item in os.listdir(temp_dir):
                src = os.path.join(temp_dir, item)
                dst = os.path.join(current_dir, item)
                
                # Backup existing files before overwriting
                if os.path.exists(dst):
                    backup_old = f'{dst}.backup_before_restore'
                    if os.path.isdir(dst):
                        if os.path.exists(backup_old):
                            shutil.rmtree(backup_old)
                        shutil.copytree(dst, backup_old)
                    else:
                        shutil.copy2(dst, backup_old)
                
                # Copy from temp to current
                if os.path.isdir(src):
                    if os.path.exists(dst):
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            
            return jsonify({
                'success': True,
                'message': f'System restored successfully from {backup_filename}. Please restart the application for changes to take effect.',
                'restart_required': True
            })
            
        finally:
            # Always cleanup temp directory
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Restore failed: {str(e)}'
        }), 500


@app.route('/list_backups', methods=['POST'])
def list_backups():
    """List available backup files in the specified directory."""
    backup_path = request.form.get('backup_path', '/media/usb')
    
    if not os.path.exists(backup_path):
        return jsonify({
            'success': False,
            'message': f'Backup path does not exist: {backup_path}',
            'backups': []
        })
    
    try:
        # List all .tar.gz files in the backup directory
        backups = []
        if os.path.isdir(backup_path):
            for filename in os.listdir(backup_path):
                if filename.startswith('fermenter_backup_') and filename.endswith('.tar.gz'):
                    full_path = os.path.join(backup_path, filename)
                    file_stat = os.stat(full_path)
                    backups.append({
                        'filename': filename,
                        'size_mb': f'{file_stat.st_size / (1024 * 1024):.2f}',
                        'modified': datetime.fromtimestamp(file_stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                    })
        
        # Sort by filename (which includes timestamp) in reverse order
        backups.sort(key=lambda x: x['filename'], reverse=True)
        
        return jsonify({
            'success': True,
            'backups': backups,
            'path': backup_path
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Failed to list backups: {str(e)}',
            'backups': []
        })


@app.route('/update_system', methods=['POST'])
def update_system():
    """Pull the latest code from the remote git repository."""
    try:
        result = subprocess.run(
            ['git', 'pull'],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=_HERE
        )
        parts = []
        if result.stdout.strip():
            parts.append(result.stdout.strip())
        if result.stderr.strip():
            parts.append(result.stderr.strip())
        output = '\n'.join(parts)
        success = result.returncode == 0
        already_up_to_date = 'Already up to date' in output or 'Already up-to-date' in output

        return jsonify({
            'success': success,
            'output': output,
            'already_up_to_date': already_up_to_date,
            'restart_required': success and not already_up_to_date
        })

    except subprocess.TimeoutExpired:
        return jsonify({
            'success': False,
            'output': 'Update timed out after 120 seconds. Please try again or update manually.'
        }), 500

    except Exception:
        return jsonify({
            'success': False,
            'output': 'Update failed due to an unexpected error. Please update manually.'
        }), 500


@app.route('/exit_system', methods=['GET', 'POST'])
def exit_system():
    """
    Handle system exit:
    - GET: Show confirmation page
    - POST with confirm=yes: Turn off all plugs and shut down.
      Returns 204 No Content so fetch()-based callers stay on the current
      page (no navigation to goodbye.html).
    - POST with confirm=no: Return to main page
    """
    # Timing constants for shutdown sequence
    PLUG_COMMAND_DELAY = 0.5  # seconds to wait for plug command to be processed
    SHUTDOWN_DELAY = 1        # seconds to wait after response before shutting down
    PROCESS_TERMINATION_TIMEOUT = 2  # seconds to wait for process to terminate

    if request.method == 'POST':
        confirm = request.form.get('confirm', 'no')
        if confirm == 'yes':
            # Set temp_control_active to False on every controller before shutdown
            # so the monitors start OFF on next startup (multi-controller aware).
            try:
                for ctrl in temp_cfg.get('controllers', []):
                    ctrl['temp_control_active'] = False
                save_json(TEMP_CFG_FILE, temp_cfg)
            except Exception as e:
                print(f"[LOG] Error setting temp_control_active=False during shutdown - monitor may start ON at next startup: {e}")

            # Turn off all plugs across every controller before shutdown
            try:
                for ctrl in temp_cfg.get('controllers', []):
                    cid = ctrl.get('controller_id', 0)
                    heating_plug = ctrl.get("heating_plug", "")
                    cooling_plug = ctrl.get("cooling_plug", "")

                    # Turn off heating plug if configured
                    if heating_plug and kasa_manager and _is_valid_controller_id(cid):
                        kasa_manager.send(cid, 'heating', heating_plug, 'off')
                        time.sleep(PLUG_COMMAND_DELAY)

                    # Turn off cooling plug if configured
                    if cooling_plug and kasa_manager and _is_valid_controller_id(cid):
                        kasa_manager.send(cid, 'cooling', cooling_plug, 'off')
                        time.sleep(PLUG_COMMAND_DELAY)
            except Exception as e:
                print(f"[LOG] Error turning off plugs during shutdown: {e}")

            # Schedule shutdown after response is delivered
            def shutdown_system():
                time.sleep(SHUTDOWN_DELAY)
                try:
                    # Terminate the kasa_manager worker subprocess
                    if kasa_manager is not None:
                        kasa_manager.stop()
                except Exception as e:
                    print(f"[LOG] Error during kasa_manager stop: {e}")

                # Spawn a detached process that kills the browser and powers off
                # the machine after Flask has exited.  Using start_new_session=True
                # ensures the child survives the Flask process termination.
                try:
                    subprocess.Popen(
                        [
                            'sh', '-c',
                            'sleep 5'
                            ' && pkill chromium-browser 2>/dev/null || true'
                            ' && pkill chromium 2>/dev/null || true'
                            ' && pkill google-chrome 2>/dev/null || true'
                            ' ; sudo poweroff 2>/dev/null || true'
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    print("[LOG] Browser kill and poweroff scheduled")
                except Exception as e:
                    print(f"[LOG] Error scheduling browser kill / poweroff: {e}")

                # Shutdown Flask
                os.kill(os.getpid(), signal.SIGINT)

            # Start shutdown in background thread
            shutdown_thread = threading.Thread(target=shutdown_system)
            shutdown_thread.daemon = True
            shutdown_thread.start()

            # Return 204 No Content — fetch() callers stay on their current page
            return ('', 204)
        else:
            # User cancelled, return to main page
            return redirect('/')

    # GET request: show confirmation page
    return render_template('exit_system.html')


# --- Program entry ---------------------------------------------------------
def open_browser(port=5001):
    """
    Open the web browser to the Flask app URL after a short delay.
    This runs in a separate thread to avoid blocking the Flask startup.

    On Raspberry Pi / Linux, Chromium is launched in fullscreen mode so
    the display fills the screen from the moment it opens.  F11 and ESC
    remain active so the user can toggle fullscreen on and off.
    Falls back to a standard browser on platforms where Chromium is not
    available.

    Includes extra delay at boot time to ensure the desktop environment
    (display server, window manager) is ready before launching the browser.

    Args:
        port: The port number Flask is running on (default: 5001)
    """
    # Short initial pause to let Flask bind to its port before we probe it
    time.sleep(0.5)

    # Additional delay if running at boot time (helps ensure desktop is ready)
    # Check if we've been running for less than 2 minutes (likely boot scenario)
    try:
        if psutil is not None:
            process = psutil.Process(os.getpid())
            uptime = time.time() - process.create_time()
            if uptime < 120:  # Process created less than 2 minutes ago
                print("[LOG] Detected recent boot - waiting 3 seconds for desktop environment")
                time.sleep(3)
            else:
                # Manual start - shorter delay
                time.sleep(1)
        else:
            # psutil not available - add a reasonable delay for safety
            time.sleep(1)
    except (ImportError, AttributeError, OSError) as e:
        print(f"[LOG] Could not determine process uptime: {e}")
        time.sleep(1)

    url = f'http://127.0.0.1:{port}'

    # Wait for Flask to actually be responding before opening the browser.
    # This avoids a "connection refused" splash on slow hardware.
    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            urllib.request.urlopen(url, timeout=1)
            print(f"[LOG] Flask is responding, opening browser...")
            break
        except (urllib.error.URLError, OSError):
            if attempt < max_attempts - 1:
                time.sleep(1)
            else:
                print(f"[LOG] Flask not responding after {max_attempts} seconds, opening browser anyway")

    # Launch Chromium in fullscreen with no address bar or error dialogs.
    # --start-fullscreen is used instead of --kiosk so that the F11 and ESC
    # keys remain active and can toggle fullscreen on and off.
    chromium_flags = [
        '--start-fullscreen',
        '--noerrdialogs',
        '--disable-infobars',
        '--disable-session-crashed-bubble',
    ]

    try:
        # 1) Chromium on Raspberry Pi OS / most Debian-based Linux
        if shutil.which('chromium-browser'):
            subprocess.Popen(
                ['chromium-browser'] + chromium_flags + [url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[LOG] Opened browser in fullscreen mode at {url} using chromium-browser")

        # 2) Chromium under the name 'chromium' (Arch, Fedora, etc.)
        elif shutil.which('chromium'):
            subprocess.Popen(
                ['chromium'] + chromium_flags + [url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[LOG] Opened browser in fullscreen mode at {url} using chromium")

        # 3) Google Chrome on Linux or macOS
        elif shutil.which('google-chrome'):
            subprocess.Popen(
                ['google-chrome'] + chromium_flags + [url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[LOG] Opened browser in fullscreen mode at {url} using google-chrome")

        # 4) macOS — open in default browser (no kiosk equivalent via CLI)
        elif shutil.which('open'):
            subprocess.Popen(
                ['open', url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[LOG] Opened browser at {url} using open (macOS)")

        # 5) Generic Linux fallback via xdg-open
        elif shutil.which('xdg-open'):
            subprocess.Popen(
                ['nohup', 'xdg-open', url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[LOG] Opened browser at {url} using xdg-open")

        else:
            # Last resort: Python's webbrowser module
            webbrowser.open(url)
            print(f"[LOG] Opened browser at {url} using webbrowser module")

    except Exception as e:
        print(f"[LOG] Could not automatically open browser: {e}")
        print(f"[LOG] Please manually navigate to {url}")


if __name__ == '__main__':
    try:
        os.makedirs(BATCHES_DIR, exist_ok=True)
    except Exception:
        pass

    # Stop other app.py instances before starting.  This MUST run inside the
    # __main__ guard so that it is skipped when multiprocessing's 'spawn'
    # start method re-imports this module in a worker subprocess.
    try:
        stopped_info = stop_other_app_py()
    except Exception as e:
        stopped_info = {"stopped": [], "errors": []}
        print(f"[WARNING] Error during startup process cleanup: {e}")

    # Remove legacy ~/chartdata directory if it exists (no longer used)
    try:
        chartdata_path = os.path.expanduser('~/chartdata')
        abs_path = os.path.abspath(chartdata_path)
        home_dir = os.path.abspath(os.path.expanduser('~'))
        # Safety: only delete if it's a direct child of home directory
        if os.path.isdir(abs_path) and os.path.dirname(abs_path) == home_dir:
            shutil.rmtree(abs_path, ignore_errors=True)
            print(f"[LOG] Removed unused legacy directory: {abs_path}")
    except Exception as e:
        print(f"[LOG] Could not remove ~/chartdata: {e}")

    # Set multiprocessing start method to 'spawn' for clean kasa worker isolation.
    # MUST be called in if __name__ == '__main__' block before any Process() calls.
    # 'spawn' starts a fresh Python interpreter for each child process, so it does
    # not inherit the parent's open file descriptors, asyncio state, or
    # multiprocessing._children registry (which caused the SIGTERM cascade with fork).
    try:
        if multiprocessing.get_start_method(allow_none=True) is None:
            multiprocessing.set_start_method('spawn')
            print("[LOG] Set multiprocessing start method to 'spawn'")
        else:
            current = multiprocessing.get_start_method()
            print(f"[LOG] Multiprocessing start method already set to: {current}")
    except RuntimeError as e:
        print(f"[LOG] Could not set multiprocessing start method: {e}")

    # Start the KasaManager (single subprocess for all controllers/plugs).
    if kasa_manager is not None:
        try:
            if _FORCE_IOT_PORT:
                # --plug mode: force all plugs onto the legacy IOT protocol (port 9999).
                # Credentials are intentionally not passed so python-kasa uses IotPlug
                # for every device, bypassing KLAP discovery entirely.
                print("[LOG] --plug mode active: starting KasaManager without credentials (IOT/port 9999 only)")
                log_kasa_diag('info', 'KasaManager starting in --plug (IOT) mode — KLAP credentials suppressed')
                kasa_manager.start(kasa_username="", kasa_password="")
            else:
                _kasa_user = system_cfg.get('kasa_username', '').strip()
                _kasa_pass = system_cfg.get('kasa_password', '').strip()
                kasa_manager.start(kasa_username=_kasa_user, kasa_password=_kasa_pass)
            print("[LOG] KasaManager started")
            log_kasa_diag('info', 'KasaManager worker subprocess started',
                          pid=kasa_manager.worker_pid)
            # Poll (up to 1 s in 50 ms steps) for the worker's asyncio event loop
            # to initialise — avoids a hard 1 s sleep while still being safe on
            # slow hardware.
            _km_wait = 0.0
            _km_limit = 1.0
            _km_step  = 0.05
            while _km_wait < _km_limit:
                if kasa_manager.is_alive():
                    break
                time.sleep(_km_step)
                _km_wait += _km_step
            if kasa_manager.is_alive():
                log_kasa_diag('info', 'KasaManager worker alive and ready',
                              pid=kasa_manager.worker_pid)
            else:
                log_kasa_diag('error', 'KasaManager worker failed to start within 1 s')
        except Exception as _km_exc:
            print(f"[LOG] KasaManager start failed: {_km_exc}")
            log_kasa_diag('error', f'KasaManager start failed: {_km_exc}')
    else:
        print("[LOG] KasaManager not available — plug control disabled")
        log_kasa_diag('warn', 'kasa_manager module not available — plug control disabled')

    # Start the single kasa_result_listener thread (serves all controllers).
    threading.Thread(target=kasa_result_listener, daemon=True).start()
    print("[LOG] Started kasa_result_listener thread")

    # Start all other background threads after kasa components are initialized
    # These threads may use kasa_manager for temperature control
    threading.Thread(target=periodic_temp_control, daemon=True).start()
    print("[LOG] Started periodic_temp_control thread")
    
    threading.Thread(target=periodic_batch_monitoring, daemon=True).start()
    print("[LOG] Started periodic_batch_monitoring thread")
    
    threading.Thread(target=ble_loop, daemon=True).start()
    print("[LOG] Started ble_loop thread")
    
    threading.Thread(target=_background_startup_sync, daemon=True).start()
    print("[LOG] Started background_startup_sync thread")

    # Determine Flask port from environment variable, config file, or default
    # Priority: 1) Environment variable, 2) Config file, 3) Default (5001)
    try:
        port_value = os.environ.get('FLASK_PORT', system_cfg.get('flask_port', 5001))
        flask_port = int(port_value)
        if flask_port < 1 or flask_port > 65535:
            raise ValueError(f"Port must be between 1 and 65535, got {flask_port}")
    except (ValueError, TypeError) as e:
        print(f"[ERROR] Invalid port configuration: {e}")
        print(f"[ERROR] Port value: {port_value}")
        if 'FLASK_PORT' in os.environ:
            print(f"[ERROR] FLASK_PORT environment variable must be a valid port number (1-65535)")
        else:
            print(f"[ERROR] flask_port in {SYSTEM_CFG_FILE} must be a valid port number (1-65535)")
        sys.exit(1)
    
    flask_host = '0.0.0.0'
    
    # Check if port is available, and if not, attempt to free it
    if not is_port_available(flask_port, flask_host):
        
        # If we stopped processes earlier, wait for port to be released
        if stopped_info.get('stopped'):
            if not wait_for_port_release(flask_port, flask_host, max_wait_seconds=10):
                # Port still in use after waiting, try to free it
                if not attempt_to_free_port(flask_port, flask_host):
                    print(f"[ERROR] Either stop that program manually or set FLASK_PORT environment variable")
                    print(f"[ERROR] Example: FLASK_PORT=5001 python3 app.py")
                    print(f"[ERROR] Or update 'flask_port' in {SYSTEM_CFG_FILE}")
                    sys.exit(1)
        else:
            # No app.py processes were stopped, but port is in use by something else
            # Attempt to automatically free the port
            if not attempt_to_free_port(flask_port, flask_host):
                print(f"[ERROR] Either identify and stop that program manually, or use a different port")
                print(f"[ERROR] To use a different port:")
                print(f"[ERROR]   - Set environment variable: FLASK_PORT=5001 python3 app.py")
                print(f"[ERROR]   - Or update 'flask_port' in {SYSTEM_CFG_FILE}")
                sys.exit(1)
    

    # Start a thread to open the browser after Flask starts
    # Only open browser in the main process (not in Werkzeug reloader child process)
    # Skip if SKIP_BROWSER_OPEN is set (e.g., when running via start.sh)
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true' and not os.environ.get('SKIP_BROWSER_OPEN'):
        browser_thread = threading.Thread(target=lambda: open_browser(flask_port), daemon=True)
        browser_thread.start()

    app.run(host=flask_host, port=flask_port, debug=False)
