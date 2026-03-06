# Demo Data Import

This directory contains utilities for importing fermentation data into the Fermenter Temperature Controller system.

## Overview

The demo data has been imported for the **Black tilt** to showcase a complete fermentation cycle:

- **Beer**: 803 Blonde Ale Clone of 805
- **Batch**: Demo Batch  
- **Brew ID**: cf38d0a8
- **Duration**: ~15 days (Dec 25, 2025 - Jan 9, 2026)
- **Starting Gravity**: 1.049
- **Final Gravity**: 1.004
- **Estimated ABV**: 5.9%

## Import Script

### `import_brewers_friend.py`

Converts Brewer's Friend JSON export format to the internal JSONL format.

**Usage:**

```bash
python3 utils/import_brewers_friend.py data.json \
    --color Black \
    --beer-name "Beer Name" \
    --batch-name "Batch Name"
```

## Verification Script

### `verify_demo_data.py`

Displays the imported demo data and verifies it's ready for chart visualization.

**Usage:**

```bash
python3 utils/verify_demo_data.py
```

## Viewing the Chart

1. Start the Flask application:
   ```bash
   python3 app3.py
   ```

2. Open your browser to:
   ```
   http://localhost:5001/chart_plotly/Black
   ```

## Notes

- All data in this system is for demonstration purposes only
- The import script preserves timestamps from the original Brewer's Friend export
- RSSI is set to a default value of -70 since it's not included in BF exports
