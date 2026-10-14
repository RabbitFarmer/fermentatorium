"""Thin async wrapper around python-kasa IotPlug / SmartPlug.

All network operations are wrapped with asyncio.wait_for to prevent
indefinite hangs when a plug accepts a TCP connection but never responds.

Plugs may speak either the legacy IOT protocol (TCP 9999, no account) or,
after a Kasa firmware update, the encrypted KLAP protocol (TCP 80, TP-Link
account credentials required).  A bare DeviceConfig/Device.connect() always
assumes the legacy protocol, so protocol detection is done with
Discover.discover_single().  If discovery is unavailable or fails for a
reason other than authentication, the legacy IotPlug path on port 9999 is
used so old plugs keep working without credentials.
"""

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

# Discover.discover_single detects the protocol (IOT vs KLAP) of a plug.
try:
    from kasa import Discover as _Discover  # type: ignore
    HAS_DISCOVER = True
except ImportError:
    _Discover = None
    HAS_DISCOVER = False

KASA_AVAILABLE = PLUG_CLASS is not None or HAS_DEVICE_CONNECT

# Retry delays (seconds) between consecutive attempts: immediate, then 1 s, then 2 s.
_RETRY_DELAYS = [0, 1, 2]


AUTH_REQUIRED_MESSAGE = (
    "This plug appears to use the newer encrypted protocol (port 80) and requires "
    "your TP-Link/Kasa account email and password. Enter them in System Settings > "
    "Kasa Account Credentials."
)
AUTH_REJECTED_MESSAGE = (
    "The plug rejected the TP-Link/Kasa account credentials. Check the email and "
    "password in System Settings > Kasa Account Credentials."
)


def _is_auth_error(exc: BaseException) -> bool:
    """True if *exc* looks like a python-kasa authentication/credentials failure."""
    for e in (exc, getattr(exc, "__cause__", None)):
        if e is None:
            continue
        name = type(e).__name__.lower()
        if "authentication" in name or "credentials" in name:
            return True
        msg = str(e).lower()
        if "credentials" in msg or "authentication" in msg or "login" in msg:
            return True
    return False


def _friendly_error(exc: BaseException, credentials) -> str:
    """Map an exception to a user-facing message (never includes credentials)."""
    if _is_auth_error(exc):
        return AUTH_REJECTED_MESSAGE if credentials is not None else AUTH_REQUIRED_MESSAGE
    text = str(exc) or type(exc).__name__
    if credentials is None and ":9999" in text and (
            "Errno 111" in text or "refused" in text.lower() or "Connect call failed" in text):
        text += " - " + AUTH_REQUIRED_MESSAGE.replace("appears to use", "may use")
    return text


async def _discover(url: str, credentials, timeout: float, port):
    """Run Discover.discover_single, tolerating python-kasa API differences."""
    inner = max(2, min(int(timeout) - 2, 6))
    kwargs = {"credentials": credentials, "timeout": inner, "discovery_timeout": min(inner, 4)}
    if port is not None:
        kwargs["port"] = port
    try:
        import inspect
        params = inspect.signature(_Discover.discover_single).parameters
        if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):
        pass
    return await asyncio.wait_for(_Discover.discover_single(url, **kwargs), timeout=timeout)


async def _open_device(url: str, credentials, timeout: float, port: int | None = None):
    """Open a connection to a plug and return (device, needs_disconnect).

    Discover.discover_single() is tried first, with or without credentials, so
    the protocol (legacy IOT/9999 or KLAP/80) is detected.  An explicit *port*
    is honoured; otherwise discovery decides.  If the plug needs authentication
    and none was supplied (or it is rejected) an actionable error is raised.
    If discovery fails for any other reason, the legacy IotPlug on port 9999 is
    used so old plugs keep working exactly as before.
    """
    if HAS_DISCOVER:
        try:
            device = await _discover(url, credentials, timeout, port)
            if device is not None:
                return device, True
        except Exception as e:
            if _is_auth_error(e):
                raise RuntimeError(_friendly_error(e, credentials)) from e
            # Fall through to the legacy path.
    if PLUG_CLASS is None:
        raise RuntimeError("kasa library not available")
    if port is not None:
        try:
            return PLUG_CLASS(url, port=port), False
        except TypeError:
            pass
    return PLUG_CLASS(url), False


async def plug_query(url: str, credentials=None, timeout: float = 10.0, port: int | None = None):
    """Query the current on/off state of a plug.

    Args:
        url: IP address or hostname of the plug.
        credentials: kasa.Credentials instance for devices that require
            authentication (e.g. EP25 hardware v2.6+ or firmware-updated HS103), or None.
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
        return None, f"Failed to query plug at {plug_desc}: {_friendly_error(e, credentials)}"
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
    timeout_update: float = 10.0,
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
            last_error = f"Failed to contact plug at {plug_desc}: {_friendly_error(e, credentials)}"
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
