#!/usr/bin/env python3
"""
Import Brewer's Friend JSON data into the fermentation monitoring system.

Usage:
    python3 utils/a4import_brewers_friend.py <input_json> --color <tilt_color> --brewid <brewid>
"""

import argparse
import json
import os
import sys
from datetime import datetime
import hashlib


def generate_brewid(beer_name, batch_name, date_str):
    """Generate brewid from beer name, batch name, and date."""
    id_str = f"{beer_name}-{batch_name}-{date_str}"
    return hashlib.sha256(id_str.encode('utf-8')).hexdigest()[:8]


def parse_timestamp(ts_str):
    """Parse various timestamp formats from Brewer's Friend data."""
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]:
        try:
            dt = datetime.strptime(ts_str.replace('+00:00', '+0000'), fmt)
            if dt.tzinfo:
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                base_time = ts_str.split('.')[0]
                return f"{base_time}Z"
        except ValueError:
            continue
    return ts_str


def convert_brewers_friend_to_jsonl(input_file, tilt_color, brewid, beer_name="", batch_name=""):
    """Convert Brewer's Friend JSON format to internal JSONL format."""
    with open(input_file, 'r') as f:
        bf_data = json.load(f)
    
    if not isinstance(bf_data, list):
        raise ValueError("Expected Brewer's Friend data to be a JSON array")
    
    first_entry = bf_data[0] if bf_data else {}
    first_timestamp = first_entry.get('created_at', '')
    
    try:
        dt = datetime.fromisoformat(first_timestamp.replace('Z', '+00:00'))
        created_date = dt.strftime("%m%d%Y")
    except:
        created_date = datetime.now().strftime("%m%d%Y")
    
    if not beer_name:
        for entry in bf_data:
            if entry.get('beer'):
                beer_name = entry['beer']
                break
    
    metadata_entry = {
        "event": "batch_metadata",
        "payload": {
            "tilt_color": tilt_color,
            "brewid": brewid,
            "created_date": created_date,
            "meta": {
                "beer_name": beer_name,
                "batch_name": batch_name
            }
        }
    }
    
    jsonl_entries = [metadata_entry]
    
    for entry in bf_data:
        gravity = entry.get('gravity')
        temp = entry.get('temp')
        created_at = entry.get('created_at')
        
        if gravity is None or temp is None or created_at is None:
            continue
        
        timestamp = parse_timestamp(created_at)
        
        sample_entry = {
            "event": "sample",
            "payload": {
                "timestamp": timestamp,
                "tilt_color": tilt_color,
                "gravity": float(gravity),
                "temp_f": int(temp),
                "current_temp": float(temp),
                "brewid": brewid,
                "rssi": -70
            }
        }
        
        jsonl_entries.append(sample_entry)
    
    return jsonl_entries


def write_jsonl_to_batch_file(jsonl_entries, output_file):
    """Write JSONL entries to a batch file."""
    os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        for entry in jsonl_entries:
            f.write(json.dumps(entry) + '\n')
    
    print(f"Wrote {len(jsonl_entries)} entries to {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Import Brewer\'s Friend JSON data into Fermenter Temp Controller'
    )
    parser.add_argument('input_file', help='Path to Brewer\'s Friend JSON file')
    parser.add_argument('--color', required=True,
                       help='Tilt color (Black, Blue, Green, Orange, Pink, Purple, Red, Yellow)')
    parser.add_argument('--brewid', help='Brew ID (8 character hex).')
    parser.add_argument('--beer-name', default='', help='Beer name for metadata')
    parser.add_argument('--batch-name', default='', help='Batch name for metadata')
    parser.add_argument('--output-dir', default='batches', help='Output directory for batch files')
    
    args = parser.parse_args()
    
    valid_colors = ['Black', 'Blue', 'Green', 'Orange', 'Pink', 'Purple', 'Red', 'Yellow']
    if args.color not in valid_colors:
        print(f"Error: Invalid tilt color '{args.color}'. Must be one of: {', '.join(valid_colors)}")
        sys.exit(1)
    
    if not args.brewid:
        if not args.beer_name or not args.batch_name:
            print("Error: Either --brewid or both --beer-name and --batch-name must be provided")
            sys.exit(1)
        date_str = datetime.now().strftime("%m%d%Y")
        brewid = generate_brewid(args.beer_name, args.batch_name, date_str)
        print(f"Generated brewid: {brewid}")
    else:
        brewid = args.brewid
    
    print(f"Converting {args.input_file}...")
    try:
        jsonl_entries = convert_brewers_friend_to_jsonl(
            args.input_file, args.color, brewid, args.beer_name, args.batch_name
        )
    except Exception as e:
        print(f"Error converting data: {e}")
        sys.exit(1)
    
    import re
    if args.beer_name:
        safe_beer_name = re.sub(r'[^a-zA-Z0-9_]', '_', args.beer_name)
        if len(jsonl_entries) > 0:
            metadata = jsonl_entries[0]
            created_date = metadata['payload'].get('created_date', datetime.now().strftime("%m%d%Y"))
            if len(created_date) == 8:
                date_yyyymmdd = created_date[4:8] + created_date[0:2] + created_date[2:4]
            else:
                date_yyyymmdd = datetime.now().strftime("%Y%m%d")
        else:
            date_yyyymmdd = datetime.now().strftime("%Y%m%d")
        output_file = os.path.join(args.output_dir, f"{safe_beer_name}_{date_yyyymmdd}_{brewid}.jsonl")
    else:
        output_file = os.path.join(args.output_dir, f"{brewid}.jsonl")
    
    write_jsonl_to_batch_file(jsonl_entries, output_file)
    
    print(f"\nImport complete!")
    print(f"  Tilt Color: {args.color}")
    print(f"  Brew ID: {brewid}")
    print(f"  Output: {output_file}")


if __name__ == '__main__':
    main()
