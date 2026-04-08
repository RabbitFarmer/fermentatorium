"""Thin async wrapper around python-kasa IotPlug / SmartPlug.

All network operations are wrapped with asyncio.wait_for to prevent
indefinite hangs when a plug accepts a TCP connection but never responds.

Newer Kasa devices (e.g. EP25 hardware v2.6+) require TP-Link account
credentials and use the KLAP protocol over port 80 instead of port 9999.
When a Credentials object is supplied, Device.connect() is used so that
python-kasa can auto-negotiate the correct protocol.  Older devices that
do not need authentication continue to use IotPlug as before.
"""

from __future__ import annotations

import asyncio

# Discover the best available plug class at import time.
try:
    from kasa.iot import IotPlug as PLUG_CLASS   # type: ignore
except ImportError:
    try:
        from kasa import SmartPlug as PLUG_CLASS  # type: ignore
    except ImportError:
        PLUG_CLASS = None

# Device.connect + DeviceConfig are available in python-kasa >= 0.6.
try:
    from kasa import Device as _Device, DeviceConfig as _DeviceConfig  # type: ignore
    HAS_DEVICE_CONNECT = True
except ImportError:
    HAS_DEVICE_CONNECT = False

KASA_AVAILABLE = PLUG_CLASS is not None or HAS_DEVICE_CONNECT

# Retry delays (seconds) between consecutive attempts: immediate, then 1 s, then 2 s.
_RETRY_DELAYS = [0, 1, 2]


async def _open_device(url: str, credentials, timeout: float, port: int | None = None):
    """Open a connection to a plug and return (device, needs_disconnect).

    When *credentials* is a kasa.Credentials object, Device.connect() is used
    so that python-kasa can auto-negotiate the correct protocol (KLAP for new
    devices, legacy encrypted for old ones).  The library handles port selection
    automatically; *port* is only needed for non-standard network configurations.

    Without credentials, IotPlug is used for backward-compatible, credential-free
    access on port 9999.  Device.connect() without credentials cannot complete the
    KLAP authentication handshake that newer devices require, so this path is not
    attempted.
    """
    if credentials is not None and HAS_DEVICE_CONNECT:
        # Use DeviceConfig timeout slightly smaller than the outer asyncio.wait_for
        # timeout so the inner per-operation timeout fires first.
        config = _DeviceConfig(host=url, credentials=credentials,
                               timeout=max(1, int(timeout) - 2),
                               port_override=port)
        device = await asyncio.wait_for(_Device.connect(config=config), timeout=timeout)
        return device, True
    if PLUG_CLASS is None:
        raise RuntimeError("kasa library not available")
    # No credentials: use the legacy IotPlug/SmartPlug which operates on port 9999.
    # Device.connect() without credentials cannot authenticate with KLAP devices,
    # so we do not attempt it here regardless of the port setting.
    return PLUG_CLASS(url), False


async def plug_query(url: str, credentials=None, timeout: float = 7.0, port: int | None = None):
    """Query the current on/off state of a plug.

    Args:
        url: IP address or hostname of the plug.
        credentials: kasa.Credentials instance for devices that require
            authentication (e.g. EP25 hardware v2.6+), or None for older
            devices that use the unauthenticated protocol.
        timeout: Network operation timeout in seconds.
        port: Override the default port (9999 for old plugs, 80 for new KLAP
            plugs).  None uses the library default.

    Returns:
        (is_on, error) where is_on is True/False on success, None on failure,
        and error is None on success or an error string on failure.
    """
    if not KASA_AVAILABLE:
        return None, "kasa library not available"
    device = None
    needs_disconnect = False
    try:
        device, needs_disconnect = await asyncio.wait_for(
            _open_device(url, credentials, timeout, port), timeout=timeout
        )
        await asyncio.wait_for(device.update(), timeout=timeout)
        is_on = getattr(device, "is_on", None)
        if is_on is None:
            return None, "Unable to determine plug state"
        return bool(is_on), None
    except Exception as e:
        plug_desc = f"{url}:{port}" if port is not None else url
        return None, f"Failed to query plug at {plug_desc}: {str(e) or type(e).__name__}"
    finally:
        if needs_disconnect and device is not None:
            try:
                await device.disconnect()
            except Exception:
                pass


async def plug_control(
    url: str,
    action: str,
    mode: str = "",
    credentials=None,
    timeout_update: float = 6.0,
    timeout_cmd: float = 10.0,
    timeout_verify: float = 5.0,
    max_retries: int = 3,
    port: int | None = None,
):
    """Turn a plug on or off with verification and retry logic.

    Args:
        url: IP address or hostname of the plug.
        action: 'on' or 'off'.
        mode: Human-readable role label used in error messages ('heating' / 'cooling').
        credentials: kasa.Credentials instance for devices that require
            authentication (e.g. EP25 hardware v2.6+), or None for older devices.
        timeout_update: Seconds to wait for the initial state-fetch.
        timeout_cmd: Seconds to wait for the on/off command.
        timeout_verify: Seconds to wait for the post-command state verification.
        max_retries: Number of attempts before giving up.
        port: Override the default port (9999 for old plugs, 80 for new KLAP
            plugs).  None uses the library default.

    Returns:
        None on success, or an error string on failure.
    """
    if not KASA_AVAILABLE:
        return "kasa library not available"

    retry_delays = _RETRY_DELAYS
    last_error = None

    for attempt in range(max_retries):
        if attempt > 0:
            await asyncio.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])

        # Wake the plug and get a fresh baseline state.
        device = None
        needs_disconnect = False
        try:
            device, needs_disconnect = await asyncio.wait_for(
                _open_device(url, credentials, timeout_update, port), timeout=timeout_update
            )
            await asyncio.wait_for(device.update(), timeout=timeout_update)
        except Exception as e:
            plug_desc = f"{url}:{port}" if port is not None else url
            last_error = f"Failed to contact plug at {plug_desc}: {str(e) or type(e).__name__}"
            if needs_disconnect and device is not None:
                try:
                    await device.disconnect()
                except Exception:
                    pass
            continue

        # Send the command.
        try:
            if action == "on":
                await asyncio.wait_for(device.turn_on(), timeout=timeout_cmd)
            else:
                await asyncio.wait_for(device.turn_off(), timeout=timeout_cmd)

            # Allow state to propagate before verifying.
            await asyncio.sleep(1.5)

            # Verify the new state.
            try:
                await asyncio.wait_for(device.update(), timeout=timeout_verify)
            except Exception:
                pass  # non-fatal; attempt to read is_on anyway

            is_on = getattr(device, "is_on", None)
            if is_on is None:
                last_error = "Unable to determine plug state after command"
                continue

            if (action == "on" and is_on) or (action == "off" and not is_on):
                return None  # success

            last_error = (
                f"State mismatch after {action}: "
                f"expected is_on={action == 'on'}, got is_on={is_on}"
            )
        except Exception as e:
            last_error = str(e)
        finally:
            if needs_disconnect and device is not None:
                try:
                    await device.disconnect()
                except Exception:
                    pass

    return last_error or "Unknown error in plug_control"
