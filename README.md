# Fermentatorium

**A Raspberry Pi fermentation monitor and temperature controller for homebrewers.**  
Track gravity and temperature from [Tilt Hydrometers](https://tilthydrometer.com/), control heating and cooling through TP-Link Kasa smart plugs, visualise trends with interactive charts, and receive push or email notifications — all from a self-hosted web dashboard.

**Free · Open Source · MIT License**

---

## Quick Start for Non-Computer People

If you are not a computer person, don't worry — here is what you need to know in plain language:

### What you need
1. A **Raspberry Pi** (Pi 3B+ or Pi 4 recommended) — a small, inexpensive computer about the size of a deck of cards.
2. A **MicroSD card** (16 GB or larger) — this acts as the Pi's hard drive.
3. A **Tilt Hydrometer** — a floating device you drop in your fermenter. It wirelessly reports gravity and temperature.
4. Optional: a **TP-Link Kasa smart plug** — to automatically control a heater or refrigerator.

### Five steps to get running
1. **Set up the Raspberry Pi** — install Raspberry Pi OS on the MicroSD card (free software, plenty of online guides for your specific Pi model).
2. **Install Fermentatorium** — connect the Pi to the internet, open a terminal, and paste one line of text (see [Installation](#installation) below).
3. **Open the dashboard** — on any phone, tablet, or computer on your home Wi-Fi, open a browser and go to `http://<your-pi-ip>:5001`.
4. **Enter your settings** — tap the **⚙ gear icon** (top-right of the dashboard) and fill in your brewery name, temperature units, and Tilt colors.
5. **Drop in the Tilt, start brewing** — Fermentatorium shows your gravity and temperature live, logs everything automatically, and can alert you by phone notification or email.

> **If you get stuck:** See the detailed sections below for step-by-step instructions on every topic.

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
7. [Operational Walkthroughs](#operational-walkthroughs)
   - [Setting Up a New Brew](#setting-up-a-new-brew)
   - [Setting Up Temperature Control](#setting-up-temperature-control)
   - [Using Charts (Chart_Plotly)](#using-charts-chart_plotly)
   - [Setting Up ntfy Push Notifications](#setting-up-ntfy-push-notifications)
   - [Setting Up Gmail Email Alerts](#setting-up-gmail-email-alerts)
8. [Configuration Reference](#configuration-reference)
   - [System Settings](#system-settings)
   - [Tilt Configuration](#tilt-configuration)
   - [Temperature Control](#temperature-control)
   - [Notifications](#notifications)
   - [External Logging](#external-logging)
9. [Compatible Kasa Smart Plugs](#compatible-kasa-smart-plugs)
10. [Updating](#updating)
11. [Backup and Restore](#backup-and-restore)
12. [Utilities](#utilities)
13. [Demo Data](#demo-data)
14. [Troubleshooting](#troubleshooting)
15. [License](#license)

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

### Quick summary
1. Flash Raspberry Pi OS to your MicroSD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Connect the Pi to your home network (Wi-Fi or Ethernet) and power it on.
3. Open a terminal on the Pi (or SSH into it from another computer).
4. Run the one-line installer below.
5. Open a browser on any device on your network and go to `http://<pi-ip>:5001`.

---

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

Open `http://<raspberry-pi-ip>:5001` and tap the **⚙ gear icon** (top-right corner of the dashboard) to access all settings pages:

1. **System Settings** — set your brewery name, brewer name, temperature units (°F / °C), timezone, and notification preferences
2. **Batch Settings** — assign each Tilt color to a batch (beer name, batch name, recipe OG/FG/ABV)
3. **Temperature Control** — configure up to 3 independent temperature controllers (Kasa plug IPs, target range, heating/cooling mode)
4. **Push/Email (in System Settings)** — enter Pushover keys, ntfy topic, or SMTP credentials and select which events trigger alerts
5. **Logging Integrations (in System Settings)** — configure Brewer's Friend or BrewFather API keys and post intervals

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

Use the systemd shortcut or stop the process directly via your system's process manager.

---

## Dashboard Overview

The web dashboard is accessible at `http://<raspberry-pi-ip>:5001` from any browser on your network.

### Navigation

All navigation is through the **⚙ gear icon** in the top-right corner of the main dashboard. Clicking it opens a dropdown menu with links to every section. There is no separate top navigation bar.

| Menu Item | Description |
|------|-------------|
| **System Settings** | Brewery name, temperature units, timezone, notifications, external logging, Kasa credentials |
| **Temperature Control** | Configure up to 3 independent temperature controllers |
| **Batch Settings** | Enter or edit beer name, batch details, recipe targets for each Tilt color |
| **Batch History** | List of all completed and active batches |
| **Utilities** | Data management, demo data, and maintenance tools |
| **Log Management** | View, archive, and delete application logs |
| **Exit System** | Shut down or reboot the Pi |

### Pages accessible by direct URL

| Page | URL | Description |
|------|-----|-------------|
| **Main Display** | `/` | Live gravity and temperature for all active Tilts |
| **Charts** | `/chart_plotly/<color>` | Interactive Plotly chart for a Tilt — gravity, temperature, and heating/cooling events |
| **Temp Summary** | `/temp_summary/<0-2>` | Live temperature control status and event log for one controller |
| **Temp Report** | `/temp_report` | Historical temperature-control log with chart and CSV export |
| **Tilt Config** | `/tilt_config` | Per-color batch assignment and Tilt device table |
| **Kasa Scan** | `/scan_kasa_plugs` | Discover Kasa plugs on the local network |
| **Export** | `/export_batch_data_csv/<brewid>` | Download a batch as CSV |
| **Backup / Restore** | System Settings page | Create and download a full system backup; restore from a `.zip` file |

### Clicking on cards

- **Tilt brew cards** on the main display are clickable — tap anywhere on the card to open the interactive chart for that Tilt color. Each card also shows a **STATUS** pill (when a batch is active) that links directly to the batch details screen.
- **Temperature control cards** on the main display are clickable — tap a temp-control card to open the temperature summary page for that controller.

---

## Operational Walkthroughs

### Setting Up a New Brew

1. **Place your Tilt** in the fermenter (or hold it upright to test — it broadcasts without liquid).
2. Open the dashboard. The Tilt's color card will appear automatically once the Pi detects it over Bluetooth.
3. Tap the **⚙ gear icon** → **Batch Settings**.
4. Find the row for your Tilt's color and enter:
   - **Beer Name** (e.g., "Centennial IPA")
   - **Batch Name** (e.g., "Batch 12")
   - **Fermentation Start Date**
   - **Recipe OG, FG, and ABV** (optional — used for progress estimates)
5. Click **Save**. The dashboard now shows your batch name on the Tilt card.
6. Gravity and temperature readings are logged automatically every 15 minutes (adjustable in System Settings).

> **Starting over mid-batch?** Use **Batch History → Archive** to close the current batch and start fresh, preserving your previous fermentation data.

---

### Setting Up Temperature Control

Temperature control requires at least one TP-Link Kasa smart plug connected to a heater or fridge/cooler.

1. **Find your plug's IP address.** Use your router's DHCP table, or go to **⚙ gear → Temperature Control → Scan Kasa Plugs**. Assign the plug a static IP in your router to prevent it from changing.
2. Tap **⚙ gear → Temperature Control**.
3. Select the **Controller tab** you want to configure (Controller 1, 2, or 3).
4. Set:
   - **Control Tilt** — choose which Tilt color provides the temperature reading for this controller.
   - **Low Limit** — minimum temperature (heater turns on when temp falls below this).
   - **High Limit** — maximum temperature (cooler turns on when temp rises above this).
   - **Temp Variance for Power Off** — the heater shuts off at *High Limit − Variance* and the cooler shuts off at *Low Limit + Variance*. This prevents the plug from cycling on and off constantly. A value of 1°F is a good starting point.
   - **Max Run Time** — maximum minutes a plug can stay on in one cycle (safety shutoff). Set to 0 to disable.
   - **Enable Heating / Enable Cooling** — check the boxes for what you are controlling.
   - **Heating Plug** — IP address of the Kasa plug connected to your heater.
   - **Cooling Plug** — IP address of the Kasa plug connected to your fridge/cooler.
   - **Heating Plug Port / Cooling Plug Port** — leave blank for automatic detection. Only override for non-standard network configurations.
5. Click **Save**.
6. Toggle the **Temp Control ON/OFF** switch at the top of the page to activate monitoring.

**Temperature Schedule:** You can define a multi-step schedule that automatically adjusts Low/High Limits as fermentation progresses (by days elapsed, gravity reached, or fermentation completion). Enable the schedule in the Temperature Schedule section and add steps. When the schedule is active, it overrides the manual limits.

> **Plug ports:** The Heating Plug Port and Cooling Plug Port fields accept the network port number used to communicate with the Kasa device (default is auto-detected). Leave blank unless instructed otherwise by the Kasa Scan results.

> **Tilt and temperature control are independent functions.** The function of the Tilt for temperature control is independent of its temperature/gravity recording function. When fermentation is complete, click on the **Close** button in ⚙ gear → Batch History. This closes the batch, taking the Tilt offline for temp/gravity reporting. However, the temperature controller will continue to operate until its switch is turned off. This is ideal for instances in which you choose to cold crash after fermentation is completed.

---

### Using Charts (Chart_Plotly)

- **From the dashboard:** Tap anywhere on a Tilt brew card to open that Tilt's interactive chart. Use the **STATUS** pill on the card to view the batch details screen instead.
- **Direct URL:** `http://<pi-ip>:5001/chart_plotly/<Color>` (e.g., `/chart_plotly/Red`).

The Plotly chart shows:
- **Gravity** (specific gravity over time) — primary Y axis
- **Temperature** — secondary Y axis
- **Heating events** — marked with red shading or markers
- **Cooling events** — marked with blue shading or markers

**Interacting with the chart:**
- **Zoom:** Click and drag to draw a selection rectangle; double-click to zoom back out.
- **Pan:** Hold Shift while dragging.
- **Toggle a data series:** Click its name in the legend to hide/show.
- **Hover:** Move the cursor over any point to see exact values.
- **Download a chart image:** Use the Plotly camera icon (top-right of chart) to save a PNG image. This is the recommended way to capture a chart for sharing or printing.

---

### Setting Up ntfy Push Notifications

ntfy is the easiest notification option — it is completely free, requires no account, and works on iOS and Android.

1. Install the **ntfy** app on your phone:
   - [iOS App Store](https://apps.apple.com/app/ntfy/id1625396347)
   - [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
   - Or use the web at [ntfy.sh](https://ntfy.sh)
2. In the ntfy app, tap **+** and subscribe to a **unique topic name** you make up (e.g., `frank-brewery-alerts`). Make the topic hard to guess — it acts as your password.
3. In Fermentatorium, go to **⚙ gear → System Settings → PUSH/eMail tab**.
4. Set **Push Provider** to **ntfy**.
5. Enter your **ntfy Topic** (the same name you subscribed to in step 2).
6. If you are using a self-hosted ntfy server, enter its URL; otherwise leave the server as `https://ntfy.sh`.
7. Set **Messaging Options** to **Push** or **Both**.
8. Click **Save** and use the **Test Push** button to verify delivery.

---

### Setting Up Gmail Email Alerts

Gmail requires an **App Password** — a special 16-character password you generate just for Fermentatorium. This is different from your regular Gmail password.

**Important:** After generating the App Password, use this Gmail account exclusively for sending Fermentatorium alerts. Do not use it for other apps with regular password sign-in. Mixing regular and app passwords in the same account can cause the App Password to be erased by Google.

1. Log in to the Gmail account you want to use for alerts (create a dedicated account if preferred).
2. Go to your **Google Account → Security → 2-Step Verification** and ensure it is enabled.
3. Search for **"App Passwords"** in Google Account settings, or go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
4. Create a new App Password — select **Mail** as the app and **Other** as the device (name it "Fermentatorium").
5. Google will display a **16-character password**. Copy it immediately — it is only shown once.
6. In Fermentatorium, go to **⚙ gear → System Settings → PUSH/eMail tab**.
7. Fill in:
   - **SMTP Host:** `smtp.gmail.com`
   - **SMTP Port:** `587`
   - **Sending Email:** your Gmail address
   - **Email Password:** the 16-character App Password (paste without spaces)
   - **Alert Email To:** the address you want alerts sent to
8. Set **Messaging Options** to **Email** or **Both**.
9. Click **Save** and use the **Test Email** button to confirm delivery.

---

## Configuration Reference

### System Settings

Stored in `config/system_config.json`. All fields are editable through **⚙ gear → System Settings**.

| Field | Default | Description |
|-------|---------|-------------|
| `brewery_name` | `"The Tilt Fermentatorium"` | Appears in notifications and the page title |
| `brewer_name` | `"Your Name"` | Brewer name shown in the UI |
| `units` | `"Fahrenheit"` | `"Fahrenheit"` or `"Celsius"` — all temperature displays and control limits use this setting |
| `timezone` | (system TZ) | IANA timezone string, e.g. `"America/Los_Angeles"` |
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
| `smtp_password` | — | Gmail App Password (16-char) or SMTP password |
| `email_to` | — | Recipient address for alert emails |

> **Config files are never overwritten by `git pull`.** Templates (`*.json.template`) track defaults in git; your live `*.json` files persist across updates.

### Tilt Configuration

Stored in `config/tilt_config.json`. Edit through **Batch Settings** in the dashboard.

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

Stored in `config/temp_control_config.json`. Edit through **⚙ gear → Temperature Control** in the dashboard.

Up to 3 independent controllers are supported. Each controller has:

| Field | Description |
|-------|-------------|
| `tilt_color` | Tilt color to read temperature from (must have an active Tilt) |
| `low_limit` | Lower temperature bound (°F or °C per system units setting) — heater turns on below this |
| `high_limit` | Upper temperature bound — cooler turns on above this |
| `power_off_variance` | Degrees of variance for early shutoff: heater turns off at *High Limit − Variance*, cooler at *Low Limit + Variance*. Prevents rapid on/off cycling. Set to 0 to use the limits directly |
| `max_run_minutes` | Maximum minutes a plug can stay on in one cycle (safety shutoff). Set to 0 to disable |
| `enable_heating` | `true` to allow the heating plug to switch on |
| `enable_cooling` | `true` to allow the cooling plug to switch on |
| `heating_plug` | IP address of the Kasa plug connected to the heater |
| `heating_plug_port` | Network port for the heating plug (leave blank for auto-detection; override only for non-standard setups) |
| `cooling_plug` | IP address of the Kasa plug connected to the cooler/fridge |
| `cooling_plug_port` | Network port for the cooling plug (leave blank for auto-detection) |
| `compressor_delay` | Minutes to wait before restarting the compressor after it was last switched off (protects the compressor) |
| `schedule_enabled` | `true` to use the temperature schedule instead of fixed limits |
| `schedule` | Array of schedule steps — each with a trigger type (days elapsed / gravity / fermentation complete), trigger value, low limit, and high limit |

**Control logic:** When temperature falls below `low_limit`, the heater turns on (if enabled). When temperature rises above `high_limit`, the cooler turns on (if enabled). The `power_off_variance` causes early shutoff to avoid overshoot. `max_run_minutes` provides a safety cutoff for any single run cycle. Temperature control pauses automatically if the Tilt goes offline.

**Temperature Schedule:** Define steps that automatically change Low/High Limits as fermentation progresses. Each step has a trigger (e.g., "after 3 days" or "when gravity ≤ 1.020") and the desired limits for that stage. When the schedule is active it overrides the manual limits.

### Notifications

Configure in **⚙ gear → System Settings → PUSH/eMail tab**.

Two independent channels are available:

#### ntfy (free, no account required — recommended)
1. Install the ntfy app on your phone (iOS / Android) — or use the web at [ntfy.sh](https://ntfy.sh)
2. Subscribe to a unique topic name of your choosing
3. Enter the same topic name in System Settings → ntfy Topic
4. Optionally enter a custom ntfy server URL for self-hosted instances
5. Set `warning_mode` to `"PUSH"` (or `"BOTH"`)

#### Pushover
1. Create an account at [pushover.net](https://pushover.net)
2. Create an application to obtain an API token
3. Enter your **User Key** and **API Token** in System Settings
4. Set `warning_mode` to `"PUSH"` (or `"BOTH"` for push + email)

#### SMTP Email

Works with Gmail (App Password required) or any SMTP relay.

| SMTP provider | Host | Port | Notes |
|---|---|---|---|
| Gmail | `smtp.gmail.com` | `587` | Requires a 16-character [App Password](https://support.google.com/accounts/answer/185833) — see [Setting Up Gmail](#setting-up-gmail-email-alerts) |
| Generic relay | your server | 25 / 465 / 587 | SSL on 465, STARTTLS on 587 |

> **Note:** Outlook / Hotmail no longer supports SMTP access for third-party apps and is not a supported option.

Set `warning_mode` to `"EMAIL"` (or `"BOTH"`) after saving credentials. Use the **Test Email** button to confirm delivery.

### External Logging

Fermentatorium can forward gravity and temperature readings to third-party logging services. Configure in **⚙ gear → System Settings → Logging Integrations tab**.

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
| **HS103** ⭐ **Preferred** | Single compact outlet, widely available — recommended for most users |
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

The easiest way to update is through the dashboard: open **⚙ gear → System Settings → Backup / Restore / Update tab** and click **Check for Updates** / **Update System**. Fermentatorium downloads the latest code from GitHub. After the update completes, you will be prompted to press the **Reboot** button on the Exit screen to restart the application with the new code.

To update manually from the command line:

```bash
cd /opt/fermentatorium   # or wherever you cloned the repo
git pull
sudo systemctl restart fermentatorium.service
```

Your configuration files (`config/*.json`) and batch data (`batches/`) are never touched by a `git pull`.

---

## Backup and Restore

From the dashboard, open **⚙ gear → System Settings → Backup & Restore / Update tab**:

- **Create Backup** — writes a `.tar.gz` archive containing config files, batch data, logs, and UI assets to the configured USB path.
- **Restore** — restore a previous `.tar.gz` backup from the configured USB path.
- **Automatic Dropbox Backups** — configure a Dropbox access token, target folder, and interval, then enable automatic backups.
- Dropbox backups use a **5-slot rotation**: backup #6 overwrites slot #1, backup #7 overwrites slot #2, and so on.

**Steps to Generate a Dropbox Access Token:**

1. **Log in to Developer Console** — Visit the [Dropbox Developer Website](https://www.dropbox.com/developers) and log in.
2. **Create App** — Click **Create App** and select **Scoped access**.
3. **Configure Scopes** — Choose the app type (**App Folder**) and name it.
4. **Set Permissions** — Navigate to the **Permissions** tab and select `files.content.read`, `files.content.write`, and `files.metadata.write`.
5. **Generate Token** — Go to the **Settings** tab, scroll to the **OAuth 2** section, and click **Generate** under "Generated access token".
6. **Copy Token** — Save the generated token securely.
7. **Set the Dropbox Folder correctly** — In Fermentatorium, enter a path inside the app folder root, such as `/FermentatoriumBackups`. Do **not** enter `/Apps/<app name>/...`; App Folder tokens already operate inside that Dropbox app directory.

USB backups are stored at your configured mount path (default `/media/usb`).

---

## Utilities

The **⚙ gear → Utilities** page in the dashboard provides access to data management and maintenance tools:

| Utility | Description |
|--------|-------------|
| **Load Demo Data** | Populate the system with sample fermentation data to explore the UI without live hardware |
| **Verify Demo Data** | Check that demo data was loaded correctly |
| **Compact & Archive Logs** | Split old tilt readings out of the temperature control log to reduce file size |
| **Purge Excess Tilt Readings** | Remove duplicate or over-frequent readings from batch files |

The `utils/` directory also contains command-line-only scripts:

| Script | Description |
|--------|-------------|
| `import_brewers_friend.py` | Convert a Brewer's Friend JSON export to Fermentatorium's JSONL batch format |
| `backfill_temp_control_jsonl.py` | Backfill temperature control events from legacy log formats |

> **Should utilities require a password?** Currently, utilities are accessible to anyone who can reach the dashboard on your local network. Since Fermentatorium is designed for home use on a private network, this is generally acceptable. If your network is shared (e.g., a club brewery), consider restricting access with your router's firewall rules or a reverse proxy with authentication (e.g., nginx with basic auth) in front of Fermentatorium.

---

## Demo Data

Fermentatorium ships with demo data capabilities so you can explore the full UI before connecting live hardware.

### Loading demo data

From the dashboard, go to **⚙ gear → Utilities → Load Demo Data**, or from the command line:

```bash
bash utils/setup_demo.sh
```

This populates several batch files with realistic fermentation data (gravity and temperature curves over a multi-day ferment). After loading:

- Open `http://<pi-ip>:5001` to see Tilt cards populated with demo readings.
- Open `http://<pi-ip>:5001/chart_plotly/Black` (or any loaded color) to view the interactive demo chart.
- Browse **Batch History** to see completed batch data.

### Verifying demo data

```bash
python3 utils/verify_demo_data.py
```

Reports whether demo files are present and correctly formatted.

### Importing from Brewer's Friend

If you have historical fermentation data in Brewer's Friend, you can import it into Fermentatorium to view it in charts and batch history.

**Export from Brewer's Friend:** In Brewer's Friend, open a batch and export it as a JSON file.

**Import into Fermentatorium** (command line):

```bash
python3 utils/import_brewers_friend.py export.json \
    --color Red \
    --beer-name "My IPA" \
    --batch-name "Batch 7"
```

Replace `Red` with the Tilt color you want to associate with the imported batch, and fill in your beer and batch names. After import, the batch will appear in Batch History and its gravity and temperature data will be available in the Plotly chart.

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

`start.sh` automatically frees port 5001 before launching. If you still see a conflict, check `app.log` or the systemd journal for the conflicting process and stop it manually.

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
