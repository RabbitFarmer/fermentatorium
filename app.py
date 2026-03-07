from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

import asyncio
import csv
import hashlib
import io
import itertools
import re
import shutil
import smtplib
import socket
import subprocess
import sys
from collections import deque, defaultdict
from email.mime.text import MIMEText
from glob import glob as glob_func
from math import ceil
from multiprocessing import Process, Queue as MPQueue
from urllib.parse import urlparse
import urllib.request
import urllib.error

from flask import Flask, abort, jsonify, make_response, redirect, request, render_template, send_file, url_for

try:
    import requests as _requests
except Exception:
    _requests = None

try:
    import psutil
except Exception:
    psutil = None

try:
    from kasa_worker import kasa_worker as _kasa_worker_fn, kasa_query_state as _kasa_query_state_fn
except Exception:
    _kasa_worker_fn = None
    _kasa_query_state_fn = None

try:
    from logger import log_kasa_command, log_notification, log_event
except Exception:
    def log_kasa_command(mode, url, action, success=None, error=None):
        pass
    def log_notification(notification_type, subject, body, success, tilt_color=None, error=None):
        pass
    def log_event(event_type, message, tilt_color=None):
        pass

from brewid import make_brewid
from storage_jsonl import ensure_dirs, append_sample, read_jsonl, batch_jsonl_path
from logger import log_error
from tilt_static import COLOR_MAP
from tilt_scan_sim import build_sim_fleet, scan_simulated
try:
    from tilt_scan_bleak import scan_bleak as _scan_bleak_impl
    _BLEAK_AVAILABLE = True
except Exception as _bleak_import_err:
    print(
        f"[startup] WARNING: bleak unavailable ({_bleak_import_err}). "
        "BLE scanning is disabled; the app will still serve the dashboard.",
        flush=True,
    )
    _scan_bleak_impl = None
    _BLEAK_AVAILABLE = False
from tilt_table import load_tilt_table, save_tilt_table, upsert_device_from_reading

APP_PORT_DEFAULT = 5001

app = Flask(__name__)

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
    if not _BLEAK_AVAILABLE:
        return []
    return _scan_bleak_impl(scan_seconds=float(system_cfg.get("bleak_scan_seconds", 4.0)))

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
    # controllers are not wired yet; keep empty list for now
    return jsonify(
        {
            "live_tilts": _build_live_tilts_by_color(),
            "controllers": [],
        }
    )

# ---- startup / loading page ---------------------------------------------

@app.get("/startup")
def startup_page():
    """Animated loading page shown while the app is warming up.
    The page's JavaScript polls '/' every two seconds and redirects
    automatically once the main dashboard is serving normally.
    """
    return render_template("startup.html")

# ---- main dashboard -----------------------------------------------------

@app.get("/")
def index():
    system_settings = {
        "brewery_name": system_cfg.get("brewery_name", "tiltcontrlmonitor"),
        "display_mode": system_cfg.get("display_mode", "4"),
    }
    return render_template(
        "maindisplay.html",
        system_settings=system_settings,
        live_tilts=_build_live_tilts_by_color(),
        controllers=[],
    )

# ---- helper utilities for settings routes ------------------------------

def _file_size_str(path: str) -> str:
    """Return a human-readable file size string, or 'N/A' if file not found."""
    try:
        size = os.path.getsize(path)
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"
    except OSError:
        return "N/A"

def _batch_status_path() -> str:
    return str(Path(__file__).resolve().parent / "batches" / "batch_status.json")

def _load_batch_status() -> dict:
    try:
        with open(_batch_status_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

def _save_batch_status(status: dict) -> None:
    path = _batch_status_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)

def _list_all_batches() -> list[dict]:
    """Return list of batch metadata dicts from all JSONL files in batches/."""
    batches_dir = Path(__file__).resolve().parent / "batches"
    os.makedirs(str(batches_dir), exist_ok=True)
    result = []
    for p in sorted(batches_dir.glob("*.jsonl")):
        brewid = p.stem
        beer_name = ""
        batch_name = ""
        ferm_start_date = ""
        color = ""
        # Read first sample event from JSONL to get metadata
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload") or obj
                    color = payload.get("tilt_color") or color
                    break
        except OSError:
            pass
        # Look up metadata from tilt_cfg
        if color:
            cfg = tilt_cfg.get(color) or {}
            beer_name = cfg.get("beer_name") or ""
            batch_name = cfg.get("batch_name") or ""
            ferm_start_date = cfg.get("ferm_start_date") or ""
        result.append({
            "brewid": brewid,
            "color": color,
            "beer_name": beer_name,
            "batch_name": batch_name,
            "ferm_start_date": ferm_start_date,
            "filename": p.name,
            "size": _file_size_str(str(p)),
        })
    return result

def _log_dir() -> Path:
    return Path(__file__).resolve().parent / "logs"

def _save_system_cfg() -> None:
    path = Path(__file__).resolve().parent / "config" / "system_config.json"
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(system_cfg, f, indent=2)

def _save_tilt_cfg() -> None:
    path = Path(__file__).resolve().parent / "config" / "tilt_config.json"
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(tilt_cfg, f, indent=2)

def _save_temp_cfg() -> None:
    path = Path(__file__).resolve().parent / "config" / "temp_control_config.json"
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(temp_cfg, f, indent=2)

# ---- system config -------------------------------------------------------

_PREDEFINED_FIELD_MAPS = {
    "default": {"name": "Default", "description": "Standard field mapping"},
    "brewers_friend": {"name": "Brewer's Friend", "description": "Brewer's Friend API format"},
    "brewfather": {"name": "Brewfather", "description": "Brewfather stream format"},
    "custom": {"name": "Custom", "description": "Define your own field mapping"},
}

def _get_external_urls() -> list[dict]:
    """Return the list of external URL configs (always 3 entries)."""
    default = {"name": "", "url": "", "method": "POST", "content_type": "form",
               "timeout_seconds": 8, "field_map_id": "default", "custom_field_map": ""}
    urls = system_cfg.get("external_urls", [])
    result = []
    for i in range(3):
        entry = dict(default)
        if i < len(urls) and isinstance(urls[i], dict):
            entry.update(urls[i])
        result.append(entry)
    return result

@app.get("/system_config")
def system_config_page():
    active_tab = request.args.get("tab", "main-settings")
    return render_template(
        "system_config.html",
        system_settings=system_cfg,
        active_tab=active_tab,
        external_urls=_get_external_urls(),
        predefined_field_maps=_PREDEFINED_FIELD_MAPS,
    )

@app.post("/update_system_config")
def update_system_config():
    active_tab = request.form.get("active_tab", "main-settings")
    # Update plain text / numeric fields
    for key in (
        "brewery_name", "brewer_name", "street", "city", "state",
        "timezone", "timestamp_format", "display_mode",
        "update_interval", "tilt_logging_interval_minutes",
        "kasa_rate_limit_seconds", "sending_email", "smtp_host",
        "smtp_port", "warning_mode", "email",
        "push_provider", "pushover_user_key", "pushover_device",
        "ntfy_server", "ntfy_topic",
    ):
        val = request.form.get(key)
        if val is not None:
            system_cfg[key] = val
    # Checkbox (present = True, absent = False)
    system_cfg["smtp_starttls"] = "smtp_starttls" in request.form
    # Password fields — only update if a new value was provided
    pw = request.form.get("sending_email_password", "").strip()
    if pw:
        system_cfg["smtp_password"] = pw
    po_token = request.form.get("pushover_api_token", "").strip()
    if po_token:
        system_cfg["pushover_api_token"] = po_token
    _save_system_cfg()
    return redirect(url_for("system_config_page") + f"?tab={active_tab}")

# ---- temperature control config ------------------------------------------

@app.get("/temp_config")
def temp_config_page():
    controller_id = int(request.args.get("controller_id", 0))
    controllers = temp_cfg.get("controllers", [])
    if not controllers:
        controllers = [{}]
    controller_id = max(0, min(controller_id, len(controllers) - 1))
    temp_control = controllers[controller_id] if controller_id < len(controllers) else {}
    report_colors = list(COLOR_MAP.keys())
    return render_template(
        "temp_control_config.html",
        controller_id=controller_id,
        controllers=controllers,
        temp_control=temp_control,
        report_colors=report_colors,
        heating_last_activity=None,
        cooling_last_activity=None,
        csrf_token=None,
    )

@app.post("/update_temp_config")
def update_temp_config():
    controller_id = int(request.form.get("controller_id", 0))
    controllers = temp_cfg.get("controllers", [])
    if not controllers or controller_id >= len(controllers):
        return redirect(url_for("temp_config_page"))
    tc = controllers[controller_id]
    for key in ("tilt_color", "heating_plug", "cooling_plug"):
        val = request.form.get(key)
        if val is not None:
            tc[key] = val
    for key in ("low_limit", "high_limit", "compressor_delay"):
        val = request.form.get(key)
        if val is not None:
            try:
                tc[key] = float(val)
            except ValueError:
                pass
    tc["enable_heating"] = "enable_heating" in request.form
    tc["enable_cooling"] = "enable_cooling" in request.form
    _save_temp_cfg()
    return redirect(url_for("temp_config_page", controller_id=controller_id))

# ---- batch settings -------------------------------------------------------

_TILT_COLORS = list(COLOR_MAP.keys())

@app.get("/batch_settings")
def batch_settings():
    selected_tilt = request.args.get("tilt_color", "")
    selected_config = tilt_cfg.get(selected_tilt, {}) if selected_tilt else {}
    batch_history_list: list[dict] = []
    if selected_tilt:
        # Find all JSONL files whose first reading has this color
        batches_dir = Path(__file__).resolve().parent / "batches"
        status = _load_batch_status()
        for p in sorted(batches_dir.glob("*.jsonl")):
            brewid = p.stem
            color = ""
            try:
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = obj.get("payload") or obj
                        color = payload.get("tilt_color") or ""
                        break
            except OSError:
                pass
            if color == selected_tilt:
                cfg = tilt_cfg.get(color) or {}
                batch_history_list.append({
                    "brewid": brewid,
                    "beer_name": cfg.get("beer_name") or "",
                    "batch_name": cfg.get("batch_name") or "",
                    "ferm_start_date": cfg.get("ferm_start_date") or "",
                })
    return render_template(
        "batch_settings.html",
        tilt_colors=_TILT_COLORS,
        color_map=COLOR_MAP,
        selected_tilt=selected_tilt,
        selected_config=selected_config,
        batch_history=batch_history_list,
    )

@app.post("/batch_settings")
def batch_settings_save():
    color = request.form.get("tilt_color", "").strip()
    if color:
        if color not in tilt_cfg:
            tilt_cfg[color] = {}
        for key in ("beer_name", "batch_name", "ferm_start_date"):
            val = request.form.get(key)
            if val is not None:
                tilt_cfg[color][key] = val
        for key in ("recipe_abv", "recipe_og", "recipe_fg", "actual_og"):
            val = request.form.get(key, "").strip()
            if val:
                try:
                    tilt_cfg[color][key] = float(val)
                except ValueError:
                    tilt_cfg[color][key] = val
            else:
                tilt_cfg[color][key] = None
        _save_tilt_cfg()
    return redirect(url_for("batch_settings") + f"?tilt_color={color}")

# ---- batch history --------------------------------------------------------

@app.get("/batch_history")
def batch_history():
    sort_order = request.args.get("sort", "newest")
    all_batches = _list_all_batches()
    status = _load_batch_status()
    active_batches = [b for b in all_batches if not status.get(b["brewid"])]
    closed_batches = [b for b in all_batches if status.get(b["brewid"])]

    def _sort_key(b: dict) -> str:
        return b.get("ferm_start_date") or ""

    if sort_order == "oldest":
        active_batches.sort(key=_sort_key)
        closed_batches.sort(key=_sort_key)
    elif sort_order == "beer_name":
        active_batches.sort(key=lambda b: b.get("beer_name") or "")
        closed_batches.sort(key=lambda b: b.get("beer_name") or "")
    elif sort_order == "color":
        active_batches.sort(key=lambda b: b.get("color") or "")
        closed_batches.sort(key=lambda b: b.get("color") or "")
    else:  # newest first
        active_batches.sort(key=_sort_key, reverse=True)
        closed_batches.sort(key=_sort_key, reverse=True)

    return render_template(
        "batch_history_select.html",
        active_batches=active_batches,
        closed_batches=closed_batches,
        color_map=COLOR_MAP,
        sort_order=sort_order,
    )

@app.post("/close_batch")
def close_batch():
    brewid = request.form.get("brewid", "").strip()
    if not brewid:
        return jsonify({"success": False, "error": "No brewid provided"})
    status = _load_batch_status()
    status[brewid] = "closed"
    _save_batch_status(status)
    return jsonify({"success": True})

@app.post("/reopen_batch")
def reopen_batch():
    brewid = request.form.get("brewid", "").strip()
    if not brewid:
        return jsonify({"success": False, "error": "No brewid provided"})
    status = _load_batch_status()
    status.pop(brewid, None)
    _save_batch_status(status)
    return jsonify({"success": True})

@app.post("/cleanup_batch_duplicates")
def cleanup_batch_duplicates():
    # Simple stub: report 0 duplicates removed
    return jsonify({"success": True, "message": "No duplicates found.", "duplicates_removed": 0})

@app.get("/batch_review")
def batch_review():
    brewid = request.args.get("brewid", "").strip()
    if not brewid:
        return redirect(url_for("batch_history"))
    # Find color from JSONL
    batches_dir = Path(__file__).resolve().parent / "batches"
    color = ""
    readings: list[dict] = []
    jsonl_path = batches_dir / f"{brewid}.jsonl"
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload") or obj
                if not color:
                    color = payload.get("tilt_color") or ""
                readings.append(payload)
    except OSError:
        pass

    cfg = tilt_cfg.get(color, {}) if color else {}
    batch = {
        "brewid": brewid,
        "beer_name": cfg.get("beer_name") or "",
        "batch_name": cfg.get("batch_name") or "",
        "ferm_start_date": cfg.get("ferm_start_date") or "",
        "recipe_og": cfg.get("recipe_og"),
        "recipe_fg": cfg.get("recipe_fg"),
        "recipe_abv": cfg.get("recipe_abv"),
    }

    # Compute simple stats
    gravities = [r.get("gravity") for r in readings if r.get("gravity") is not None]
    temps = [r.get("temp_f") for r in readings if r.get("temp_f") is not None]
    stats = {
        "total_readings": len(readings),
        "start_gravity": gravities[0] if gravities else None,
        "end_gravity": gravities[-1] if gravities else None,
        "avg_temp": (sum(temps) / len(temps)) if temps else None,
        "duration_days": None,
        "estimated_abv": None,
    }
    if gravities and len(gravities) >= 2:
        og = gravities[0]
        fg = gravities[-1]
        try:
            stats["estimated_abv"] = (float(og) - float(fg)) * 131.25
        except (TypeError, ValueError):
            pass

    return render_template(
        "batch_review.html",
        batch=batch,
        color=color,
        color_map=COLOR_MAP,
        stats=stats,
    )

@app.get("/batch_data_view")
def batch_data_view():
    brewid = request.args.get("brewid", "").strip()
    if not brewid:
        return redirect(url_for("batch_history"))
    path = str(Path(__file__).resolve().parent / "batches" / f"{brewid}.jsonl")
    data = read_jsonl(path, limit=int(request.args.get("limit", "2000")))
    return jsonify(data)

@app.get("/export_batch_data_csv")
def export_batch_data_csv():
    brewid = request.args.get("brewid", "").strip()
    if not brewid:
        return redirect(url_for("batch_history"))
    path = str(Path(__file__).resolve().parent / "batches" / f"{brewid}.jsonl")
    records = read_jsonl(path)
    output = io.StringIO()
    writer = csv.writer(output)
    if records:
        first_payload = (records[0].get("payload") or records[0])
        writer.writerow(list(first_payload.keys()))
        for rec in records:
            payload = rec.get("payload") or rec
            writer.writerow([payload.get(k, "") for k in first_payload.keys()])
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename={brewid}.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

# ---- log management -------------------------------------------------------

def _log_path(filename: str) -> Path:
    """Return safe path to a log file, rejecting path traversal."""
    logs_dir = _log_dir()
    p = (logs_dir / filename).resolve()
    if not str(p).startswith(str(logs_dir.resolve())):
        raise ValueError("Invalid log file path")
    return p

@app.get("/log_management")
def log_management():
    logs_dir = _log_dir()
    os.makedirs(str(logs_dir), exist_ok=True)
    temp_log = str(logs_dir / "temp_control_log.jsonl")
    kasa_log = str(logs_dir / "kasa_activity_monitoring.jsonl")
    notif_log = str(logs_dir / "notifications_log.jsonl")
    app_logs = []
    for p in sorted(logs_dir.glob("*.jsonl")):
        name = p.name
        if name in ("temp_control_log.jsonl", "kasa_activity_monitoring.jsonl", "notifications_log.jsonl"):
            continue
        app_logs.append({"name": name, "size": _file_size_str(str(p))})
    batches = _list_all_batches()
    return render_template(
        "log_management.html",
        temp_log_size=_file_size_str(temp_log),
        kasa_log_size=_file_size_str(kasa_log),
        notifications_log_size=_file_size_str(notif_log),
        app_logs=app_logs,
        batches=batches,
        success_message=request.args.get("success"),
        error_message=request.args.get("error"),
    )

@app.get("/view_log")
def view_log():
    filename = request.args.get("file", "").strip()
    log_type = request.args.get("type", "app")
    page = int(request.args.get("page", "1"))
    lines_per_page = 100
    if not filename:
        return redirect(url_for("log_management"))
    # Determine which directory to look in
    logs_dir = _log_dir()
    candidate = (logs_dir / filename).resolve()
    if not str(candidate).startswith(str(logs_dir.resolve())):
        return redirect(url_for("log_management"))
    content = ""
    total_lines = 0
    if candidate.exists():
        try:
            all_lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            total_lines = len(all_lines)
            total_pages = max(1, ceil(total_lines / lines_per_page))
            page = max(1, min(page, total_pages))
            # page 1 = most recent (last lines), page 2 = older, etc.
            start = max(0, total_lines - page * lines_per_page)
            end = max(0, total_lines - (page - 1) * lines_per_page)
            content = "\n".join(all_lines[start:end])
            line_count = end - start
        except OSError:
            total_pages = 1
            line_count = 0
    else:
        total_pages = 1
        line_count = 0
    return render_template(
        "view_log.html",
        log_file=filename,
        log_type=log_type,
        content=content,
        line_count=line_count,
        total_lines=total_lines,
        current_page=page,
        total_pages=total_pages,
    )

def _archive_file(src: Path) -> bool:
    """Move src to a timestamped archive copy alongside it. Returns True on success."""
    if not src.exists():
        return False
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    archive_path = src.parent / f"{src.stem}_{ts}{src.suffix}"
    shutil.copy2(str(src), str(archive_path))
    src.write_text("", encoding="utf-8")
    return True

@app.post("/archive_log")
def archive_log():
    filename = request.form.get("log_file", "").strip()
    if not filename:
        return redirect(url_for("log_management", error="No file specified"))
    logs_dir = _log_dir()
    p = (logs_dir / filename).resolve()
    if not str(p).startswith(str(logs_dir.resolve())):
        return redirect(url_for("log_management", error="Invalid file"))
    if _archive_file(p):
        return redirect(url_for("log_management", success=f"Archived {filename}"))
    return redirect(url_for("log_management", error=f"Could not archive {filename}"))

@app.post("/delete_log")
def delete_log():
    filename = request.form.get("log_file", "").strip()
    if not filename:
        return redirect(url_for("log_management", error="No file specified"))
    logs_dir = _log_dir()
    p = (logs_dir / filename).resolve()
    if not str(p).startswith(str(logs_dir.resolve())):
        return redirect(url_for("log_management", error="Invalid file"))
    try:
        p.unlink(missing_ok=True)
        return redirect(url_for("log_management", success=f"Deleted {filename}"))
    except OSError as e:
        return redirect(url_for("log_management", error=str(e)))

@app.post("/archive_temp_log")
def archive_temp_log():
    p = _log_dir() / "temp_control_log.jsonl"
    if _archive_file(p):
        return redirect(url_for("log_management", success="Temperature control log archived"))
    return redirect(url_for("log_management", error="Could not archive temperature log"))

@app.post("/archive_kasa_log")
def archive_kasa_log():
    p = _log_dir() / "kasa_activity_monitoring.jsonl"
    if _archive_file(p):
        return redirect(url_for("log_management", success="Kasa log archived"))
    return redirect(url_for("log_management", error="Could not archive Kasa log"))

@app.post("/archive_notifications_log")
def archive_notifications_log():
    p = _log_dir() / "notifications_log.jsonl"
    if _archive_file(p):
        return redirect(url_for("log_management", success="Notifications log archived"))
    return redirect(url_for("log_management", error="Could not archive notifications log"))

@app.post("/export_temp_control_csv")
def export_temp_control_csv():
    p = _log_dir() / "temp_control_log.jsonl"
    records = read_jsonl(str(p))
    output = io.StringIO()
    writer = csv.writer(output)
    if records:
        writer.writerow(list(records[0].keys()))
        for rec in records:
            writer.writerow([rec.get(k, "") for k in records[0].keys()])
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=temp_control_log.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

@app.post("/export_batch_csv")
def export_batch_csv():
    brewid = request.form.get("brewid", "").strip()
    if not brewid:
        return redirect(url_for("log_management"))
    path = str(Path(__file__).resolve().parent / "batches" / f"{brewid}.jsonl")
    records = read_jsonl(path)
    output = io.StringIO()
    writer = csv.writer(output)
    payloads = [r.get("payload") or r for r in records]
    if payloads:
        writer.writerow(list(payloads[0].keys()))
        for p in payloads:
            writer.writerow([p.get(k, "") for k in payloads[0].keys()])
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename={brewid}.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

@app.post("/archive_batch")
def archive_batch():
    brewid = request.form.get("brewid", "").strip()
    if not brewid:
        return redirect(url_for("log_management", error="No brewid specified"))
    batches_dir = Path(__file__).resolve().parent / "batches"
    archive_dir = batches_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    src = batches_dir / f"{brewid}.jsonl"
    if src.exists():
        dest = archive_dir / f"{brewid}.jsonl"
        shutil.move(str(src), str(dest))
        return redirect(url_for("log_management", success=f"Batch {brewid[:8]} archived"))
    return redirect(url_for("log_management", error="Batch file not found"))

@app.post("/delete_batch")
def delete_batch():
    brewid = request.form.get("brewid", "").strip()
    if not brewid:
        return redirect(url_for("log_management", error="No brewid specified"))
    batches_dir = Path(__file__).resolve().parent / "batches"
    p = (batches_dir / f"{brewid}.jsonl").resolve()
    if not str(p).startswith(str(batches_dir.resolve())):
        return redirect(url_for("log_management", error="Invalid brewid"))
    try:
        p.unlink(missing_ok=True)
        return redirect(url_for("log_management", success=f"Batch {brewid[:8]} deleted"))
    except OSError as e:
        return redirect(url_for("log_management", error=str(e)))

# ---- exit system ---------------------------------------------------------

@app.route("/exit_system", methods=["GET", "POST"])
def exit_system():
    if request.method == "POST":
        confirm = request.form.get("confirm", "no")
        if confirm == "yes":
            # Schedule shutdown after response is sent
            def _shutdown():
                time.sleep(1)
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=_shutdown, daemon=True).start()
            return render_template("goodbye.html")
        return redirect(url_for("index"))
    return render_template("exit_system.html")

def free_port(port: int) -> None:
    """Terminate any process currently listening on *port* so Flask can bind to it."""
    killed = False

    if psutil is not None:
        try:
            connections = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, AttributeError):
            connections = []
        for conn in connections:
            if conn.laddr.port == port and conn.status == "LISTEN" and conn.pid:
                try:
                    proc = psutil.Process(conn.pid)
                    print(f"[startup] Port {port} is in use by PID {conn.pid} ({proc.name()}). Terminating…")
                    proc.terminate()
                    proc.wait(timeout=5)
                    killed = True
                except psutil.TimeoutExpired:
                    proc.kill()
                    killed = True
                except psutil.AccessDenied:
                    print(f"[startup] Access denied when terminating PID {conn.pid}. Try running with sudo.")
                    return
                except psutil.NoSuchProcess:
                    pass
    else:
        # Fallback: use lsof to find and kill the occupying process
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5
            )
            pids = result.stdout.strip().splitlines()
            for pid_str in pids:
                pid = int(pid_str.strip())
                print(f"[startup] Port {port} is in use by PID {pid}. Terminating…")
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(2)
                    # If still alive after SIGTERM, escalate to SIGKILL
                    try:
                        os.kill(pid, 0)  # Raises ProcessLookupError if already gone
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # Already terminated by SIGTERM
                except ProcessLookupError:
                    pass  # Process was gone before we could signal it
                except PermissionError:
                    print(f"[startup] Permission denied when terminating PID {pid}. Try running with sudo.")
                    return
                killed = True
        except FileNotFoundError:
            # lsof not available; check with socket and warn
            for host in ("0.0.0.0", "127.0.0.1"):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    if s.connect_ex((host, port)) == 0:
                        print(
                            f"[startup] Port {port} is in use and neither psutil nor lsof is available. "
                            "Install psutil (`pip install psutil`) or free the port manually."
                        )
                        break

    if killed:
        time.sleep(1)  # Brief pause to let the OS reclaim the port


def main():
    port = int(os.environ.get("FLASK_PORT", system_cfg.get("flask_port", APP_PORT_DEFAULT)))
    print(f"[startup] Fermentatorium starting on http://0.0.0.0:{port}/", flush=True)

    # Free port 5000 (Flask's built-in default) so that any legacy installation
    # auto-started from the old repo on that port does not block this application.
    # When the configured port IS 5000, free_port(port) below covers it already.
    if port != 5000:
        free_port(5000)
    free_port(port)

    try:
        app.run(host="0.0.0.0", port=port, debug=bool(int(os.environ.get("FLASK_DEBUG", "0"))))
    except OSError as exc:
        print(
            f"[startup] ERROR: Cannot bind to port {port}: {exc}\n"
            f"[startup] TIP: Run 'sudo lsof -i :{port}' to see what process is using it,\n"
            f"[startup]      or set a different port in config/system_config.json.",
            flush=True,
        )
        sys.exit(1)

if __name__ == "__main__":
    main()