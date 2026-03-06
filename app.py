from __future__ import annotations

import json
import os
import re
import signal
import shutil
import threading
import time
from datetime import datetime
from glob import glob as glob_func
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, request, render_template, send_file, url_for

try:
    import requests as _requests
except Exception:
    _requests = None

try:
    from kasa_worker import kasa_worker as _kasa_worker_fn, kasa_query_state as _kasa_query_state_fn
except Exception:
    _kasa_worker_fn = None
    _kasa_query_state_fn = None

try:
    from logger import log_kasa_command, log_notification, log_event, send_email, send_push, attempt_send_notifications
except Exception:
    def log_kasa_command(mode, url, action, success=None, error=None):
        pass
    def log_notification(notification_type, subject, body, success, tilt_color=None, error=None):
        pass
    def log_event(event_type, message, tilt_color=None):
        pass
    def send_email(subject, body, cfg=None):
        return False, "logger not available"
    def send_push(body, subject="Fermenter Notification", cfg=None):
        return False, "logger not available"
    def attempt_send_notifications(subject, body, cfg=None):
        return False

from brewid import make_brewid
from storage_jsonl import ensure_dirs, append_sample, read_jsonl, batch_jsonl_path
from logger import log_error
from tilt_static import COLOR_MAP, TILT_UUIDS
from tilt_scan_sim import build_sim_fleet, scan_simulated
from tilt_scan_bleak import scan_bleak
from tilt_table import load_tilt_table, save_tilt_table, upsert_device_from_reading

APP_PORT_DEFAULT = 5001

# ---- Config file paths --------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SYSTEM_CFG_FILE = str(BASE_DIR / 'config' / 'system_config.json')
TILT_CONFIG_FILE = str(BASE_DIR / 'config' / 'tilt_config.json')
TEMP_CFG_FILE = str(BASE_DIR / 'config' / 'temp_control_config.json')
BATCHES_DIR = str(BASE_DIR / 'batches')
LOG_PATH = str(BASE_DIR / 'temp_control' / 'temp_control_log.jsonl')
PER_PAGE = 30
MAX_EXTERNAL_URLS = 3

VALID_SYSTEM_CONFIG_TABS = {
    'main-settings', 'push-email', 'logging-integrations', 'backup-restore'
}

PREDEFINED_FIELD_MAPS = {
    "brewersfriend": {
        "name": "Brewer's Friend",
        "map": {
            "tilt_color": "name",
            "temp_f": "temp",
            "gravity": "gravity",
        }
    }
}

app = Flask(__name__)

# ---- Helper: save JSON file atomically ----------------------------------

def save_json(path, data):
    """Save data to a JSON file, creating parent directories as needed."""
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        log_error(f"[save_json] Failed to save {path}: {e}")
        return False


def get_predefined_field_maps():
    return PREDEFINED_FIELD_MAPS


def _format_file_size(size_bytes):
    """Return a human-readable file size string."""
    for unit in ['bytes', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


# ---- Jinja filter: localtime (template expects it) ----------------------

@app.template_filter("localtime")
def localtime_filter(value):
    """
    Accepts:
      - ISO8601 string like '2026-03-04T10:00:00Z'
      - None
    Returns a simple local display string.
    """
    if not value:
        return "--"
    try:
        # handle trailing Z
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)

# ---- config loading (template -> real) ---------------------------------

def _load_or_init(path: str, template_path: str) -> dict:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(template_path, "r", encoding="utf-8") as f:
            data = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_configs():
    system_cfg = _load_or_init("config/system_config.json", "config/system_config.json.template")
    tilt_cfg = _load_or_init("config/tilt_config.json", "config/tilt_config.json.template")
    temp_cfg = _load_or_init("config/temp_control_config.json", "config/temp_control_config.json.template")
    return system_cfg, tilt_cfg, temp_cfg

ensure_dirs()
system_cfg, tilt_cfg, temp_cfg = load_configs()

# ---- tilt table (per-device registry) ----------------------------------

tilt_table = load_tilt_table()
tilt_table_lock = threading.Lock()

_last_tilt_table_save = 0.0
_TILT_TABLE_SAVE_MIN_INTERVAL_S = 10.0  # debounce to reduce SD writes

def normalize_mac(mac: str) -> str:
    return (mac or "").strip().upper()

def maybe_save_tilt_table(force: bool = False) -> None:
    global _last_tilt_table_save
    t = time.time()
    if force or (t - _last_tilt_table_save) >= _TILT_TABLE_SAVE_MIN_INTERVAL_S:
        with tilt_table_lock:
            save_tilt_table(tilt_table)
        _last_tilt_table_save = t

# ---- Tilt Table update --------------------------------------------------

@app.post("/api/tilt_table/<mac>")
def api_tilt_table_update(mac: str):
    """
    Update per-device registry fields.

    Example:
      curl -X POST http://127.0.0.1:5001/api/tilt_table/F7:2A:46:06:32:0C \
        -H 'Content-Type: application/json' \
        -d '{"tilt_type":"standard","device_label":"Basement Black (Standard)"}'
    """
    mac_n = normalize_mac(mac)
    data = request.get_json(force=True, silent=True) or {}

    tilt_type = (data.get("tilt_type") or "").strip().lower()
    device_label = (data.get("device_label") or "").strip()

    allowed_types = {"standard", "pro", "mini-pro", "unknown", ""}
    if tilt_type not in allowed_types:
        return jsonify(
            {
                "error": "invalid tilt_type",
                "allowed": sorted(t for t in allowed_types if t),
            }
        ), 400

    with tilt_table_lock:
        rec = tilt_table.get(mac_n)
        if rec is None:
            # create a minimal record if missing; will be filled on next scan
            rec = {"mac": mac_n, "tilt_color": "Unknown", "uuid": "", "first_seen": "", "last_seen": ""}
            tilt_table[mac_n] = rec

        if tilt_type:
            rec["tilt_type"] = tilt_type
        if device_label != "":
            rec["device_label"] = device_label

        save_tilt_table(tilt_table)

    return jsonify(rec)



# ---- runtime state ------------------------------------------------------

# Keep latest reading per BrewID (good for storage and future batch views)
live_by_brewid: dict[str, dict] = {}
live_lock = threading.Lock()

def choose_scan_mode() -> str:
    mode = (system_cfg.get("tilt_scan_mode") or "bleak").lower()
    if mode not in ("bleak", "sim"):
        mode = "bleak"
    return mode

sim_fleet = build_sim_fleet(n_per_color=int(system_cfg.get("sim_n_per_color", 1)))

def resolve_batch_id_for_device(color: str, mac: str) -> str:
    """
    Backward compatible resolution:
      - Prefer per-device mapping under tilt_cfg[color]["devices"][<MAC>]
      - Fall back to per-color mapping under tilt_cfg[color]
    """
    cobj = tilt_cfg.get(color) or {}

    devices = cobj.get("devices") or {}
    mac_n = normalize_mac(mac)

    if isinstance(devices, dict) and mac_n in devices:
        dobj = devices.get(mac_n) or {}
        bid = (dobj.get("batch_id") or dobj.get("batch_name") or dobj.get("beer_name") or "").strip()
        if bid:
            return bid

    return (cobj.get("batch_id") or cobj.get("batch_name") or cobj.get("beer_name") or "batch").strip()

def scan_once() -> list[dict]:
    mode = choose_scan_mode()
    if mode == "sim":
        return scan_simulated(sim_fleet)
    return scan_bleak(scan_seconds=float(system_cfg.get("bleak_scan_seconds", 4.0)))

def _build_live_tilts_by_color() -> dict[str, dict]:
    """
    Build what the legacy template expects: dict keyed by *tilt color*.
    If multiple BrewIDs share a color (possible), keep the most recent by timestamp.
    """
    by_color: dict[str, dict] = {}

    with live_lock:
        items = list(live_by_brewid.items())

    for brewid, r in items:
        color = r.get("tilt_color") or "Unknown"
        cfg = tilt_cfg.get(color) or {}

        # timestamp: prefer reading timestamp
        ts = r.get("timestamp") or r.get("captured_at")

        card = {
            "beer_name": cfg.get("beer_name") or cfg.get("batch_name") or "Unnamed Beer",
            "batch_name": cfg.get("batch_name") or "",
            "recipe_og": cfg.get("recipe_og"),
            "recipe_fg": cfg.get("recipe_fg"),
            "recipe_abv": cfg.get("recipe_abv"),
            "original_gravity": cfg.get("original_gravity") or cfg.get("actual_og") or cfg.get("og"),
            "actual_og": cfg.get("actual_og") or cfg.get("original_gravity"),
            "gravity": r.get("gravity"),
            "temp_f": r.get("temp_f"),
            "rssi": r.get("rssi"),
            "tilt_color": color,
            "color_code": COLOR_MAP.get(color, "#333"),
            "brewid": brewid,
            # template uses mac_address field name
            "mac_address": r.get("mac"),
            "timestamp": ts,
        }

        # keep newest for this color
        prev = by_color.get(color)
        if not prev:
            by_color[color] = card
            continue

        # compare timestamps lexicographically (ISO sorts OK)
        prev_ts = prev.get("timestamp") or ""
        this_ts = ts or ""
        if str(this_ts) >= str(prev_ts):
            by_color[color] = card

    return by_color

def poll_loop():
    interval = float(system_cfg.get("scan_interval_seconds", 5.0))
    while True:
        try:
            readings = scan_once()
            now = datetime.utcnow().isoformat() + "Z"
            updates = {}

            tilt_table_dirty = False

            for r in readings:
                color = r.get("tilt_color")
                mac = normalize_mac(r.get("mac", ""))
                model = int(r.get("model", 0))
                uuid = str(r.get("uuid", "") or "")

                if not color or not mac:
                    continue

                # upsert complete per-device record
                try:
                    with tilt_table_lock:
                        upsert_device_from_reading(
                            tilt_table,
                            mac=mac,
                            tilt_color=color,
                            uuid=uuid,
                            rssi=r.get("rssi"),
                            temp_f=r.get("temp_f"),
                            gravity=r.get("gravity"),
                        )
                    tilt_table_dirty = True
                except Exception as e:
                    log_error(f"tilt_table upsert error: {e}")

                batch_id = resolve_batch_id_for_device(color, mac)
                brewid = make_brewid(batch_id=batch_id, tilt_color=color, model=model, mac=mac)

                payload = dict(r)
                payload["mac"] = mac
                payload["brewid"] = brewid
                payload["captured_at"] = now

                updates[brewid] = payload
                append_sample(brewid, payload)

            if tilt_table_dirty:
                maybe_save_tilt_table()

            with live_lock:
                live_by_brewid.update(updates)

        except Exception as e:
            log_error(f"poll loop error: {e}")

        time.sleep(interval)

threading.Thread(target=poll_loop, daemon=True).start()

# ---- API endpoints ------------------------------------------------------

@app.get("/api/live")
def api_live():
    # returns the BrewID-keyed dict
    with live_lock:
        return jsonify(live_by_brewid)

@app.get("/api/batch/<brewid>")
def api_batch(brewid: str):
    path = batch_jsonl_path(brewid)
    return jsonify(read_jsonl(path, limit=int(request.args.get("limit", "2000"))))

@app.get("/api/tilt_table")
def api_tilt_table():
    with tilt_table_lock:
        return jsonify(tilt_table)

# ---- endpoints expected by template JS ---------------------------------

@app.get("/live_snapshot")
def live_snapshot():
    controllers_data = []
    for controller in temp_cfg.get('controllers', []):
        tilt_color = controller.get("tilt_color", "")
        controllers_data.append({
            "controller_id": controller.get("controller_id", 0),
            "current_temp": controller.get("current_temp"),
            "low_limit": controller.get("low_limit"),
            "high_limit": controller.get("high_limit"),
            "tilt_color": tilt_color,
            "tilt_color_code": COLOR_MAP.get(tilt_color, ""),
            "heater_on": controller.get("heater_on"),
            "cooler_on": controller.get("cooler_on"),
            "heater_pending": controller.get("heater_pending"),
            "cooler_pending": controller.get("cooler_pending"),
            "enable_heating": controller.get("enable_heating"),
            "enable_cooling": controller.get("enable_cooling"),
            "status": controller.get("status"),
            "mode": controller.get("mode", "Off"),
            "temp_control_active": controller.get("temp_control_active", False),
            "heating_error": controller.get("heating_error", False),
            "cooling_error": controller.get("cooling_error", False),
            "notifications_trigger": controller.get("notifications_trigger"),
            "notification_comm_failure": controller.get("notification_comm_failure"),
            "last_reading_time": controller.get("last_reading_time"),
        })
    return jsonify({
        "live_tilts": _build_live_tilts_by_color(),
        "controllers": controllers_data,
        "warning_mode": system_cfg.get("warning_mode"),
    })

# ---- startup splash -----------------------------------------------------

@app.get("/startup")
def startup():
    return render_template("startup.html")

# ---- system config ------------------------------------------------------

@app.get("/system_config")
def system_config():
    active_tab = request.args.get('tab', 'main-settings')
    if active_tab not in VALID_SYSTEM_CONFIG_TABS:
        active_tab = 'main-settings'
    external_urls = system_cfg.get("external_urls", [])
    while len(external_urls) < MAX_EXTERNAL_URLS:
        external_urls.append({
            "name": "", "url": "", "method": "POST",
            "content_type": "form", "timeout_seconds": 8, "field_map_id": "default"
        })
    return render_template('system_config.html',
                           system_settings=system_cfg,
                           external_urls=external_urls,
                           predefined_field_maps=get_predefined_field_maps(),
                           active_tab=active_tab)


@app.post("/update_system_config")
def update_system_config():
    data = request.form
    active_tab = data.get('active_tab', 'main-settings')
    if active_tab not in VALID_SYSTEM_CONFIG_TABS:
        active_tab = 'main-settings'

    sending_email_password = data.get("sending_email_password", "")
    if sending_email_password:
        system_cfg["smtp_password"] = sending_email_password

    pushover_api_token = data.get("pushover_api_token", "")
    if pushover_api_token:
        system_cfg["pushover_api_token"] = pushover_api_token

    ntfy_auth_token = data.get("ntfy_auth_token", "")
    if ntfy_auth_token:
        system_cfg["ntfy_auth_token"] = ntfy_auth_token

    external_urls = []
    for i in range(MAX_EXTERNAL_URLS):
        url_config = {
            "name": data.get(f"external_name_{i}", "").strip() or f"Service {i + 1}",
            "url": data.get(f"external_url_{i}", "").strip(),
            "method": data.get(f"external_method_{i}", "POST"),
            "content_type": data.get(f"external_content_type_{i}", "form"),
            "timeout_seconds": int(data.get(f"external_timeout_seconds_{i}", 8)),
            "field_map_id": data.get(f"external_field_map_id_{i}", "default"),
        }
        if url_config["field_map_id"] == "custom":
            custom_map = data.get(f"external_custom_field_map_{i}", "").strip()
            if custom_map:
                url_config["custom_field_map"] = custom_map
        external_urls.append(url_config)

    system_cfg.update({
        "brewery_name": data.get("brewery_name", ""),
        "brewer_name": data.get("brewer_name", ""),
        "email": data.get("email", ""),
        "display_mode": data.get("display_mode", "4"),
        "update_interval": data.get("update_interval", "2"),
        "external_refresh_rate": data.get("external_refresh_rate", system_cfg.get("external_refresh_rate", "15")),
        "external_urls": external_urls,
        "warning_mode": data.get("warning_mode", "NONE"),
        "sending_email": data.get("sending_email", system_cfg.get('sending_email', '')),
        "smtp_host": data.get("smtp_host", system_cfg.get('smtp_host', 'smtp.gmail.com')),
        "smtp_port": int(data.get("smtp_port", system_cfg.get('smtp_port', 587))),
        "smtp_starttls": 'smtp_starttls' in data,
        "push_provider": data.get("push_provider", system_cfg.get("push_provider", "pushover")),
        "pushover_user_key": data.get("pushover_user_key", system_cfg.get("pushover_user_key", "")),
        "pushover_device": data.get("pushover_device", system_cfg.get("pushover_device", "")),
        "ntfy_server": data.get("ntfy_server", system_cfg.get("ntfy_server", "https://ntfy.sh")),
        "ntfy_topic": data.get("ntfy_topic", system_cfg.get("ntfy_topic", "")),
        "enable_kasa_activity_log": 'enable_kasa_activity_log' in data,
        "tilt_logging_interval_minutes": int(data.get("tilt_logging_interval_minutes",
                                                        system_cfg.get("tilt_logging_interval_minutes", 15))),
    })
    system_cfg['temp_control_notifications'] = {
        'enable_temp_below_low_limit': 'enable_temp_below_low_limit' in data,
        'enable_temp_above_high_limit': 'enable_temp_above_high_limit' in data,
        'enable_heating_on': 'enable_heating_on' in data,
        'enable_heating_off': 'enable_heating_off' in data,
        'enable_cooling_on': 'enable_cooling_on' in data,
        'enable_cooling_off': 'enable_cooling_off' in data,
        'enable_kasa_error': 'enable_kasa_error' in data,
    }
    system_cfg['batch_notifications'] = {
        'enable_loss_of_signal': 'enable_loss_of_signal' in data,
        'loss_of_signal_timeout_minutes': int(data.get('loss_of_signal_timeout_minutes', 30)),
        'enable_fermentation_starting': 'enable_fermentation_starting' in data,
        'enable_fermentation_completion': 'enable_fermentation_completion' in data,
        'enable_daily_report': 'enable_daily_report' in data,
        'daily_report_time': data.get('daily_report_time', '09:00'),
    }
    save_json(SYSTEM_CFG_FILE, system_cfg)
    return redirect(f'/system_config?tab={active_tab}')


@app.post("/test_email")
def test_email():
    subject = "TEST - Fermentatorium"
    body = ("*** TEST MESSAGE ***\n\nThis is a TEST email from your Fermentatorium system.\n\n"
            "If you received this, your email settings are configured correctly!\n\n*** TEST MESSAGE ***")
    success, error_msg = send_email(subject, body)
    log_notification('email', subject, body, success, error=error_msg if not success else None)
    if success:
        return jsonify({'success': True, 'message': 'Test email sent successfully! Check your inbox.'})
    return jsonify({'success': False, 'message': f'Failed to send test email: {error_msg}'})


@app.post("/test_push")
def test_push():
    push_provider = system_cfg.get("push_provider", "pushover").lower()
    provider_name = "Pushover" if push_provider == "pushover" else "ntfy"
    subject = "TEST - Fermentatorium"
    body = (f"*** TEST MESSAGE *** This is a TEST push notification from your Fermentatorium system. "
            f"If you received this, your {provider_name} settings are configured correctly! *** TEST MESSAGE ***")
    success, error_msg = send_push(body, subject=subject)
    log_notification('push', subject, body, success, error=error_msg if not success else None)
    if success:
        return jsonify({'success': True,
                        'message': f'Test push notification sent successfully via {provider_name}! Check your device.'})
    return jsonify({'success': False, 'message': f'Failed to send test push notification: {error_msg}'})

_SSRF_BLOCKED_HOSTS = re.compile(
    r'^(localhost|127\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)',
    re.IGNORECASE,
)

def _is_safe_external_url(url: str) -> tuple[bool, str]:
    """Return (ok, error_message). Reject internal/loopback targets."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Malformed URL"
    if parsed.scheme not in ('http', 'https'):
        return False, "URL must start with http:// or https://"
    hostname = parsed.hostname or ""
    if not hostname:
        return False, "URL must include a hostname"
    if _SSRF_BLOCKED_HOSTS.match(hostname) or hostname == "0.0.0.0":
        return False, "Requests to internal addresses are not allowed"
    return True, ""


@app.post("/test_external_logging")
def test_external_logging():
    """
    Test external logging connection with a test payload.

    Security note: This endpoint intentionally makes outbound HTTP requests to
    user-configured URLs for testing external logging integrations.  Risk is
    mitigated by:
      - Admin-only access (system config page)
      - Allowlist enforcement: only http/https to non-internal hosts
      - Timeout limits
      - No sensitive data in test payload
    """
    try:
        data = request.get_json()
        url = (data.get('url') or '').strip()
        if not url:
            return jsonify({'success': False, 'message': 'No URL provided'})

        safe, err = _is_safe_external_url(url)
        if not safe:
            return jsonify({'success': False, 'message': err})

        # Reconstruct URL from validated parsed components to avoid using raw user input directly
        _parsed_safe = urlparse(url)
        validated_url = _parsed_safe.geturl()

        test_payload = {
            "tilt_color": "TEST", "temp_f": 68.5, "gravity": 1.050,
            "brewid": "test_batch", "beer_name": "Test Beer",
            "timestamp": datetime.utcnow().isoformat() + "Z", "test": True,
        }
        method = (data.get('method') or system_cfg.get("external_method", "POST")).upper()
        content_type = data.get('content_type') or system_cfg.get("external_content_type", "form")
        send_json = (content_type == "json")
        timeout = int(data.get('timeout_seconds') or system_cfg.get("external_timeout_seconds", 8) or 8)

        try:
            parsed_url = urlparse(validated_url)
            netloc = parsed_url.netloc.lower()
            # Exact match or subdomain of brewersfriend.com
            if netloc == 'brewersfriend.com' or netloc.endswith('.brewersfriend.com'):
                test_payload = {"name": "TEST", "temp": 68.5, "temp_unit": "F",
                                "gravity": 1.050, "gravity_unit": "G", "beer": "Test Connection"}
                send_json = True
        except Exception:
            pass

        if _requests is None:
            return jsonify({'success': False, 'message': 'Requests library not available'})

        try:
            if send_json:
                resp = _requests.request(method, validated_url, json=test_payload,
                                         headers={"Content-Type": "application/json"}, timeout=timeout)
            else:
                resp = _requests.request(method, validated_url, data=test_payload,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"},
                                         timeout=timeout)
            if 200 <= resp.status_code < 300:
                return jsonify({'success': True, 'message': f'Connection successful! Status: {resp.status_code}'})
            return jsonify({'success': False,
                            'message': f'Connection failed with HTTP status {resp.status_code}'})
        except Exception:
            return jsonify({'success': False,
                            'message': 'Request failed. Please check the URL and try again.'})
    except Exception:
        return jsonify({'success': False,
                        'message': 'An error occurred while testing the connection.'})

# ---- tilt config & batch settings ---------------------------------------

@app.route("/tilt_config", methods=["GET", "POST"])
def tilt_config():
    selected = request.args.get('tilt_color') or request.form.get('tilt_color')
    if request.method == 'POST':
        color = request.form.get('tilt_color')
        action = request.form.get('action')
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
                           system_settings=system_cfg)


@app.route("/batch_settings", methods=["GET", "POST"])
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
        brew_id = existing.get('brewid') or make_brewid(
            batch_id=beer_name or batch_name, tilt_color=color, model=0, mac="")

        batch_entry = {
            "beer_name": beer_name,
            "batch_name": batch_name,
            "ferm_start_date": start_date,
            "recipe_og": data.get('recipe_og', '') or '',
            "recipe_fg": data.get('recipe_fg', '') or '',
            "recipe_abv": data.get('recipe_abv', '') or '',
            "actual_og": data.get('actual_og', '') or None,
            "og_confirmed": False,
            "brewid": brew_id,
            "is_active": True,
            "closed_date": None,
        }
        if color in tilt_cfg and 'notification_state' in tilt_cfg[color]:
            batch_entry['notification_state'] = dict(tilt_cfg[color]['notification_state'])
        else:
            batch_entry['notification_state'] = {
                "fermentation_start_datetime": None,
                "fermentation_completion_datetime": None,
                "last_daily_report": None,
            }

        os.makedirs(BATCHES_DIR, exist_ok=True)
        hist_file = os.path.join(BATCHES_DIR, f'batch_history_{color}.json')
        try:
            with open(hist_file, 'r') as f:
                batches = json.load(f)
        except Exception:
            batches = []

        updated = False
        for i, b in enumerate(batches):
            if b.get('brewid') == brew_id:
                batches[i] = batch_entry
                updated = True
                break
        if not updated:
            batches.append(batch_entry)

        try:
            with open(hist_file, 'w') as f:
                json.dump(batches, f, indent=2)
        except Exception as e:
            print(f"[LOG] Could not save batch history for {color}: {e}")

        tilt_cfg[color] = batch_entry
        save_json(TILT_CONFIG_FILE, tilt_cfg)
        return redirect(f"/batch_settings?tilt_color={color}")

    selected = request.args.get('tilt_color')
    action = request.args.get('action')
    config = tilt_cfg.get(selected, {}) if selected else {}
    hist_file = os.path.join(BATCHES_DIR, f'batch_history_{selected}.json') if selected else None
    batch_history = []
    if hist_file and os.path.exists(hist_file):
        try:
            with open(hist_file) as f:
                batch_history = json.load(f)
        except Exception:
            pass
    live_tilts = _build_live_tilts_by_color()
    return render_template('batch_settings.html',
                           tilt_cfg=tilt_cfg,
                           tilt_colors=list(TILT_UUIDS.values()),
                           active_colors=list(live_tilts.keys()),
                           live_tilts=live_tilts,
                           selected_tilt=selected,
                           selected_config=config,
                           system_settings=system_cfg,
                           action=action,
                           batch_history=batch_history,
                           color_map=COLOR_MAP)

# ---- batch history & review ---------------------------------------------

@app.get("/batch_history")
def batch_history():
    sort_order = request.args.get('sort', 'newest')
    active_batches = []
    closed_batches = []

    for color in TILT_UUIDS.values():
        hist_file = os.path.join(BATCHES_DIR, f'batch_history_{color}.json')
        if not os.path.exists(hist_file):
            continue
        try:
            with open(hist_file) as f:
                batches = json.load(f)
            for batch in batches:
                batch['color'] = color
                if 'is_active' not in batch:
                    batch['is_active'] = True
                if batch.get('is_active', True):
                    active_batches.append(batch)
                else:
                    closed_batches.append(batch)
        except Exception as e:
            print(f"[LOG] Error loading batch history for {color}: {e}")

    def apply_sort(batches, order):
        if order == 'newest':
            return sorted(batches, key=lambda x: x.get('ferm_start_date', ''), reverse=True)
        if order == 'oldest':
            return sorted(batches, key=lambda x: x.get('ferm_start_date', ''))
        if order == 'beer_name':
            return sorted(batches, key=lambda x: (x.get('beer_name', '').lower(), x.get('ferm_start_date', '')))
        if order == 'color':
            return sorted(batches, key=lambda x: (x.get('color', ''), x.get('ferm_start_date', '')), reverse=True)
        return batches

    return render_template('batch_history_select.html',
                           active_batches=apply_sort(active_batches, sort_order),
                           closed_batches=apply_sort(closed_batches, sort_order),
                           color_map=COLOR_MAP,
                           sort_order=sort_order)


def _load_batch_info(brewid):
    """Find batch_info dict and color for given brewid across all history files."""
    for color in TILT_UUIDS.values():
        hist_file = os.path.join(BATCHES_DIR, f'batch_history_{color}.json')
        if not os.path.exists(hist_file):
            continue
        try:
            with open(hist_file) as f:
                for b in json.load(f):
                    if b.get('brewid') == brewid:
                        return b, color
        except Exception:
            pass
    return None, None


def _load_batch_data(brewid):
    """Load all JSONL entries for the given brewid."""
    batch_data = []
    for path in glob_func(os.path.join(BATCHES_DIR, f'*{brewid}*.jsonl')):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            batch_data.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
    return batch_data


def _calculate_batch_statistics(batch_data, batch_info):
    all_samples = [
        entry.get('payload', entry)
        for entry in batch_data
        if entry.get('event') in ('sample', 'SAMPLE', 'tilt_reading')
    ]
    stats = {
        'total_readings': len(all_samples),
        'duration_days': None, 'start_gravity': None, 'end_gravity': None,
        'gravity_change': None, 'start_temp': None, 'end_temp': None,
        'avg_temp': None, 'min_temp': None, 'max_temp': None, 'estimated_abv': None,
    }
    if not all_samples:
        return stats
    temps = [s.get('temp_f') for s in all_samples if s.get('temp_f') is not None]
    if temps:
        stats.update({'avg_temp': round(sum(temps) / len(temps), 1),
                      'min_temp': min(temps), 'max_temp': max(temps),
                      'start_temp': temps[0], 'end_temp': temps[-1]})
    gravs = [s.get('gravity') for s in all_samples if s.get('gravity') is not None]
    if gravs:
        stats.update({'start_gravity': gravs[0], 'end_gravity': gravs[-1],
                      'gravity_change': round(gravs[0] - gravs[-1], 3)})
        actual_og = batch_info.get('actual_og')
        if actual_og:
            try:
                stats['estimated_abv'] = round((float(actual_og) - gravs[-1]) * 131.25, 2)
            except (ValueError, TypeError):
                pass
    timestamps = [s.get('timestamp') for s in all_samples if s.get('timestamp')]
    if len(timestamps) >= 2:
        try:
            t0 = datetime.fromisoformat(timestamps[0].replace('Z', '+00:00'))
            t1 = datetime.fromisoformat(timestamps[-1].replace('Z', '+00:00'))
            stats['duration_days'] = (t1 - t0).days
        except Exception:
            pass
    return stats


@app.get("/batch_review/<brewid>")
def batch_review(brewid):
    if not re.match(r'^[a-zA-Z0-9\-_]+$', brewid):
        log_error(f"[batch_review] Rejected invalid brewid (length={len(brewid)})")
        return "Invalid batch ID", 400
    batch_info, color = _load_batch_info(brewid)
    if not batch_info:
        return "Batch not found", 404
    batch_data = _load_batch_data(brewid)
    return render_template('batch_review.html',
                           batch=batch_info, color=color,
                           batch_data=batch_data,
                           stats=_calculate_batch_statistics(batch_data, batch_info),
                           color_map=COLOR_MAP)

# ---- temperature control config -----------------------------------------

def _ensure_three_controllers():
    """Ensure temp_cfg has exactly 3 controllers; mutates and saves if needed."""
    controllers = temp_cfg.setdefault('controllers', [])
    changed = False
    while len(controllers) < 3:
        new_id = len(controllers)
        controllers.append({
            "controller_id": new_id, "low_limit": 50, "high_limit": 54,
            "tilt_color": "", "enable_heating": False, "enable_cooling": False,
            "heating_plug": "", "cooling_plug": "", "compressor_delay": 5,
            "current_temp": None, "heater_on": False, "cooler_on": False,
            "heater_pending": False, "cooler_pending": False,
            "heating_error": False, "cooling_error": False,
            "temp_control_enabled": True, "temp_control_active": False,
            "mode": "Off", "status": "Not Configured",
        })
        changed = True
    if changed:
        save_json(TEMP_CFG_FILE, temp_cfg)
    return controllers


@app.get("/temp_config")
def temp_config():
    try:
        controller_id = int(request.args.get('controller_id', 0))
        if not 0 <= controller_id <= 2:
            controller_id = 0
    except (ValueError, TypeError):
        controller_id = 0
    controllers = _ensure_three_controllers()
    current_controller = controllers[controller_id] if controller_id < len(controllers) else {}
    return render_template('temp_control_config.html',
                           temp_control=current_controller,
                           controller_id=controller_id,
                           controllers=controllers,
                           tilt_cfg=tilt_cfg,
                           system_settings=system_cfg,
                           live_tilts=_build_live_tilts_by_color())


@app.post("/update_temp_config")
def update_temp_config():
    data = request.form
    try:
        controller_id = int(data.get('controller_id', 0))
        if not 0 <= controller_id <= 2:
            controller_id = 0
    except (ValueError, TypeError):
        controller_id = 0
    controllers = _ensure_three_controllers()
    if controller_id >= len(controllers):
        return redirect('/temp_config?controller_id=0')
    controller = controllers[controller_id]
    try:
        low_limit = float(data.get('low_limit', controller.get('low_limit', 50)))
    except (ValueError, TypeError):
        low_limit = controller.get('low_limit', 50)
    try:
        high_limit = float(data.get('high_limit', controller.get('high_limit', 54)))
    except (ValueError, TypeError):
        high_limit = controller.get('high_limit', 54)
    if high_limit <= low_limit:
        low_limit = controller.get('low_limit', 50)
        high_limit = controller.get('high_limit', 54)
    controller.update({
        "tilt_color": data.get('tilt_color', ''),
        "low_limit": low_limit,
        "high_limit": high_limit,
        "enable_heating": 'enable_heating' in data,
        "enable_cooling": 'enable_cooling' in data,
        "heating_plug": data.get("heating_plug", ""),
        "cooling_plug": data.get("cooling_plug", ""),
    })
    save_json(TEMP_CFG_FILE, temp_cfg)
    return redirect(f'/temp_config?controller_id={controller_id}')


@app.post("/toggle_temp_control")
def toggle_temp_control():
    """Toggle temp_control_active on a controller."""
    try:
        data = request.get_json() if request.is_json else request.form
        try:
            controller_id = int(data.get('controller_id', 0))
            if not 0 <= controller_id <= 2:
                controller_id = 0
        except (ValueError, TypeError):
            controller_id = 0
        controllers = _ensure_three_controllers()
        if controller_id >= len(controllers):
            return jsonify({'error': f'Controller {controller_id} not found'}), 400
        controller = controllers[controller_id]

        active_value = data.get('active')
        if isinstance(active_value, bool):
            new_state = active_value
        else:
            new_state = str(active_value).lower() in ('true', '1')

        new_session = str(data.get('new_session', False)).lower() in ('true', '1')
        if new_state and new_session and os.path.exists(LOG_PATH):
            try:
                logs_dir = str(BASE_DIR / 'logs')
                os.makedirs(logs_dir, exist_ok=True)
                ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                tilt_color = controller.get("tilt_color", "unknown")
                archive = os.path.join(logs_dir,
                                       f"temp_control_controller{controller_id}_{tilt_color}_{ts}.jsonl")
                shutil.move(LOG_PATH, archive)
            except Exception as e:
                return jsonify({"success": False, "error": f"Failed to archive log: {e}"}), 500

        controller['temp_control_active'] = new_state
        save_json(TEMP_CFG_FILE, temp_cfg)
        redirect_url = (f"/temp_config?controller_id={controller_id}"
                        if (new_state and new_session) else None)
        return jsonify({"success": True, "active": new_state, "redirect": redirect_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ---- temperature report -------------------------------------------------

@app.route("/temp_report", methods=["GET", "POST"])
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
        return render_template('temp_report_select.html',
                               colors=colors,
                               default_color=colors[0] if colors else None)

    entries = []
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        if obj.get('event') in ('tilt_reading', 'SAMPLE'):
                            entries.append(obj.get('payload', obj))
                    except Exception:
                        pass
        except Exception as e:
            print(f"[LOG] Could not read log for temp_report: {e}")

    tc = tilt_cfg.get(tilt_color, {})
    brewid = tc.get('brewid')
    filtered = [p for p in entries if
                (brewid and p.get('brewid') == brewid) or
                (not brewid and (p.get('beer_name') == tc.get('beer_name') or
                                  p.get('batch_name') == tc.get('batch_name')))]

    lines = []
    for p in reversed(filtered):
        ts = p.get('timestamp', '')
        tempf = p.get('temp_f', '')
        grav = p.get('gravity', '')
        bid = p.get('brewid') or '--'
        beer = p.get('beer_name') or p.get('batch_name') or ''
        lines.append(f"{ts} — {beer} — Temp: {tempf}°F — Gravity: {grav} — Brew ID: {bid}")

    total_pages = max(1, ceil(len(lines) / PER_PAGE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * PER_PAGE
    page_data = lines[start:start + PER_PAGE]

    return render_template('temp_report_display.html',
                           color=tilt_color,
                           page=page, total_pages=total_pages,
                           page_data=page_data, at_end=(page >= total_pages))

# ---- log management -----------------------------------------------------

@app.get("/log_management")
def log_management():
    try:
        def file_size(path):
            try:
                return _format_file_size(os.path.getsize(path))
            except Exception:
                return "0 bytes"

        temp_log_size = file_size(LOG_PATH)
        kasa_log_path = str(BASE_DIR / 'logs' / 'kasa_activity_monitoring.jsonl')
        kasa_log_size = file_size(kasa_log_path)
        notif_log_path = str(BASE_DIR / 'logs' / 'notifications_log.jsonl')
        notifications_log_size = file_size(notif_log_path)

        app_logs = []
        log_dir = str(BASE_DIR / 'logs')
        if os.path.exists(log_dir):
            for filename in os.listdir(log_dir):
                if filename.endswith('.log') and filename != '.gitkeep':
                    filepath = os.path.join(log_dir, filename)
                    app_logs.append({
                        'name': filename,
                        'size': _format_file_size(os.path.getsize(filepath)),
                        'path': filepath,
                    })

        batches = []
        if os.path.exists(BATCHES_DIR):
            for filename in os.listdir(BATCHES_DIR):
                if filename.endswith('.jsonl') and not filename.endswith('.backup'):
                    filepath = os.path.join(BATCHES_DIR, filename)
                    name_without_ext = filename[:-6]
                    brewid = name_without_ext.rsplit('_', 1)[-1]
                    beer_name = batch_name = ferm_start_date = None
                    for color, cfg in tilt_cfg.items():
                        if cfg.get('brewid') == brewid:
                            beer_name = cfg.get('beer_name')
                            batch_name = cfg.get('batch_name')
                            ferm_start_date = cfg.get('ferm_start_date')
                            break
                    batches.append({
                        'filename': filename, 'brewid': brewid,
                        'beer_name': beer_name, 'batch_name': batch_name,
                        'ferm_start_date': ferm_start_date,
                        'size': _format_file_size(os.path.getsize(filepath)),
                    })

        def sort_key(b):
            d = b.get('ferm_start_date')
            if d:
                try:
                    return datetime.strptime(d, '%Y-%m-%d')
                except Exception:
                    pass
            return datetime(1900, 1, 1)

        batches.sort(key=sort_key, reverse=True)
        return render_template('log_management.html',
                               temp_log_size=temp_log_size,
                               kasa_log_size=kasa_log_size,
                               notifications_log_size=notifications_log_size,
                               app_logs=app_logs,
                               batches=batches,
                               success_message=request.args.get('success'),
                               error_message=request.args.get('error'))
    except Exception as e:
        print(f"[LOG] Error in log_management: {e}")
        return "Error loading log management page", 500

# ---- chart routes -------------------------------------------------------

@app.get("/chart_plotly")
def chart_plotly_index():
    colors = list(tilt_cfg.keys())
    if colors:
        return redirect(f'/chart_plotly/{colors[0]}')
    return render_template('chart_plotly.html', tilt_color=None, system_settings=system_cfg)


@app.get("/chart_plotly/<tilt_color>")
def chart_plotly_for(tilt_color):
    if tilt_color and tilt_color != "TempControl" and tilt_color not in tilt_cfg:
        abort(404)
    return render_template('chart_plotly.html',
                           tilt_color=tilt_color,
                           tilt_cfg=tilt_cfg,
                           system_settings=system_cfg)


@app.get("/chart_data/<tilt_color>")
def chart_data_for(tilt_color):
    """Return gravity/temp data for chart from stored JSONL."""
    limit = request.args.get('limit', type=int, default=500)
    all_data = request.args.get('all', '').lower() in ('1', 'true', 'yes', 'on')

    brewid = tilt_cfg.get(tilt_color, {}).get('brewid')
    if not brewid:
        return jsonify([])

    path = batch_jsonl_path(brewid)
    rows = read_jsonl(path, limit=0 if all_data else limit)
    samples = []
    for entry in rows:
        if isinstance(entry, dict):
            payload = entry.get('payload', entry)
            if payload.get('gravity') is not None:
                samples.append({
                    'timestamp': payload.get('timestamp') or payload.get('captured_at'),
                    'gravity': payload.get('gravity'),
                    'temp_f': payload.get('temp_f'),
                })
    return jsonify(samples)

# ---- exit system --------------------------------------------------------

@app.route("/exit_system", methods=["GET", "POST"])
def exit_system():
    resp = render_template("exit_system.html")
    def _shutdown():
        time.sleep(0.8)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_shutdown, daemon=True).start()
    return resp

# ---- main dashboard -----------------------------------------------------

@app.get("/")
def index():
    return render_template(
        "maindisplay.html",
        system_settings=system_cfg,
        live_tilts=_build_live_tilts_by_color(),
        controllers=temp_cfg.get("controllers", []),
    )

def main():
    port = int(os.environ.get("FLASK_PORT", system_cfg.get("flask_port", APP_PORT_DEFAULT)))
    app.run(host="0.0.0.0", port=port, debug=bool(int(os.environ.get("FLASK_DEBUG", "0"))))

if __name__ == "__main__":
    main()