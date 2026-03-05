from __future__ import annotations

import re

_BREWID_RE = re.compile(r"^(?P<batch>.+)-(?P<color>[a-z]{3})-(?P<model>[01])-(?P<mac>[0-9A-F]{4})$")

def normalize_mac(mac: str) -> str:
    mac_clean = mac.replace(":", "").replace("-", "").upper()
    if len(mac_clean) < 4:
        raise ValueError(f"MAC too short: {mac!r}")
    return mac_clean

def mac4(mac: str) -> str:
    return normalize_mac(mac)[-4:]

def color3(color: str) -> str:
    c = (color or "").strip().lower()
    if len(c) < 3:
        raise ValueError(f"color too short: {color!r}")
    return c[:3]

def make_brewid(*, batch_id: str, tilt_color: str, model: int, mac: str) -> str:
    if not batch_id or not str(batch_id).strip():
        raise ValueError("batch_id is required")
    if model not in (0, 1):
        raise ValueError("model must be 0 or 1")
    return f"{batch_id.strip()}-{color3(tilt_color)}-{model}-{mac4(mac)}"

def parse_brewid(brewid: str) -> dict:
    m = _BREWID_RE.match(brewid or "")
    if not m:
        raise ValueError(f"Invalid BrewID: {brewid!r}")
    return {
        "batch_id": m.group("batch"),
        "color3": m.group("color"),
        "model": int(m.group("model")),
        "mac4": m.group("mac"),
    }