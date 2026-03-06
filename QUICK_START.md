# Quick Start - Temperature Controller Tracing

## Problem
- Brown temp card showing incorrectly on main display
- Temp settings screen shows single controller (should show 3)

## Solution
Added comprehensive tracing to help you find the problem quickly!

## 3-Step Quick Start

### Step 1: Start the App and Check Server Console

```bash
python3 app3.py
```

**Look for these traces**:
```
[TRACE] Loaded temp config from config/temp_control_config.json
[TRACE] temp_cfg_raw has 'controllers' key: True/False  <- Should be True
[TRACE] Number of controllers in config: X              <- Should be 3
[TRACE] Config already in new format with 3 controllers <- Good!
```

### Step 2: Open Dashboard and Check Browser Console

1. Open http://localhost:5001 in your browser
2. Press **F12** to open Developer Tools
3. Click **Console** tab

**Look for these traces**:
```
[TRACE] maindisplay.html loaded
[TRACE] Controllers data: Array(3) [...]  <- Should have 3 items
[TRACE] Number of controllers: 3          <- Should be 3
```

### Step 3: Check Temperature Settings Page

1. Click "Temperature Control" in navigation
2. Check server console again:
```
[TRACE] temp_config() route called
[TRACE] Number of controllers in temp_cfg: 3  <- Should be 3
```

## Getting Help

See TRACING_GUIDE.md for complete troubleshooting information.

## Cheat Sheet: start.sh and Boot-to-Program Setup

### What start.sh does
`start.sh` automatically navigates to its own directory, activates or creates a Python virtual environment, installs dependencies if needed, and starts `app3.py` in the background.

### Installing start.sh to boot at login (Raspberry Pi Desktop)

```bash
chmod +x ~/threecontrol-/start.sh
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/threecontrol.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Three Controller
Exec=bash /home/pi/threecontrol-/start.sh
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
```

## Cheat Sheet: Full Fresh Installation

### Requirements
- Raspberry Pi 4 or Pi 3B+
- MicroSD card >= 16 GB
- Internet connection

### Step 1 - Flash the OS
Download Raspberry Pi Imager and flash Raspberry Pi OS (64-bit).

### Step 2 - Update the system
```bash
sudo apt update && sudo apt upgrade -y
```

### Step 3 - Install prerequisites
```bash
sudo apt install -y python3 python3-pip python3-venv git bluetooth bluez libbluetooth-dev libglib2.0-dev
```

### Step 4 - Clone the repository
```bash
cd ~
git clone https://github.com/RabbitFarmer/threecontrol-.git
cd threecontrol-
```

### Step 5 - Run the startup script
```bash
chmod +x start.sh
./start.sh
```
