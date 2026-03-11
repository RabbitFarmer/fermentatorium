from __future__ import annotations

import asyncio
import struct
import time
from typing import Dict, List, Optional

from bleak import BleakScanner


def _uuid_bytes(hex_uuid_no_dashes: str) -> bytes:
    return bytes.fromhex(hex_uuid_no_dashes.replace("-", "").lower())


# Tilt iBeacon UUIDs by color (standard mapping)
TILT_UUID_BY_COLOR: dict[str, bytes] = {
    "Red": _uuid_bytes("a495bb10c5b14b44b5121370f02d74de"),
    "Green": _uuid_bytes("a495bb20c5b14b44b5121370f02d74de"),
    "Black": _uuid_bytes("a495bb30c5b14b44b5121370f02d74de"),
    "Purple": _uuid_bytes("a495bb40c5b14b44b5121370f02d74de"),
    "Orange": _uuid_bytes("a495bb50c5b14b44b5121370f02d74de"),
    "Blue": _uuid_bytes("a495bb60c5b14b44b5121370f02d74de"),
    "Yellow": _uuid_bytes("a495bb70c5b14b44b5121370f02d74de"),
    "Pink": _uuid_bytes("a495bb80c5b14b44b5121370f02d74de"),
}
COLOR_BY_TILT_UUID: dict[bytes, str] = {v: k for k, v in TILT_UUID_BY_COLOR.items()}


def _parse_ibeacon_from_apple_mfg(payload: bytes) -> Optional[dict]:
    """
    Apple (0x004C) iBeacon payload:
      0x02 0x15 + 16B UUID + 2B major + 2B minor + 1B txpower
    """
    if not payload or len(payload) < 23:
        return None
    if payload[0] != 0x02 or payload[1] != 0x15:
        return None

    uuid_bytes = payload[2:18]
    major = int.from_bytes(payload[18:20], "big")
    minor = int.from_bytes(payload[20:22], "big")
    txpower = struct.unpack("b", payload[22:23])[0]
    return {"uuid_bytes": uuid_bytes, "major": major, "minor": minor, "txpower": txpower}


async def _scan_tilts(scan_seconds: float) -> List[dict]:
    found: Dict[str, dict] = {}

    def cb(device, adv):
        mfg = getattr(adv, "manufacturer_data", None) or {}
        apple = mfg.get(76)  # 0x004C Apple
        if not apple:
            return

        pkt = _parse_ibeacon_from_apple_mfg(apple)
        if not pkt:
            return

        tilt_color = COLOR_BY_TILT_UUID.get(pkt["uuid_bytes"])
        if not tilt_color:
            # You said UUID always yields color; keep this guard anyway.
            return

        # Tilt encoding:
        # major = temperature (°F)
        # minor = gravity * 1000
        temp_f = pkt["major"]
        gravity = pkt["minor"] / 1000.0

        addr = getattr(device, "address", None) or ""

# Prefer AdvertisementData.rssi (Bleak deprecates BLEDevice.rssi)
        rssi = getattr(adv, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", None)

        found[addr] = {
            "source": "bleak",
            "mac": addr,
            "rssi": rssi,
            "uuid": pkt["uuid_bytes"].hex(),
            "tilt_color": tilt_color,
            "temp_f": float(temp_f),
            "gravity": float(gravity),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": 1,
        }

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        await asyncio.sleep(scan_seconds)
    finally:
        await scanner.stop()

    return list(found.values())


def scan_bleak(scan_seconds: float = 6.0) -> List[dict]:
    return asyncio.run(_scan_tilts(scan_seconds))