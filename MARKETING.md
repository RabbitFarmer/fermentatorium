# 🍺 The Tilt Fermentatorium
## *Your Brewery's Always-On Fermentation Command Center*

**Free · Open Source · Raspberry Pi · MIT License**

---

### Take the Guesswork Out of Fermentation

The **Tilt Fermentatorium** turns your Raspberry Pi into a full-featured fermentation monitor and temperature controller. Whether you're watching over one batch or managing three fermenters at once, Fermentatorium gives you real-time gravity and temperature data, automated temperature control, interactive charts, and instant alerts — all from a beautiful web dashboard accessible from anywhere on your home network.

---

### ✅ Key Features at a Glance

| Capability | Details |
|---|---|
| **Multi-Tilt Monitoring** | All 8 Tilt colors (Black, Blue, Green, Orange, Pink, Purple, Red, Yellow) — standard, Pro, and Mini-Pro models |
| **3 Independent Temperature Controllers** | Simultaneous heating and/or cooling for up to 3 fermenters using TP-Link Kasa smart plugs |
| **Live Web Dashboard** | Real-time gravity & temperature display — use with a monitor or go fully headless |
| **Interactive Charts** | Gravity trends, temperature history, and heating/cooling events powered by Plotly |
| **External Logging** | Post data to Brewer's Friend, BrewFather, or any custom endpoint on a configurable interval |
| **Push & Email Alerts** | Pushover, ntfy, or SMTP email — 11 configurable notification types |
| **CSV Data Export** | Download batch and temperature-control logs for offline analysis |
| **Remote Access** | Secure browser-based access via Raspberry Pi Connect — no VPN or port forwarding needed |
| **Privacy-First** | All config, batch data, and logs stay on your system — nothing tracked remotely |

---

### 🔔 Smart Notifications (11 Event Types)

Never miss a critical moment in fermentation:

- 📉 **Fermentation Started** — gravity drops 0.010+ points confirmed across 3 readings
- 🏁 **Fermentation Complete** — gravity stable (±0.002) for 24 hours
- 📊 **Daily Progress Report** — scheduled summary of gravity changes
- 📡 **Tilt Signal Lost** — alert when your Tilt goes quiet (configurable timeout, default 30 min)
- 🌡️ **Temperature Out of Range** — alert when temp exceeds your high or low limit
- 🔥 / ❄️ **Heating & Cooling Events** — optional alerts when plugs switch on or off
- 🔌 **Kasa Plug Failure** — notified immediately if heating/cooling equipment goes offline

All notifications include smart deduplication and automatic retry with exponential backoff (up to 3 attempts).

---

### 🌡️ Flexible Temperature Control

Configure each of the 3 controllers independently:

- **Heating Only** — one Kasa plug per fermenter
- **Cooling Only** — one Kasa plug per fermenter
- **Heating + Cooling** — two Kasa plugs, automatic midpoint switching
- **Safety Shutoff** — temperature control stops automatically if the Tilt signal is lost

---

### ⚡ One-Command Install

Getting started takes a single command on a fresh Raspberry Pi OS:

```bash
curl -sSL https://raw.githubusercontent.com/RabbitFarmer/fermentatorium/main/installer/automated-install.sh | sudo bash
```

Then open your browser to `http://<your-pi-ip>:5001` — that's it!

---

### 🛠️ What You Need

| Item | Notes |
|---|---|
| Raspberry Pi | Any model running Raspberry Pi OS with Python 3 & Bluetooth |
| Tilt Hydrometer(s) | Standard, Pro, or Mini-Pro — any color(s) |
| TP-Link Kasa Smart Plug(s) | Optional — for temperature control (15 A rated) |
| Home Network | Wi-Fi or Ethernet for dashboard access |

---

### 📥 Download & Get Started

> **GitHub Repository:** [https://github.com/RabbitFarmer/fermentatorium](https://github.com/RabbitFarmer/fermentatorium)

```bash
# Clone and install manually
git clone https://github.com/RabbitFarmer/fermentatorium.git
cd fermentatorium
sudo ./install.sh
```

**License:** MIT — free to use, modify, and share.  
**Author:** RabbitFarmer (Frank Cunningham) © 2026

---

*Brew better. Ferment smarter. Relax more.* 🍻
