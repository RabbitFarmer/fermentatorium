# Fermentatorium

**A Raspberry Pi fermentation monitor and temperature controller for homebrewers.**  
Track gravity and temperature from [Tilt Hydrometers](https://tilthydrometer.com/), control heating and cooling through TP-Link Kasa smart plugs, visualise trends with interactive charts, and receive push or email notifications — all from a self-hosted web dashboard.

**Free · Open Source · MIT License**

---

## Table of Contents

1. [Features](#features)
2. [Hardware Requirements](#hardware-requirements)
3. [Installation](#installation)
   - [Option A — One-Command Install (Recommended)](#option-a--one-command-install-recommended)
   - [Option B — Manual Clone + Install](#option-b--manual-clone--install)
   - [Option C — Desktop Autostart (Pi with Monitor)](#option-c--desktop-autostart-pi-with-monitor)
4. [First-Run Configuration](#first-run-configuration)
5. [Starting and Stopping](#starting-and-stopping)
6. [Dashboard Overview](#dashboard-overview)
7. [Configuration Reference](#configuration-reference)
   - [System Settings](#system-settings)
   - [Tilt Configuration](#tilt-configuration)
   - [Temperature Control](#temperature-control)
   - [Notifications](#notifications)
   - [External Logging](#external-logging)
8. [Compatible Kasa Smart Plugs](#compatible-kasa-smart-plugs)
9. [Updating](#updating)
10. [Backup and Restore](#backup-and-restore)
11. [Utilities](#utilities)
12. [Troubleshooting](#troubleshooting)
13. [License](#license)

---

## Features

| Capability | Details |
|---|---|
| **Multi-Tilt Monitoring** | All 8 Tilt colors (Black, Blue, Green, Orange, Pink, Purple, Red, Yellow) — standard, Pro, and Mini-Pro models |
| **3 Independent Temperature Controllers** | Simultaneous heating and/or cooling for up to 3 fermenters using TP-Link Kasa smart plugs |
| **Live Web Dashboard** | Real-time gravity & temperature display — works headless or with a local monitor in kiosk mode |
| **Interactive Charts** | Gravity trends, temperature history, and heating/cooling events powered by Plotly |
| **External Logging** | Post data to Brewer's Friend, BrewFather, or any custom HTTP endpoint on a configurable interval |
| **Push & Email Alerts** | Pushover, ntfy (free/self-hosted), or SMTP email — 11 configurable notification types |
| **CSV Data Export** | Download batch and temperature-control logs for offline analysis |
| **Batch History** | Full lifecycle view per batch including gravity curve, estimated ABV, and event timeline |
| **Remote Access** | Secure browser-based access via Raspberry Pi Connect — no VPN or port forwarding needed |
| **Backup / Restore** | One-click system backup and restore of all config and batch data from the dashboard |
| **Self-Updating** | In-dashboard update button pulls the latest code from GitHub |
| **Privacy-First** | All config, batch data, and logs live on your system — nothing tracked remotely |

### Smart Notifications (11 event types)

- 📉 **Fermentation Started** — gravity drops 0.010+ points confirmed across 3 readings
- 🏁 **Fermentation Complete** — gravity stable (±0.002) for 24 hours
- 📊 **Daily Progress Report** — scheduled summary of gravity changes
- 📡 **Tilt Signal Lost** — alert when your Tilt goes quiet (configurable timeout, default 30 min)
- 🌡️ **Temperature Out of Range** — alert when temp exceeds your high or low limit
- 🔥 / ❄️ **Heating & Cooling Events** — optional alerts when plugs switch on or off
- 🔌 **Kasa Plug Failure** — immediate notification if heating/cooling equipment goes offline

All notifications include smart deduplication and automatic retry with exponential backoff (up to 3 attempts).

---

## Hardware Requirements

| Item | Notes |
|---|---|
| **Raspberry Pi** | Any model with Python 3.9+ and Bluetooth; Pi 3B+ or Pi 4 recommended |
| **Raspberry Pi OS** | 32-bit or 64-bit Lite or Desktop; Debian Bookworm or Bullseye |
| **MicroSD card** | 16 GB or larger |
| **Tilt Hydrometer(s)** | Standard, Pro, or Mini-Pro — any color combination |
| **TP-Link Kasa Smart Plug(s)** | Optional — only required for temperature control (see [Compatible Kasa Smart Plugs](#compatible-kasa-smart-plugs)) |
| **Home Network** | Wi-Fi or Ethernet; plug and Pi must be on the same subnet |

> **No Tilt yet?** Fermentatorium runs a built-in Tilt simulator (`tilt_scan_sim.py`) and ships with demo data utilities so you can explore the full UI before connecting live hardware.

---

## Installation

### Option A — One-Command Install (Recommended)

On a fresh Raspberry Pi OS installation with internet access, run:

```bash
curl -sSL https://raw.githubusercontent.com/RabbitFarmer/fermentatorium/main/installer/automated-install.sh | sudo bash
```

> **Security note:** Piping directly to `bash` executes the script without reviewing it first. To inspect before running:
> ```bash
> curl -sSL https://raw.githubusercontent.com/RabbitFarmer/fermentatorium/main/installer/automated-install.sh -o /tmp/fermentatorium-install.sh
> less /tmp/fermentatorium-install.sh   # review it
> sudo bash /tmp/fermentatorium-install.sh
> ```

The automated installer:
1. Installs OS dependencies: `git`, `python3-venv`, `python3-pip`, `bluetooth`, `bluez`, `ca-certificates`
2. Clones the repository into `/opt/fermentatorium`
3. Creates a Python virtual environment (`venv/`) and installs all Python packages from `requirements.txt`
4. Adds your user to the `bluetooth` group for BLE Tilt scanning
5. Creates and enables `fermentatorium.service` (starts automatically at boot)

After installation finishes, open a browser on any device on your local network:

```
http://<raspberry-pi-ip>:5001
```

### Option B — Manual Clone + Install

Clone the repository anywhere you like, then run the installer from inside it:

```bash
git clone https://github.com/RabbitFarmer/fermentatorium.git
cd fermentatorium
sudo ./install.sh
```

`install.sh` performs the same steps as the automated installer (venv creation, pip install, bluetooth group, and systemd service) but uses the directory it lives in as the install root — no files are copied elsewhere.

After `install.sh` finishes, you need to **log out and back in** (or run `newgrp bluetooth`) for the Bluetooth group membership to take effect.

Open the dashboard:
```
http://<raspberry-pi-ip>:5001
```

### Option C — Desktop Autostart (Pi with Monitor)

If your Pi has a monitor and you want the browser to open automatically when you log in:

```bash
bash ~/fermentatorium/install_desktop_autostart.sh
```

This creates `~/.config/autostart/fermentatorium.desktop` so `start.sh` runs at login, which launches the app in the background and opens Chromium in kiosk mode pointing to `http://127.0.0.1:5001`.

> **Note:** Desktop autostart and the systemd service can coexist. `start.sh` detects when `fermentatorium.service` is already running and simply waits for Flask to respond, then opens the browser — it never starts a second app instance.

---

## First-Run Configuration

On first launch, Fermentatorium copies the config templates in `config/` to live JSON files with safe defaults. No manual editing is needed — everything is configurable through the web dashboard.

Open `http://<raspberry-pi-ip>:5001` and navigate to each settings page:

1. **System Settings** — set your brewery name, brewer name, temperature units (°F / °C), timezone, and port
2. **Tilt Config** — assign each Tilt color to a batch (beer name, batch name, recipe OG/FG/ABV)
3. **Temp Control Settings** — configure up to 3 independent temperature controllers (Kasa plug IPs, target range, heating/cooling mode)
4. **Notifications** — enter Pushover keys, ntfy topic, or SMTP credentials and select which events trigger alerts
5. **External Logging** — configure Brewer's Friend or BrewFather API keys and post intervals

---

## Starting and Stopping

### Systemd service (installed by `install.sh` — recommended for headless setups)

```bash
# Check status
sudo systemctl status fermentatorium.service

# Start / stop / restart
sudo systemctl start fermentatorium.service
sudo systemctl stop fermentatorium.service
sudo systemctl restart fermentatorium.service

# View live logs
journalctl -u fermentatorium.service -f --no-pager

# View last 200 lines of logs
journalctl -u fermentatorium.service -n 200 --no-pager
```

The service starts automatically on boot. The `stopit.sh` helper in the repo root is a shortcut for `sudo systemctl stop fermentatorium`.

### Manual start (desktop / development)

```bash
bash ~/fermentatorium/start.sh
```

`start.sh` creates the virtual environment if it does not exist, installs any missing packages, frees port 5001 if occupied, and launches `app.py` in the background. It also opens the browser to `http://<local-ip>:5001` when a display is available.

Application output is written to `app.log` in the repo root.

### Stop the manually-started app

```bash
# Systemd shortcut (works even for the manually-started process if port is in use)
bash ~/fermentatorium/stopit.sh
# or kill the process directly:
pkill -f app.py
```

---

## Dashboard Overview

The web dashboard is accessible at `http://<raspberry-pi-ip>:5001` from any browser on your network.

| Page | URL | Description |
|------|-----|-------------|
| **Main Display** | `/` | Live gravity and temperature for all active Tilts |
| **Charts** | `/chart_plotly/<color>` | Interactive Plotly chart for a Tilt — gravity, temperature, and heating/cooling events |
| **Batch Settings** | `/batch_settings` | Enter or edit beer name, batch details, recipe targets for each Tilt color |
| **Batch History** | `/batch_history` | List of all completed and active batches |
| **Batch Review** | `/batch_review/<brewid>` | Full batch detail view: gravity curve, event log, and CSV export |
| **Temp Control** | `/temp_config` | Configure up to 3 independent temperature controllers |
| **Temp Summary** | `/temp_summary/<0-2>` | Live temperature control status and event log for one controller |
| **Temp Report** | `/temp_report` | Historical temperature-control log with chart and CSV export |
| **System Config** | `/system_config` | Brewery name, units, timezone, port, notification settings, external logging, Kasa credentials |
| **Tilt Config** | `/tilt_config` | Per-color batch assignment and Tilt device table |
| **Kasa Scan** | `/scan_kasa_plugs` | Discover Kasa plugs on the local network |
| **Log Management** | `/log_management` | View, archive, and delete application logs |
| **Export** | `/export_batch_data_csv/<brewid>` | Download a batch as CSV |
| **Backup / Restore** | System Config page | Create and download a full system backup; restore from a `.zip` file |

The top navigation bar provides quick access to all major sections. A **Startup** page (`/startup`) is shown on fresh installs to guide initial configuration.

---

## Configuration Reference

### System Settings

Stored in `config/system_config.json`. All fields are editable through the **System Settings** page.

| Field | Default | Description |
|-------|---------|-------------|
| `brewery_name` | `"The Tilt Fermentatorium"` | Appears in notifications and the page title |
| `brewer_name` | `"Your Name"` | Brewer name shown in the UI |
| `units` | `"Fahrenheit"` | `"Fahrenheit"` or `"Celsius"` |
| `timezone` | (system TZ) | IANA timezone string, e.g. `"America/Los_Angeles"` |
| `flask_port` | `5001` | TCP port the web server listens on |
| `tilt_inactivity_timeout_minutes` | `30` | Minutes of silence before a Tilt is considered offline |
| `push_provider` | `"pushover"` | `"pushover"` or `"ntfy"` |
| `pushover_user_key` | `""` | Pushover user key |
| `pushover_api_token` | `""` | Pushover API token |
| `ntfy_server` | `"https://ntfy.sh"` | ntfy server URL (use your own for self-hosted) |
| `ntfy_topic` | `""` | ntfy topic name |
| `ntfy_auth_token` | `""` | ntfy auth token (for protected topics) |
| `warning_mode` | `"NONE"` | `"NONE"`, `"EMAIL"`, `"PUSH"`, or `"BOTH"` |
| `smtp_host` | — | SMTP server hostname (e.g. `smtp.gmail.com`) |
| `smtp_port` | `587` | SMTP port (587 for STARTTLS, 465 for SSL) |
| `sending_email` | — | From address and SMTP username |
| `smtp_password` | — | SMTP password or Gmail App Password (16-char) |
| `email_to` | — | Recipient address for alert emails |

> **Config files are never overwritten by `git pull`.** Templates (`*.json.template`) track defaults in git; your live `*.json` files persist across updates.

### Tilt Configuration

Stored in `config/tilt_config.json`. Edit through **Tilt Config** or **Batch Settings** in the dashboard.

Each Tilt color entry has:

| Field | Description |
|-------|-------------|
| `beer_name` | Name of the beer being fermented |
| `batch_name` | Batch identifier (e.g. `"Batch 42"`) |
| `ferm_start_date` | Fermentation start date (`MM/DD/YYYY`) |
| `recipe_og` | Recipe original gravity |
| `recipe_fg` | Recipe final gravity |
| `recipe_abv` | Recipe target ABV |
| `actual_og` | Confirmed original gravity (locked in after first reliable reading) |
| `brewid` | Auto-generated 8-character batch ID used for data storage |

### Temperature Control

Stored in `config/temp_control_config.json`. Edit through **Temp Control Settings** in the dashboard.

Up to 3 independent controllers are supported. Each controller has:

| Field | Description |
|-------|-------------|
| `tilt_color` | Tilt color to read temperature from (must have an active Tilt) |
| `low_limit` | Lower temperature bound (°F or °C, matching your units setting) |
| `high_limit` | Upper temperature bound |
| `enable_heating` | `true` to allow the heating plug to switch on |
| `enable_cooling` | `true` to allow the cooling plug to switch on |
| `heating_plug` | IP address of the Kasa plug connected to the heater |
| `cooling_plug` | IP address of the Kasa plug connected to the cooler/fridge |
| `compressor_delay` | Minutes to wait before restarting the compressor after it was last switched off (protects the compressor) |

**Control logic:** When temperature falls below `low_limit`, the heater turns on (if enabled). When temperature rises above `high_limit`, the cooler turns on (if enabled). Temperature control pauses automatically if the Tilt goes offline.

### Notifications

Configure in **System Settings** on the dashboard. Two independent channels are available:

#### Pushover
1. Create an account at [pushover.net](https://pushover.net)
2. Create an application to obtain an API token
3. Enter your **User Key** and **API Token** in System Settings → Notifications
4. Set `warning_mode` to `"PUSH"` (or `"BOTH"` for push + email)

#### ntfy (free, no account required)
1. Install the ntfy app on your phone (iOS / Android) — or use the web at [ntfy.sh](https://ntfy.sh)
2. Subscribe to a unique topic name of your choosing
3. Enter the same topic name in System Settings → ntfy Topic
4. Optionally enter a custom ntfy server URL for self-hosted instances
5. Set `warning_mode` to `"PUSH"` (or `"BOTH"`)

#### SMTP Email
Works with Gmail (App Password required), Outlook, or any SMTP relay.

| SMTP provider | Host | Port | Notes |
|---|---|---|---|
| Gmail | `smtp.gmail.com` | `587` | Requires a 16-character [App Password](https://support.google.com/accounts/answer/185833) |
| Outlook / Hotmail | `smtp-mail.outlook.com` | `587` | Standard SMTP password |
| Generic relay | your server | 25 / 465 / 587 | SSL on 465, STARTTLS on 587 |

Set `warning_mode` to `"EMAIL"` (or `"BOTH"`) after saving credentials. Use the **Test Email** button in System Settings to confirm delivery.

### External Logging

Fermentatorium can forward gravity and temperature readings to third-party logging services on a configurable interval. Configure in **System Settings → External Logging**.

| Service | Details |
|---------|---------|
| **Brewer's Friend** | Enter your API key; readings are posted to the Brewer's Friend streaming endpoint |
| **BrewFather** | Enter your custom stream URL; readings posted in BrewFather batch format |
| **User-Defined** | Enter any HTTP endpoint; readings are forwarded as JSON POST |

Test the connection with the **Test External Logging** button before starting a batch.

---

## Compatible Kasa Smart Plugs

Fermentatorium controls heaters and coolers through TP-Link Kasa smart plugs on your local network.

### Plugs that work WITHOUT a TP-Link account

These models use the older local IoT protocol (port 9999) and respond to commands on your local network without any account login:

| Model | Notes |
|-------|-------|
| **HS100** | Single outlet, no energy monitoring |
| **HS103** | Single compact outlet, widely available |
| **HS105** | Mini dual outlet |
| **HS110** | Single outlet with energy monitoring |
| **KP115** | Single outlet with energy monitoring — **firmware 1.x only**; updating firmware may switch the device to KLAP (requires login) |

> **Tip:** If you own one of the models above, disable automatic firmware updates in the Kasa app before first use to prevent the device from upgrading to KLAP firmware that requires credentials.

### Plugs that REQUIRE a TP-Link username/password

These models use the newer KLAP protocol. Enter your TP-Link account email and password in **System Settings → Kasa Credentials**:

| Model | Notes |
|-------|-------|
| **EP25** (firmware v2.6+) | Most common "new" Kasa outlet; earlier firmware may still work without credentials |
| **KP125M** | Matter-compatible outlet, always requires login |
| **KP400** | Outdoor double outlet, always requires login |

Use the **Scan for Kasa Plugs** button in the dashboard to discover plugs on your network, then paste their IP addresses into the temperature control configuration.

---

## Updating

The easiest way to update is through the dashboard: open **System Settings** and click **Check for Updates** / **Update System**. Fermentatorium fetches the latest code from GitHub and restarts automatically.

To update manually from the command line:

```bash
cd /opt/fermentatorium   # or wherever you cloned the repo
git pull
sudo systemctl restart fermentatorium.service
```

Your configuration files (`config/*.json`) and batch data (`batches/`) are never touched by a `git pull`.

---

## Backup and Restore

From the dashboard, open **System Settings → Backup & Restore**:

- **Create Backup** — downloads a `.zip` archive containing all config files, batch data, temperature logs, and notification logs.
- **Restore** — upload a backup `.zip` to restore a previous state (config and data files only; the code itself is not included in backups).

Backups are stored in the `export/` directory on the Pi until downloaded or deleted.

---

## Utilities

The `utils/` directory contains helper scripts for data management:

| Script | Description |
|--------|-------------|
| `import_brewers_friend.py` | Convert a Brewer's Friend JSON export to Fermentatorium's JSONL batch format |
| `setup_demo.sh` | Load demo fermentation data so you can explore charting features without live hardware |
| `verify_demo_data.py` | Verify that demo data was loaded correctly |
| `archive_compact_logs.py` | Compact older JSONL log files to reduce disk usage |
| `backfill_temp_control_jsonl.py` | Backfill temperature control events from legacy log formats |
| `purge_excess_tilt_readings.py` | Remove duplicate or excess Tilt readings from batch files |

### Import from Brewer's Friend

```bash
python3 utils/import_brewers_friend.py export.json \
    --color Red \
    --beer-name "My IPA" \
    --batch-name "Batch 7"
```

### Load demo data

```bash
bash utils/setup_demo.sh
```

Then open `http://<pi-ip>:5001/chart_plotly/Black` to view the demo fermentation chart.

---

## Troubleshooting

### Tilt not appearing in the dashboard

- Verify the Tilt is powered on and floating in liquid (or held upright in air for testing).
- Ensure your user is in the `bluetooth` group: `groups $USER`. If not, run `sudo usermod -aG bluetooth $USER` and log out and back in.
- Check that `bluetooth.service` is running: `sudo systemctl status bluetooth`.
- View application logs: `journalctl -u fermentatorium.service -n 100 --no-pager`.

### Kasa plug not responding

- Confirm the plug is on the same Wi-Fi network and subnet as the Pi.
- Find the plug's IP address in your router's DHCP table. Assign it a static lease to prevent the IP from changing.
- If the plug model requires a TP-Link account, ensure credentials are entered in **System Settings → Kasa Credentials**.
- Use the **Scan for Kasa Plugs** button in the dashboard to test connectivity.

### Port 5001 already in use

`start.sh` automatically kills any process on port 5001 before launching. If you still see a conflict:

```bash
sudo lsof -i :5001        # identify the process
sudo kill <PID>
sudo systemctl restart fermentatorium.service
```

### `python3-venv` missing (PEP 668 error)

```bash
sudo apt update
sudo apt install python3-venv python3-full
sudo ./install.sh   # re-run the installer
```

### Application started but browser shows a blank page or error

Check `app.log` (for manually started instances) or the systemd journal:

```bash
# systemd
journalctl -u fermentatorium.service -n 50 --no-pager

# manual start
tail -50 ~/fermentatorium/app.log
```

### Desktop autostart opens two browser windows

Run `install_desktop_autostart.sh` to remove stale LXDE autostart entries that may conflict with the XDG `.desktop` entry. The script also cleans up old port-5000 entries from previous installations.

```bash
bash ~/fermentatorium/install_desktop_autostart.sh
```

---

## License

```
MIT License

Copyright (c) 2026 RabbitFarmer

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

**Author:** RabbitFarmer (Frank Cunningham) © 2026

> *Brew better. Ferment smarter. Relax more.* 🍻
