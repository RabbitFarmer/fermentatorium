# Fermentatorium

A Raspberry Pi-based fermentation monitoring and temperature control system using Tilt Hydrometers and TP-Link Kasa smart plugs.

---

## Equipment — Kasa Smart Plugs

Fermentatorium controls heating and cooling elements through TP-Link Kasa smart plugs.

### Plugs that work WITHOUT a TP-Link username/password

These models use the older local IOT protocol (port 9999) and respond to commands on your local network without any account login:

| Model | Notes |
|-------|-------|
| **HS100** | Single outlet, no energy monitoring |
| **HS103** | Single compact outlet, widely available |
| **HS105** | Mini dual outlet |
| **HS110** | Single outlet with energy monitoring |
| **KP115** | Single outlet with energy monitoring — **firmware 1.x only**; updating firmware may switch the device to KLAP (requires login) |

> **Tip:** If you already own one of the models above, disable automatic firmware updates in the Kasa app before first use to prevent the device from upgrading to a KLAP-based firmware that requires credentials.

### Plugs that REQUIRE a TP-Link username/password

These models use the newer KLAP protocol and require a TP-Link account email and password configured in Fermentatorium's System Settings:

| Model | Notes |
|-------|-------|
| **EP25** (firmware v2.6+) | The most common "new" Kasa outlet; earlier firmware versions may still work without credentials |
| **KP125M** | Matter-compatible outlet, always requires login |
| **KP400** | Outdoor double outlet, always requires login |

If you configure your TP-Link account credentials in **System Settings → Kasa**, Fermentatorium will attempt the KLAP handshake automatically for these newer devices.
