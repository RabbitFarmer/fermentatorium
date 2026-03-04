"""
BrewID utilities for tiltcontrlmonitor:
- Centralized BrewID generation and parsing

BrewID format: '{batch_id}-{color_code}-{model_code}-{mac_short}'
  - batch_id: user/batch-defined string
  - color_code: first 3 characters of Tilt color (lowercase)
  - model_code: 1=Pro/Mini-Pro (low value > 5000), 0=Standard
  - mac_short: last 4 hex chars of MAC (uppercase, no colons)
"""

import re

def make_brewid(batch_id, color, model_code, mac):
    """
    Create an expanded BrewID using batch_id, color, model_code, and last 4 of MAC.
    """
    color_code = color[:3].lower()
    mac_clean = mac.replace(':', '').replace('-', '').upper()
    mac_short = mac_clean[-4:]
    return f"{batch_id}-{color_code}-{model_code}-{mac_short}"

def parse_brewid(brewid):
    """
    Parse a BrewID into its components.
    Returns dict with batch_id, color_code, model_code, mac_short
    """
    match = re.match(r"(.+)-([a-z]{2,3})-(\d+)-([0-9A-F]{4})$", brewid)
    if not match:
        raise ValueError("Invalid BrewID format!")
    return {
        "batch_id": match.group(1),
        "color_code": match.group(2),
        "model_code": int(match.group(3)),
        "mac_short": match.group(4)
    }