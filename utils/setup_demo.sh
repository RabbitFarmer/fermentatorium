#!/bin/bash
# Setup script for demo fermentation data

set -e

echo "=========================================="
echo "Three Control - Demo Setup"
echo "=========================================="
echo ""

if [ ! -f "app.py" ]; then
    echo "Error: Please run this script from the threecontrol root directory"
    exit 1
fi

echo "Setting up configuration files..."

if [ ! -f "config/tilt_config.json" ]; then
    echo "  Creating config/tilt_config.json from template..."
    cp config/tilt_config.json.template config/tilt_config.json
    
    python3 -c "
import json

with open('config/tilt_config.json', 'r') as f:
    config = json.load(f)

config['Black'] = {
    'beer_name': '803 Blonde Ale Clone of 805',
    'batch_name': 'Demo Batch',
    'ferm_start_date': '12/25/2025',
    'recipe_og': '1.050',
    'recipe_fg': '1.010',
    'recipe_abv': '5.2',
    'actual_og': '1.049',
    'brewid': 'cf38d0a8',
    'og_confirmed': True,
    'notification_state': {
        'fermentation_start_datetime': '2025-12-25T14:27:59Z',
        'fermentation_completion_datetime': None,
        'last_daily_report': None
    }
}

with open('config/tilt_config.json', 'w') as f:
    json.dump(config, f, indent=2)

print('  Black tilt configured with demo data')
"
else
    echo "  config/tilt_config.json already exists"
fi

if [ ! -f "config/system_config.json" ]; then
    echo "  Creating config/system_config.json from template..."
    cp config/system_config.json.template config/system_config.json
fi

# Always ensure the brewery name is set correctly for the demo
python3 -c "
import json
path = 'config/system_config.json'
with open(path, 'r') as f:
    cfg = json.load(f)
cfg['brewery_name'] = 'The Tilt Fermentatorium'
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
print('  Brewery name set to: The Tilt Fermentatorium')
"

if [ ! -f "config/temp_control_config.json" ]; then
    echo "  Creating config/temp_control_config.json from template..."
    cp config/temp_control_config.json.template config/temp_control_config.json
fi

echo ""
echo "Generating demo batch data..."
python3 utils/generate_demo_data.py

echo ""
echo "Verifying demo data..."
python3 utils/verify_demo_data.py

echo ""
echo "=========================================="
echo "Demo setup complete!"
echo "=========================================="
echo ""
echo "To view the demo chart:"
echo "  1. Start the Flask app:"
echo "     python3 app.py"
echo ""
echo "  2. Open in your browser:"
echo "     http://localhost:5001/chart_plotly/Black"
echo ""
