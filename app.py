from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, request, render_template, url_for

from brewid import make_brewid
from storage_jsonl import ensure_dirs, append_sample, read_jsonl, batch_jsonl_path
from logger import log_error
from tilt_static import COLOR_MAP
from tilt_scan_sim import build_sim_fleet, scan_simulated
from tilt_scan_bleak import scan_bleak
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
    # controllers are not wired yet; keep empty list for now
    return jsonify(
        {
            "live_tilts": _build_live_tilts_by_color(),
            "controllers": [],
        }
    )

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

def main():
    port = int(os.environ.get("FLASK_PORT", system_cfg.get("flask_port", APP_PORT_DEFAULT)))
    app.run(host="0.0.0.0", port=port, debug=bool(int(os.environ.get("FLASK_DEBUG", "0"))))

if __name__ == "__main__":
    main()