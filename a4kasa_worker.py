#!/usr/bin/env python3
"""
a4kasa_worker.py - worker process for controlling Kasa plugs.

This worker is intended to be started as a separate Process from a4app.py:
    proc = Process(target=kasa_worker, args=(kasa_queue, kasa_result_queue))

Behavior:
- Monkey-patches zoneinfo.ZoneInfo to accept common timezone abbreviations
  (e.g. 'EST' -> 'America/New_York') before importing python-kasa so that
  device responses using abbreviated TZ keys don't raise ZoneInfoNotFoundError.
- Listens on cmd_queue for commands of the form:
    {'mode': 'heating'|'cooling', 'url': '<ip_or_host>', 'action': 'on'|'off'}
  Places a confirmation dict on result_queue:
    {'mode':..., 'action':..., 'success': True/False, 'url':..., 'error': '...'}
- Uses asyncio to interact with python-kasa plugs (IotPlug preferred).
"""

import os
import queue as _queue
import time
import asyncio

# Defensive TZ environment: ensure a sane TZ is available for zoneinfo fallback
os.environ.setdefault('TZ', 'UTC')
try:
    time.tzset()
except Exception:
    # tzset may not exist on some platforms (e.g., Windows), ignore
    pass

# --- Monkey-patch zoneinfo.ZoneInfo for common abbreviations ----------
try:
    import zoneinfo
    from zoneinfo import ZoneInfo as _ZoneInfo

    _ZONE_ABBREV_MAP = {
        'EST': 'America/New_York',
        'EDT': 'America/New_York',
        'CST': 'America/Chicago',
        'CDT': 'America/Chicago',
        'MST': 'America/Denver',
        'MDT': 'America/Denver',
        'PST': 'America/Los_Angeles',
        'PDT': 'America/Los_Angeles',
        'UTC': 'UTC'
    }

    class ZoneInfoAlias:
        def __new__(cls, key):
            mapped = _ZONE_ABBREV_MAP.get(key, key)
            return _ZoneInfo(mapped)

    # Apply monkeypatch so python-kasa (or other libraries) calling ZoneInfo(...)
    # get the aliasing behavior.
    zoneinfo.ZoneInfo = ZoneInfoAlias
except Exception:
    # If zoneinfo isn't available or monkeypatch fails, continue; errors will surface later.
    pass

# Now import kasa and other helpers (after zoneinfo patch)
try:
    # Prefer the new IOT API when available
    from kasa.iot import IotPlug as PlugClass  # type: ignore
    HAS_IOT = True
except Exception:
    try:
        from kasa import SmartPlug as PlugClass  # type: ignore
        HAS_IOT = False
    except Exception:
        PlugClass = None
        HAS_IOT = False

# Import application logger helper if available (non-blocking)
try:
    from logger import log_error
except Exception:
    def log_error(msg):
        # fallback logger
        try:
            print(f"[kasa_worker][ERROR] {msg}")
        except Exception:
            pass

# --- Worker implementation ---------------------------------------------
def kasa_worker(cmd_queue, result_queue):
    """
    Main loop: consume commands from cmd_queue and put confirmation dicts into result_queue.
    Each confirmation is a dict:
        {'mode': str, 'action': str, 'success': bool, 'url': str, 'error': str}

    Commands are dispatched concurrently: all commands that are waiting in the queue
    at the same moment are grouped and run as parallel asyncio tasks, one task per
    unique URL.  Commands that target the *same* URL are serialised within that task
    to avoid sending conflicting actions to one plug simultaneously.

    Creates a persistent event loop to avoid network binding issues that occur when
    asyncio.run() creates a new event loop for each command in multiprocessing workers.
    """

    # Worker started - minimal diagnostic output
    if PlugClass is None:
        err = "kasa library not available"
        log_error(err)
        # Drain commands and return failure results so the controller doesn't hang.
        while True:
            try:
                cmd = cmd_queue.get()
                if not isinstance(cmd, dict):
                    continue
                result_queue.put({
                    'mode': cmd.get('mode', 'unknown'),
                    'action': cmd.get('action', 'off'),
                    'success': False,
                    'url': cmd.get('url', ''),
                    'error': err
                })
            except Exception:
                time.sleep(0.5)
        # unreachable

    # Create a persistent event loop for this worker process
    # This avoids network binding issues that occur when creating new loops for each command
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run_command(command):
        """Run a single kasa command and push the result onto result_queue."""
        mode = command.get('mode', 'unknown')
        url = command.get('url', '')
        action = command.get('action', 'off')
        if not url:
            error = "No URL provided"
            log_error(f"{mode.upper()} plug operation skipped: {error}")
            result_queue.put({'mode': mode, 'action': action, 'success': False, 'url': url, 'error': error})
            return
        try:
            error = await kasa_control(url, action, mode)
        except Exception as e:
            error = str(e)
            log_error(f"{mode.upper()} kasa_control run failed: {error}")
        result_queue.put({'mode': mode, 'action': action,
                          'success': (error is None), 'url': url, 'error': error})

    async def _run_batch(commands):
        """
        Run a batch of commands concurrently, but serialise any commands that
        share the same target URL (so we never send conflicting on/off to one plug).
        """
        # Group by URL to enforce per-plug serialisation
        from collections import OrderedDict
        by_url = OrderedDict()
        for cmd in commands:
            by_url.setdefault(cmd.get('url', ''), []).append(cmd)

        async def _run_url_group(cmds):
            for cmd in cmds:
                await _run_command(cmd)

        # Run each URL-group as an independent asyncio task
        await asyncio.gather(*[_run_url_group(cmds) for cmds in by_url.values()])

    try:
        while True:
            try:
                # Block until at least one command arrives
                first = cmd_queue.get()
                batch = []
                if isinstance(first, dict):
                    batch.append(first)

                # Drain any additional commands already waiting (non-blocking)
                while True:
                    try:
                        cmd = cmd_queue.get_nowait()
                        if isinstance(cmd, dict):
                            batch.append(cmd)
                    except _queue.Empty:
                        break

                if batch:
                    loop.run_until_complete(_run_batch(batch))

            except Exception as e:
                # Defensive: log and sleep briefly, then continue
                try:
                    log_error(f"kasa_worker loop exception: {e}")
                except Exception:
                    print(f"[kasa_worker] loop exception (logging failed): {e}")
                time.sleep(0.5)
                continue
    finally:
        # Clean up event loop when worker exits
        try:
            loop.close()
        except Exception as e:
            log_error(f"Error closing event loop: {e}")

async def kasa_query_state(url):
    """
    Query the current state of a plug without changing it.
    Returns:
      (is_on, error) tuple where:
        - is_on is True/False if successful, None if failed
        - error is None on success, error string on failure
    """
    if PlugClass is None:
        return None, "kasa plug class not available"

    try:
        plug = PlugClass(url)
        await asyncio.wait_for(plug.update(), timeout=6)
        is_on = getattr(plug, "is_on", None)
        if is_on is None:
            return None, "Unable to determine plug state"
        return is_on, None
    except Exception as e:
        err = f"Failed to query plug at {url}: {e}"
        log_error(err)
        return None, err

async def kasa_control(url, action, mode):
    """
    Perform the plug action and verify resulting state with retry logic.
    
    Implements retry logic as recommended for TP-Link Kasa smart plugs to handle
    network instability and transient failures. Retries up to 3 times with
    exponential backoff delays.
    
    Returns:
      None on success
      error string on failure
    """
    if PlugClass is None:
        return "kasa plug class not available"

    # Retry configuration: up to 3 attempts with delays
    max_retries = 3
    retry_delays = [0, 1, 2]  # First attempt immediate, then 1s, then 2s
    
    last_error = None
    
    for attempt in range(max_retries):
        if attempt > 0:
            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            await asyncio.sleep(delay)
        
        try:
            plug = PlugClass(url)
            # Initial update to refresh device state - critical for reliability
            await asyncio.wait_for(plug.update(), timeout=6)
            
        except Exception as e:
            last_error = f"Failed to contact plug at {url}: {e}"
            if attempt < max_retries - 1:
                continue
            else:
                log_error(last_error)
                return last_error

        try:
            # Wake up the plug immediately before sending the command
            # This ensures the device is ready to receive and process the command
            await asyncio.wait_for(plug.update(), timeout=6)
            
            # Send the command
            if action == 'on':
                await plug.turn_on()
            else:
                await plug.turn_off()

            # Brief pause to let state change propagate - important for reliability
            await asyncio.sleep(0.5)

            # Refresh state to verify command succeeded
            try:
                await asyncio.wait_for(plug.update(), timeout=5)
            except Exception as e:
                # non-fatal: we'll still attempt to read is_on if available
                log_error(f"WARNING: State verification update failed for {mode} plug at {url}: {e}")

            is_on = getattr(plug, "is_on", None)
            if is_on is None:
                last_error = "Unable to determine plug state after command"
                if attempt < max_retries - 1:
                    continue
                else:
                    log_error(f"{mode.upper()} plug at {url}: {last_error}")
                    return last_error
            
            # Verify state matches expected result
            if (action == 'on' and is_on) or (action == 'off' and not is_on):
                # Success - state matches expected, return without logging
                return None
            else:
                last_error = f"State mismatch after {action}: expected is_on={action == 'on'}, actual is_on={is_on}"
                if attempt < max_retries - 1:
                    continue
                else:
                    log_error(f"{mode.upper()} plug at {url}: {last_error}")
                    return last_error

        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                continue
            else:
                log_error(f"{mode.upper()} plug at {url} error during command execution: {last_error}")
                return last_error
    
    # Should not reach here, but return last error if we do
    return last_error or "Unknown error in kasa_control"
