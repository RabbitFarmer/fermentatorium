#!/usr/bin/env python3
"""
Archive/compact temp_control/temp_control_log.jsonl:

- Splits tilt_reading entries into batches/<brewid_or_color>_YYYYMMDDTHHMMSS.jsonl
- Rebuilds temp_control/temp_control_log.jsonl keeping:
    * All non-tilt_reading events
    * The last `keep_per_tilt` tilt_reading entries per brewid (or color fallback)
- Backup is created automatically.

Usage:
    # Run from repository root directory
    python3 utils/archive_compact_logs.py --log temp_control/temp_control_log.jsonl --batches batches --keep 1
"""
import argparse
import json
import os
from collections import deque, defaultdict
from datetime import datetime

def ensure_dir(p):
    os.makedirs(p, exist_ok=True)

def archive_split(input_log, batches_dir, keep_per_tilt=1):
    ensure_dir(batches_dir)
    bak = f"{input_log}.{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.bak"
    os.rename(input_log, bak)
    print(f"Backup saved to: {bak}")

    non_tilt_lines = []
    tilt_buffers = defaultdict(lambda: deque(maxlen=keep_per_tilt))

    with open(bak, 'r') as f:
        for line in f:
            line_strip = line.rstrip('\n')
            if not line_strip:
                continue
            try:
                obj = json.loads(line_strip)
            except Exception:
                non_tilt_lines.append(line_strip)
                continue

            event = obj.get('event')
            payload = obj.get('payload') or {}
            if event == 'tilt_reading':
                brewid = payload.get('brewid') or payload.get('brew_id') or ''
                color = payload.get('color') or payload.get('tilt_color') or ''
                key = brewid if brewid else (color if color else 'unknown')
                tilt_buffers[key].append(line_strip)
            else:
                non_tilt_lines.append(line_strip)

    for key, dq in tilt_buffers.items():
        safe_key = key.replace('/', '_').replace(' ', '_') or 'unknown'
        archive_name = f"{safe_key}_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        dest = os.path.join(batches_dir, archive_name)
        with open(dest, 'w') as af:
            for ln in dq:
                af.write(ln + "\n")
        print(f"Wrote archive for {key} -> {dest}")

    with open(input_log, 'w') as out:
        for ln in non_tilt_lines:
            out.write(ln + "\n")
        for key, dq in tilt_buffers.items():
            for ln in dq:
                out.write(ln + "\n")

    print(f"Rebuilt compact log: {input_log}")
    return bak

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--log', default='temp_control/temp_control_log.jsonl')
    p.add_argument('--batches', default='batches')
    p.add_argument('--keep', type=int, default=1, help='number of tilt_reading entries to keep per brew/color in main log')
    args = p.parse_args()

    if not os.path.exists(args.log):
        print("Log file not found:", args.log)
        return

    bak = archive_split(args.log, args.batches, keep_per_tilt=args.keep)
    print("Done. Original log backed up at:", bak)

if __name__ == '__main__':
    main()
