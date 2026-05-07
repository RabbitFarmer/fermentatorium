#!/usr/bin/env python3
"""
Generate demo fermentation data for the Black tilt (brewid: cf38d0a8).

Creates batches/cf38d0a8.jsonl with a realistic 14-day fermentation curve
so the demo chart, batch review, and verify steps all work correctly.
"""

import json
import math
import os
import random
from datetime import datetime, timedelta

BATCHES_DIR = 'batches'
BREWID = 'cf38d0a8'
TILT_COLOR = 'Black'
BEER_NAME = '803 Blonde Ale Clone of 805'
BATCH_NAME = 'Demo Batch'
START_TIME = datetime(2025, 12, 25, 14, 27, 59)
OG = 1.049
FG = 1.010
FERMENT_DAYS = 14
INTERVAL_MINUTES = 15


def main():
    os.makedirs(BATCHES_DIR, exist_ok=True)
    output_path = os.path.join(BATCHES_DIR, f'{BREWID}.jsonl')

    if os.path.exists(output_path):
        print(f"  Demo batch data already exists: {output_path}")
        return 0

    random.seed(42)  # reproducible demo data

    entries = []

    # Metadata header
    entries.append({
        "event": "batch_metadata",
        "payload": {
            "tilt_color": TILT_COLOR,
            "brewid": BREWID,
            "created_date": "12252025",
            "meta": {
                "beer_name": BEER_NAME,
                "batch_name": BATCH_NAME,
                "recipe_og": "1.050",
                "recipe_fg": "1.010",
                "recipe_abv": "5.2",
                "actual_og": "1.049",
                "ferm_start_date": "12/25/2025",
            }
        }
    })

    # Generate sample readings every INTERVAL_MINUTES for FERMENT_DAYS
    total_intervals = (FERMENT_DAYS * 24 * 60) // INTERVAL_MINUTES

    for i in range(total_intervals + 1):
        t = START_TIME + timedelta(minutes=i * INTERVAL_MINUTES)
        progress = i / total_intervals  # 0.0 → 1.0

        # Gravity: exponential-style decay (fast early, slow late)
        gravity = FG + (OG - FG) * math.exp(-5.0 * progress)
        gravity = round(max(FG, gravity), 4)

        # Temperature: ramp from 65 °F to 68 °F over first two days, then hold
        temp_base = 65.0 + 3.0 * min(1.0, progress * (FERMENT_DAYS / 2))
        temp_f = round(temp_base + random.uniform(-0.5, 0.5), 1)

        rssi = random.randint(-80, -65)

        entries.append({
            "event": "sample",
            "payload": {
                "timestamp": t.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "gravity": gravity,
                "temp_f": temp_f,
                "rssi": rssi,
                "tilt_color": TILT_COLOR,
            }
        })

    with open(output_path, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry) + '\n')

    sample_count = len(entries) - 1  # subtract metadata row
    print(f"  Generated {sample_count} sample readings → {output_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
