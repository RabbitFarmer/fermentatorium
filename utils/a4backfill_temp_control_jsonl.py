#!/usr/bin/env python3
"""
Backfill temp_control_log.jsonl with tilt reading data from batch files.

Useful when batch data was collected but temp_control logging was not enabled,
or when migrating from an older data format.

Usage:
    python3 utils/a4backfill_temp_control_jsonl.py --color Red --brewid <brewid>
"""
import argparse
import json
import os
from datetime import datetime

BATCHES_DIR = 'batches'
TEMP_CONTROL_LOG = 'temp_control/temp_control_log.jsonl'

def ensure_dir(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def find_batch_file(brewid):
    """Find a batch file matching the given brewid prefix."""
    if not os.path.exists(BATCHES_DIR):
        return None
    for fname in os.listdir(BATCHES_DIR):
        if fname.endswith('.jsonl') and brewid in fname:
            return os.path.join(BATCHES_DIR, fname)
    return None

def backfill(brewid, tilt_color, dry_run=False):
    batch_file = find_batch_file(brewid)
    if not batch_file:
        print(f"No batch file found for brewid: {brewid}")
        return 0

    print(f"Found batch file: {batch_file}")
    
    ensure_dir(TEMP_CONTROL_LOG)
    
    entries_to_add = []
    with open(batch_file, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            event = obj.get('event')
            payload = obj.get('payload', {})
            
            if event == 'sample':
                entry = {
                    "event": "tilt_reading",
                    "timestamp": payload.get('timestamp'),
                    "payload": {
                        "tilt_color": tilt_color,
                        "brewid": brewid,
                        "gravity": payload.get('gravity'),
                        "temp_f": payload.get('temp_f') or payload.get('current_temp'),
                        "rssi": payload.get('rssi', -70),
                    }
                }
                entries_to_add.append(entry)
    
    print(f"Found {len(entries_to_add)} sample entries to backfill")
    
    if dry_run:
        print("DRY RUN - no changes made")
        return len(entries_to_add)
    
    with open(TEMP_CONTROL_LOG, 'a') as f:
        for entry in entries_to_add:
            f.write(json.dumps(entry) + '\n')
    
    print(f"Added {len(entries_to_add)} entries to {TEMP_CONTROL_LOG}")
    return len(entries_to_add)

def main():
    parser = argparse.ArgumentParser(description='Backfill temp_control_log.jsonl from batch files')
    parser.add_argument('--brewid', required=True, help='Brew ID to backfill')
    parser.add_argument('--color', required=True, help='Tilt color')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    args = parser.parse_args()
    
    count = backfill(args.brewid, args.color, dry_run=args.dry_run)
    print(f"Done. Processed {count} entries.")

if __name__ == '__main__':
    main()
