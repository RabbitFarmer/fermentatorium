#!/usr/bin/env python3
"""
Verify and visualize the demo fermentation data for the Black tilt.
"""

import json
import os
from datetime import datetime

BATCHES_DIR = 'batches'
BREWID = 'cf38d0a8'
TILT_COLOR = 'Black'

def main():
    batch_file = os.path.join(BATCHES_DIR, f'{BREWID}.jsonl')
    
    if not os.path.exists(batch_file):
        print(f"Error: Batch file not found: {batch_file}")
        return 1
    
    entries = []
    with open(batch_file, 'r') as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))
    
    metadata = entries[0] if entries and entries[0].get('event') == 'batch_metadata' else None
    samples = [e for e in entries if e.get('event') == 'sample']
    
    print("=" * 80)
    print("DEMO FERMENTATION DATA - BLACK TILT")
    print("=" * 80)
    print()
    
    if metadata:
        payload = metadata.get('payload', {})
        meta = payload.get('meta', {})
        print(f"Beer Name:    {meta.get('beer_name', 'N/A')}")
        print(f"Batch Name:   {meta.get('batch_name', 'N/A')}")
        print(f"Brew ID:      {payload.get('brewid', 'N/A')}")
        print()
    
    print(f"Total Sample Points: {len(samples)}")
    print()
    
    if samples:
        first = samples[0]['payload']
        last = samples[-1]['payload']
        
        print(f"Fermentation Start:")
        print(f"  Date/Time: {first.get('timestamp', 'N/A')}")
        print(f"  Gravity:   {first.get('gravity', 'N/A')}")
        print(f"  Temp (F):  {first.get('temp_f', 'N/A')}")
        print()
        
        print(f"Fermentation End:")
        print(f"  Date/Time: {last.get('timestamp', 'N/A')}")
        print(f"  Gravity:   {last.get('gravity', 'N/A')}")
        print(f"  Temp (F):  {last.get('temp_f', 'N/A')}")
        print()
        
        gravity_drop = first.get('gravity', 0) - last.get('gravity', 0)
        print(f"Total Gravity Drop: {gravity_drop:.3f}")
        
        ABV_MULTIPLIER = 131.25
        abv = gravity_drop * ABV_MULTIPLIER
        print(f"Estimated ABV:      {abv:.1f}%")
        print()
    
    print("=" * 80)
    print()
    print("Demo data is ready for chart visualization!")
    print()
    print("To view the chart:")
    print("  1. Start the Flask app: python3 app.py")
    print(f"  2. Navigate to: http://localhost:5001/chart_plotly/{TILT_COLOR}")
    print()
    
    return 0

if __name__ == '__main__':
    exit(main())
