"""Thin async wrapper around python-kasa IotPlug / SmartPlug.

All network operations are wrapped with asyncio.wait_for to prevent
indefinite hangs when a plug accepts a TCP connection but never responds.
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

KASA_AVAILABLE = PLUG_CLASS is not None


async def plug_query(url: str, timeout: float = 7.0):
    """Query the current on/off state of a plug.

    Returns:
        (is_on, error) where is_on is True/False on success, None on failure,
        and error is None on success or an error string on failure.
    """
    if PLUG_CLASS is None:
        return None, "kasa library not available"
    try:
        plug = PLUG_CLASS(url)
        await asyncio.wait_for(plug.update(), timeout=timeout)
        is_on = getattr(plug, "is_on", None)
        if is_on is None:
            return None, "Unable to determine plug state"
        return bool(is_on), None
    except Exception as e:
        return None, f"Failed to query plug at {url}: {e or type(e).__name__}"


async def plug_control(
    url: str,
    action: str,
    mode: str = "",
    timeout_update: float = 6.0,
    timeout_cmd: float = 10.0,
    timeout_verify: float = 5.0,
    max_retries: int = 3,
):
    """Turn a plug on or off with verification and retry logic.

    Returns:
        None on success, or an error string on failure.
    """
    if PLUG_CLASS is None:
        return "kasa library not available"

    retry_delays = [0, 1, 2]
    last_error = None

    for attempt in range(max_retries):
        if attempt > 0:
            await asyncio.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])

        # Wake the plug and get a fresh baseline state.
        try:
            plug = PLUG_CLASS(url)
            await asyncio.wait_for(plug.update(), timeout=timeout_update)
        except Exception as e:
            last_error = f"Failed to contact plug at {url}: {e or type(e).__name__}"
            continue

        # Send the command.
        try:
            if action == "on":
                await asyncio.wait_for(plug.turn_on(), timeout=timeout_cmd)
            else:
                await asyncio.wait_for(plug.turn_off(), timeout=timeout_cmd)

            # Allow state to propagate before verifying.
            await asyncio.sleep(1.5)

            # Verify the new state.
            try:
                await asyncio.wait_for(plug.update(), timeout=timeout_verify)
            except Exception:
                pass  # non-fatal; attempt to read is_on anyway

            is_on = getattr(plug, "is_on", None)
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

    return last_error or "Unknown error in plug_control"
