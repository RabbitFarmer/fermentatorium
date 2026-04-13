### THE TILE FERMENTATORIUM ###

# Tilt Device Fermentation Monitor and Temperature Controller
This project is a Raspberry Pi-based fermentation monitor and temperature controller for homebrewing. It uses Tilt hydrometers and TP-Link Kasa smart plugs to manage and log fermentation temperature with a web dashboard.

> **Privacy Note for New Users:** This repository does not contain any personal data. Your configuration files, batch data, and logs are automatically created on your system and remain private - they are not tracked by git.

## Features

- **Multi-Device Tilt Support**: Track multiple Tilt hydrometers simultaneously for fermentation monitoring
  - Supports standard Tilt, Tilt Pro and / or Tilt Mini-Pro models.
  - Supports all 8 colors of the Tilt Hydrometer (Black, Blue, Green, Orange, Pink, Purple, Red, Yellow)
  - Each Tilt can be independently monitored for fermentation data
  - How many Tilts can be monitored at the same time?  Mix and match models and colors.  You'll run out of tilts before you run out of monitoring capacity. It can monitor every Tilt within the range of the Tilt BLE signals.   - Multiple Tilts of the same color is not a problem.
    
- **Three Independent Temperature Controllers**:
  - Up to 3 Tilts can be selected for double duty:  monitor the fermentation gravity and temperature and also act as fermenter temperature controller.
  - Control up to 3 fermenters simultaneously
  - Each controller can be assigned to a different Tilt color
  - Independent temperature limits (high/low) for each controller
  - Each controller manages its own heating and cooling Kasa smart plugs (15 amp rated)
  - Use 1 TP-Link Kara plug for each function (1 for heating for one fermenter, 1 for cooling for same or another fermenter)
  - Controllers operate completely independently of one another
  - Temperature control UI is integrated directly into each Tilt's display for easy monitoring
    
 **Three Settings Options for each Temperature Controller
  - Set Heating Only: use 1 Kasa plug per fermenter.  Heat starts at low temperature limit and turns off at high limit.
  - Set Cooling Only:  use 1 Kasa plug per fermenter.  Cooling starts at high temperature limit and turns off at lower limit.
  - Set Both Heating and Cooling: use 2 Kasa plugs per fermenter.  Heating starts at low temperature limit and stops at mid-point between low and high limits.  Cooling starts at high limit and stops at mid-point between high and low limits.
  - SAFETY SETTING:  Temperature Controls will shut down if Tilt signal is lost after 2 attempts to read the Tilt.
    
- **Interactive Charting**: Real-time charts for both Tilt-tracked fermentation data and temperature control monitoring
  - Fermentation charts display gravity and temperature trends over time
  - Temperature control charts show heating/cooling events and temperature readings
  - Powered by Plotly for interactive zooming, panning, and data exploration
    
- **CSV Data Export**: Export fermentation batch data and temperature control logs to CSV format for external analysis
- Batch history and temperature logging to JSONL/CSV
  
- Reads Tilt hydrometer data via Bluetooth (BLE).  Range from Tilt is determinant on Tilt signal strength.

- Web dashboard for monitoring and configuration (Flask)
  - Accessible from anywhere on your home network
  - Use a monitor or go headless after initial setup.
    
- Remote access via [Raspberry Pi Connect](https://connect.raspberrypi.com) (free, no VPN required)

- **Email/Push notifications for fermentation status and temperature alerts**
  - Temperature control alerts (temp out of range, heating/cooling events, Kasa plug failures)
  - Batch alerts (signal loss, fermentation starting, daily reports)
  - Configurable notification settings per event type
  - See [Notification Types](#notification-types) section below for complete list of 11 notification types

## Getting Started -

### Prerequisites

- Raspberry Pi (recommended) running Raspberry Pi OS
- Python 3
- Bluetooth enabled (for Tilt hydrometer scanning)
- (Optional) TP-Link Kasa plugs for temperature control

## Installation
### One-Command Install (Recommended)

For a fresh Raspberry Pi OS installation, run **as your normal (non-root) user**:

```bash
curl -sSL https://raw.githubusercontent.com/RabbitFarmer/fermentatorium/main/installer/automated-install.sh | sudo bash
```

> **Important:** run the command above from your regular user account with `sudo` (e.g. `pi`, `flc3`).
> Do **not** log in directly as root — the installer uses `$SUDO_USER` to determine which user will
> own the repo checkout and run the service.  If `$SUDO_USER` is unset (e.g. you are already logged
> in as root) the service will run as root, which is not recommended.

This will:

- Install OS dependencies (`git`, `python3`, `python3-venv`, `bluez`, …)
- Clone the repository to `/tmp/fermentatorium-installer/fermentatorium` (owned by your user)
- Add your user to the `bluetooth` group for BLE (Tilt) access
- Create a Python virtual environment inside the repo and install `requirements.txt`
- Install and enable `fermentatorium.service` (systemd), running directly from the cloned directory as your user

After installation, open:

- `http://<raspberry-pi-ip>:5001` (example: `http://192.168.0.120:5001`)

### Quick Installation (Git Clone)

```bash
git clone https://github.com/RabbitFarmer/fermentatorium.git
cd fermentatorium
sudo ./install.sh
```

> **Note:** clone the repo as your normal user first, then run `sudo ./install.sh`.
> `install.sh` detects the invoking user via `$SUDO_USER` and runs the service as that user.

### Service Management

```bash
sudo systemctl status fermentatorium.service --no-pager
sudo systemctl restart fermentatorium.service
journalctl -u fermentatorium.service -n 200 --no-pager
```

### Manual Installation (Advanced)

If you prefer to install manually:

1. **Clone the repository**
   ```bash
   git clone https://github.com/RabbitFarmer/fermentatorium.git
   cd fermentatorium
   ```

2. **Add your user to the bluetooth group** (for Tilt BLE scanning)
   ```bash
   sudo usermod -aG bluetooth "$USER"
   ```

3. **Create the virtual environment + install dependencies**
   ```bash
   python3 -m venv venv
   venv/bin/pip install --upgrade pip
   venv/bin/pip install -r requirements.txt
   ```

4. **Install systemd service**
   Create `/etc/systemd/system/fermentatorium.service` (see `install.sh` for the exact unit contents), then:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now fermentatorium.service
   ```

6. **Set up a Python virtual environment (REQUIRED on Raspberry Pi):**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

   > sudo apt install python3-venv python3-full
   > ```

7. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

8. **Start the application:**
   
   **Option A: Using the convenience script (automatically opens browser):**
   ```bash
   ./start.sh
   ```
   This script will start the Flask app and automatically open `http://127.0.0.1:5001` in your default browser.
   
   **Option B: Manual start:**
   ```bash
   python3 app.py
   ```
   Then visit `http://<raspberry-pi-ip>:5001` in your browser.



### First Run Configuration ###

**On first run, the application automatically creates configuration files from templates.**

- Configuration files are created in the `config/` directory
- Use the web interface, click on the gear icon to configure your settings (brewery name, Kasa plug IPs, Tilt assignments, etc.)
- Your personal configuration, passwords and data files are **not tracked in git** - they remain private on your system

For more details, see [config/README.md](config/README.md).

### Running on System Startup

You have two options for auto-starting the application at boot:

#### Option 1: Desktop Autostart (Recommended for setups with monitor)

If you have a Raspberry Pi with a monitor, keyboard, and mouse, and want the browser to open automatically:

```bash
# Run the desktop autostart installer
bash install_desktop_autostart.sh
```

This will:
- ✓ Start the application when you log in
- ✓ Open the browser automatically to the dashboard
- ✓ Wait for the Flask server to be ready (up to 2 minutes at boot)
- ✓ No sudo required

#### Option 2: Systemd Service (Recommended for headless setups)

For headless setups or if you prefer the application to run as a background service:

```bash
# Run the automated service installer (requires full path)
bash /full/path/to/threecontrol-/install_service.sh

# Example:
# bash /home/pi/threecontrol-/install_service.sh
```

> **Note:** The installer must be run with the full path to ensure correct service file generation.

The installer will:
- ✓ Automatically detect your installation directory and username
- ✓ Generate a service file with correct paths for your setup
- ✓ Install and optionally enable/start the systemd service
- ✓ Run in background without opening browser (access via network)

**Alternative - Manual systemd Installation:**
```bash
# Edit fermenter.service with your paths, then:
sudo cp fermenter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fermenter
sudo systemctl start fermenter
```


## Configuration -- Gear Icon 

- Edit system settings via the web dashboard.
- Configure batch and temperature settings for each Tilt hydrometer color.

## Remote Access

Access your Fermenter Temperature Controller from anywhere using **Raspberry Pi Connect** — the official, free remote access solution from the Raspberry Pi Foundation.

### Raspberry Pi Connect (Recommended)

[connect.raspberrypi.com](https://connect.raspberrypi.com) provides secure, browser-based remote access to your Raspberry Pi desktop and shell without needing to configure VPNs, port forwarding, or firewall rules.

#### Setup

1. **Sign up** for a free account at [connect.raspberrypi.com](https://connect.raspberrypi.com).

2. **Install Raspberry Pi Connect** on your Pi:
   ```bash
   sudo apt update
   sudo apt install rpi-connect
   ```

3. **Sign in** to link your Pi to your account:
   ```bash
   rpi-connect signin
   ```

4. **Access your Pi remotely** by logging in at [connect.raspberrypi.com](https://connect.raspberrypi.com) from any browser.
   - Use the **Screen Sharing** feature to view the full desktop, or
   - Use the **Remote Shell** to run commands directly.

5. **Open the Fermenter Dashboard** from the remote browser:
   ```
   http://localhost:5001
   ```
   (Use the local address since you're accessing it through the Pi's own browser via Connect.)

> **No special configuration needed** — Raspberry Pi Connect handles all the networking securely through the Raspberry Pi Foundation's servers. Your fermenter data never leaves your home network.

### RUNNING THE PROGRAM AFTER SETUP ###
- After program start, activate a Tilt hydrometer.
- Program will detect it and display "Unknown Beer"
- Click on Gear icon, Batch Settings to set up your fermenter.
- Click on Gear icon, Temperature Contol Settings to set up batches using Temperature Control. Notice that the settings screen and Temperature Control display on the main display employ an On/Off switch. When ON, the settings are put to action. When OFF, temperature control is not operational. 

## File Structure

### Core Application Files
- `app.py` — Main web server and controller
- `start.sh` — Convenience script to start the app and open browser
- `tilt_static.py` — Tilt UUIDs and color maps
- `kasa_worker.py` — Kasa plug interface
- `logger.py` — Logging and notification system
- `fermentation_monitor.py` — Fermentation stability logic
- `batch_history.py` — Batch logging and management
- `archive_compact_logs.py` — Log archival and compaction utility

### Directory Structure
```
/config/              Configuration files (JSON)
  ├── system_config.json
  ├── tilt_config.json
  ├── temp_control_config.json
  ├── batch_settings.json
  └── config.json

/batches/             Per-batch data files (JSONL)
  ├── {brewname}_{YYYYmmdd}_{brewid}.jsonl
  └── batch_history_{color}.json

/temp_control/        Temperature control logs (JSONL)
  └── temp_control_log.jsonl

/logs/                General application logs
  ├── error.log
  ├── warning.log
  └── kasa_errors.log

/templates/           HTML files for web UI
/static/              CSS and static assets
/export/              Exported CSV files
```

### Configuration Files
Configuration files are stored in `/config/` directory and contain:
- `system_config.json` - System-wide settings (brewery info, SMTP, notifications, external logging)
- `tilt_config.json` - Per-tilt configuration (batch info, OG/FG targets)
- `temp_control_config.json` - Temperature control settings for up to 3 independent controllers
- `batch_settings.json` - Batch-specific settings
- `config.json` - Additional configuration options

### External Logging Integrations

The system supports posting fermentation data to external logging services like Brewer's Friend, BrewFather, or custom endpoints.

**Configuration via Web Dashboard:**
1. Navigate to **System Settings** → **Logging Integrations** tab
2. Set the **External Post Interval** (recommended: 15 minutes)
3. For each external service (up to 3):
   - Enter a **Service Name** (e.g., "Brewer's Friend")
   - Enter the **URL** from your external service
   - Configure **HTTP Method**, **Content Type**, and **Request Timeout**
   - Select appropriate **Field Map Template** or create custom mapping

**Brewer's Friend Integration:**
- Brewer's Friend supports both `/tilt/` and `/stream/` endpoints
- The program can use **either** endpoint - both work correctly
- Recommended: Use the **Stream endpoint** (`https://log.brewersfriend.com/stream/YOUR_API_KEY`) for real-time logging
- The `/tilt/` endpoint is also supported for compatibility with Tilt-specific integrations
- Field mapping: The system automatically maps Tilt data fields to Brewer's Friend's expected format

**URL Format Examples:**
- Brewer's Friend Stream: `https://log.brewersfriend.com/stream/YOUR_API_KEY`
- Brewer's Friend Tilt: `https://log.brewersfriend.com/tilt/YOUR_API_KEY`
- BrewFather: `http://log.brewfather.net/stream?id=YOUR_STREAM_ID`
- Custom endpoint: `https://your-server.com/api/fermentation-data`

**Field Mapping:**
The system provides predefined field maps for common services and allows custom JSON mapping for other services. Available data fields include:
- `timestamp` - ISO 8601 timestamp
- `tilt_color` - Tilt hydrometer color
- `gravity` - Specific gravity reading
- `temp_f` - Temperature in Fahrenheit
- `brew_id` - Unique batch identifier
- `device` - Device identifier

**Request Timeout:**
The Request Timeout setting (default: 8 seconds) controls how long the system will wait for a response from the external service before timing out. This prevents the system from hanging if the external service is slow or unavailable.

## Notification Types

The system can send notifications via Email and/or Push (Pushover or ntfy) for various fermentation and temperature control events. All notifications  use a common notification system with deduplication to prevent duplicate alerts.

### Batch Notifications

Batch notifications monitor fermentation progress and tilt signal status:

1. **Loss of Signal** - Sent when no Tilt readings have been received for the configured timeout period (default: 30 minutes)
   - Subject: `{Brewery Name} - Loss of Signal`
   - Includes: Brewery name, Tilt color, Beer name, Date/Time
   - Configurable: `enable_loss_of_signal` in batch notifications settings

2. **Fermentation Started** - Sent when gravity drops 0.010+ points from original gravity across 3 consecutive readings
   - Subject: `{Brewery Name} - Fermentation Started`
   - Includes: Brewery name, Tilt color, Beer name, Starting gravity, Current gravity
   - Configurable: `enable_fermentation_starting` in batch notifications settings

3. **Fermentation Completion** - Sent when gravity has been stable (±0.002) for 24 hours
   - Subject: `{Brewery Name} - Fermentation Completion`
   - Includes: Brewery name, Tilt color, Beer name, Final gravity, Apparent attenuation
   - Configurable: `enable_fermentation_completion` in batch notifications settings

4. **Daily Report** - Sent once per day at a configured time with fermentation progress
   - Subject: `{Brewery Name} - Daily Report`
   - Includes: Starting gravity, Current gravity, Net change, Change since yesterday
   - Configurable: `enable_daily_report` and `daily_report_time` in batch notifications settings

### Temperature Control Notifications

Temperature control notifications alert when temperatures exceed limits, when heating/cooling equipment changes state, or when Kasa smart plugs fail to respond:

3. **Temperature Below Low Limit** - Sent when current temperature drops below the configured low limit
   - Subject: `{Brewery Name} - Temperature Control Alert`
   - Includes: Current temperature, Low limit setting, Tilt color
   - Configurable: `enable_temp_below_low_limit` in temperature control notifications settings

4. **Temperature Above High Limit** - Sent when current temperature rises above the configured high limit
   - Subject: `{Brewery Name} - Temperature Control Alert`
   - Includes: Current temperature, High limit setting, Tilt color
   - Configurable: `enable_temp_above_high_limit` in temperature control notifications settings

5. **Heating On** - Sent when the heating control is activated
   - Subject: `{Brewery Name} - Temperature Control Alert`
   - Includes: Current temperature, Low limit setting, Tilt color
   - Configurable: `enable_heating_on` in temperature control notifications settings (disabled by default)

6. **Heating Off** - Sent when the heating control is deactivated
   - Subject: `{Brewery Name} - Temperature Control Alert`
   - Includes: Current temperature, Tilt color
   - Configurable: `enable_heating_off` in temperature control notifications settings (disabled by default)

7. **Cooling On** - Sent when the cooling control is activated
   - Subject: `{Brewery Name} - Temperature Control Alert`
   - Includes: Current temperature, High limit setting, Tilt color
   - Configurable: `enable_cooling_on` in temperature control notifications settings (disabled by default)

8. **Cooling Off** - Sent when the cooling control is deactivated
   - Subject: `{Brewery Name} - Temperature Control Alert`
   - Includes: Current temperature, Tilt color
   - Configurable: `enable_cooling_off` in temperature control notifications settings (disabled by default)

9. **Kasa Plug Connection Failure** - Sent when a Kasa smart plug fails to respond or connection is lost
   - Subject: `{Brewery Name} - Kasa Plug Connection Failure`
   - Includes: Mode (Heating/Cooling), Plug URL, Error message, Tilt color
   - Configurable: `enable_kasa_error` in temperature control notifications settings
   - Note: Helps alert when heating/cooling equipment becomes unreachable

**Note:** Heating On/Off and Cooling On/Off notifications are disabled by default to avoid notification overload, but users can enable them if desired. These events are always logged to the temperature chart regardless of notification settings.

### Notification Delivery Methods

- **Email** - SMTP-based email notifications (supports Gmail, custom SMTP servers)

- **Push** -   Mobile push notifications via:
  - **Pushover** - Paid service ($5 one-time per platform, very reliable)
  - **ntfy** - Free, open-source, self-hostable alternative
  - **Both** - Send via both Email and Push simultaneously
  - Some phones may block push notifications. Check permissions in your cellphone.
    
### Notification Deduplication

All notifications use a 10-second pending queue with deduplication to prevent duplicate alerts. If the same notification is triggered multiple times within 10 seconds (e.g., from rapid BLE updates), only the first notification will be sent.

### Retry Mechanism

Failed notifications are automatically retried with exponential backoff:
- First retry: After 5 minutes
- Second retry: After 30 minutes
- Maximum retries: 2 (total of 3 attempts including initial send)

### Configuration

Notification settings can be configured via the web dashboard (click Gear icon):
- Navigate to **System Settings** → **Push/Email** tab for delivery method settings
- Navigate to **Batch Settings** for batch notification preferences
- Navigate to **Temp Control Settings** for temperature control notification preferences

## Reception Range
-  The program's reception of Tilt BLE signals is contingent upon distance the Raspberry Pi is from the tilt in the fermenter, fermenter construction (ie. steel, plastic), cooling device used (freezer may impaire signal), and signal strength of the individual Tilts.
-  
## License

MIT License
Copyright (c) 2026 RabbitFarmer aka Frank Cunningham

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Credits

- [Tilt Hydrometer](https://tilthydrometer.com/)
- [python-kasa](https://github.com/python-kasa/python-kasa)
- [Bleak](https://github.com/hbldh/bleak)
