from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


DEFAULT_TILT_TABLE_PATH = "config/tilt_table.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_mac(mac: str) -> str:
    return (mac or "").strip().upper()


@dataclass
class TiltDeviceRecord:
    mac: str
    tilt_color: str
    uuid: str

    # user/enrichment fields
    tilt_type: str = "unknown"  # "standard" | "pro" | "mini-pro" | "unknown"
    device_label: str = ""

    # auto-maintained fields
    first_seen: str = ""
    last_seen: str = ""
    rssi_last: Optional[int] = None
    last_temp_f: Optional[float] = None
    last_gravity: Optional[float] = None

    # calibration variances — stored per physical device so they survive color changes
    temp_variance: float = 0.0
    gravity_variance: float = 0.0


def load_tilt_table(path: str = DEFAULT_TILT_TABLE_PATH) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f) or {}


def save_tilt_table(table: Dict[str, Dict[str, Any]], path: str = DEFAULT_TILT_TABLE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def upsert_device_from_reading(
    table: Dict[str, Dict[str, Any]],
    *,
    mac: str,
    tilt_color: str,
    uuid: str,
    rssi: Optional[int],
    temp_f: Optional[float],
    gravity: Optional[float],
) -> Dict[str, Any]:
    mac_n = normalize_mac(mac)
    now = _utc_now_iso()

    rec = table.get(mac_n)
    if rec is None:
        rec_obj = TiltDeviceRecord(
            mac=mac_n,
            tilt_color=tilt_color,
            uuid=uuid,
            first_seen=now,
            last_seen=now,
            rssi_last=rssi,
            last_temp_f=temp_f,
            last_gravity=gravity,
        )
        rec = asdict(rec_obj)
        table[mac_n] = rec
    else:
        rec.setdefault("mac", mac_n)
        rec["tilt_color"] = tilt_color
        rec["uuid"] = uuid
        rec["last_seen"] = now
        rec.setdefault("first_seen", now)
        rec.setdefault("tilt_type", "unknown")
        rec.setdefault("device_label", "")

        if rssi is not None:
            rec["rssi_last"] = rssi
        if temp_f is not None:
            rec["last_temp_f"] = temp_f
        if gravity is not None:
            rec["last_gravity"] = gravity
        rec.setdefault("temp_variance",    0.0)
        rec.setdefault("gravity_variance", 0.0)

    return rec


def set_device_variances(
    table: Dict[str, Dict[str, Any]],
    *,
    mac: str,
    temp_variance: float,
    gravity_variance: float,
) -> None:
    """Persist calibration variances onto the tilt_table record for this MAC."""
    mac_n = normalize_mac(mac)
    if mac_n and mac_n in table:
        table[mac_n]["temp_variance"]    = temp_variance
        table[mac_n]["gravity_variance"] = gravity_variance


def get_device_variances(
    table: Dict[str, Dict[str, Any]],
    *,
    mac: str,
) -> tuple[float, float]:
    """Return (temp_variance, gravity_variance) for a MAC, or (0.0, 0.0) if not found."""
    mac_n = normalize_mac(mac)
    if mac_n and mac_n in table:
        rec = table[mac_n]
        return (
            float(rec.get("temp_variance",    0) or 0),
            float(rec.get("gravity_variance", 0) or 0),
        )
    return (0.0, 0.0)