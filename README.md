# Three Controller - Fermentation Monitor

This project is a Raspberry Pi-based fermentation monitor and temperature controller for homebrewing. It uses Tilt hydrometers and TP-Link Kasa smart plugs to manage and log fermentation temperature with a web dashboard.

> **Privacy Note for New Users:** This repository does not contain any personal data. Your configuration files, batch data, and logs are automatically created on your system and remain private - they are not tracked by git.

## Features

- **Multi-Device Tilt Support**: Track multiple Tilt hydrometers simultaneously for fermentation monitoring
  - Supports all 8 colors of the standard Tilt Hydrometer (Black, Blue, Green, Orange, Pink, Purple, Red, Yellow)
  - Each Tilt can be independently monitored for fermentation data
- **Three Independent Temperature Controllers**: Control up to 3 fermenters simultaneously
  - Each controller can be assigned to a different Tilt color
  - Independent temperature limits (high/low) for each controller
  - Each controller manages its own heating and cooling Kasa smart plugs (15 amp rated)
  - Controllers operate completely independently of one another
  - Temperature control UI is integrated directly into each Tilt's card for easy monitoring
- **Interactive Charting**: Real-time charts for both Tilt-tracked fermentation data and temperature control monitoring
  - Fermentation charts display gravity and temperature trends over time
  - Temperature control charts show heating/cooling events and temperature readings
  - Powered by Plotly for interactive zooming, panning, and data exploration
- **CSV Data Export**: Export fermentation batch data and temperature control logs to CSV format for external analysis
- Reads Tilt hydrometer data via Bluetooth (BLE)
- Web dashboard for monitoring and configuration (Flask)
  - Accessible from anywhere on your home network
  - Remote access via [Raspberry Pi Connect](https://connect.raspberrypi.com) (free, no VPN required)
- Batch history and temperature logging to JSONL/CSV
- **Email/Push notifications for fermentation status and temperature alerts**
  - Temperature control alerts (temp out of range, heating/cooling events, Kasa plug failures)
  - Batch alerts (signal loss, fermentation starting, daily reports)
  - Configurable notification settings per event type
  - See [NOTIFICATIONS.md](NOTIFICATIONS.md) for detailed configuration guide
  - See [Notification Types](#notification-types) section below for complete list of 11 notification types

## Getting Started

### Prerequisites

- Raspberry Pi (recommended)
- Python 3.7+
- Bluetooth enabled (for Tilt)
- TP-Link Kasa plugs for temperature control
...