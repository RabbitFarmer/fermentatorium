#!/usr/bin/env python3
"""
kasa_worker.py - worker process for controlling Kasa plugs.

This worker is intended to be started as a separate Process from app.py:
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
except Exception as _kasa_iot_err:
    try:
        from kasa import SmartPlug as PlugClass  # type: ignore
        HAS_IOT = False
    except Exception as _kasa_smart_err:
        PlugClass = None
        HAS_IOT = False
        # Print immediately so the error is visible in app.log / terminal output.
        print(
            f"[kasa_worker] WARNING: python-kasa not importable — plug control disabled.\n"
            f"  IotPlug import error:  {_kasa_iot_err}\n"
            f"  SmartPlug import error: {_kasa_smart_err}\n"
            f"  Run:  pip install python-kasa  (inside your virtual environment)"
        )

# Device.connect is available in python-kasa >= 0.6 and supports the KLAP
# protocol used by newer Kasa devices (e.g. EP25 hardware v2.6+).
try:
    from kasa import Device as _Device, DeviceConfig as _DeviceConfig, Credentials as _Credentials  # type: ignore
    HAS_DEVICE_CONNECT = True
except Exception:
    HAS_DEVICE_CONNECT = False

# Import application logger helpers if available (non-blocking)
try:
    from logger import log_error, log_kasa_diag
except Exception:
    def log_error(msg, **extra):
        try:
            print(f"[kasa_worker][ERROR] {msg}")
        except Exception:
            pass
    def log_kasa_diag(level, msg, **extra):
        try:
            extra_str = (' ' + str(extra)) if extra else ''
            print(f"[kasa_worker][{level.upper()}] {msg}{extra_str}")
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

    # ── Prevent atexit cascade ────────────────────────────────────────────
    # Python's 'fork' start method copies the parent's multiprocessing
    # _children registry into every child.  When a child exits its atexit
    # handler would call .terminate() on every Process object inherited from
    # the parent — i.e. on sibling kasa_worker processes — killing them all
    # whenever any single worker exits.  Clearing _children here breaks that
    # cascade.  This must run before any other code so the atexit handler
    # has nothing to clean up.
    try:
        from multiprocessing.process import _children as _mp_children
        _mp_children.clear()
    except Exception:
        pass

    # Worker started - minimal diagnostic output
    log_kasa_diag('info', 'kasa_worker process started',
                  pid=os.getpid(),
                  plug_class=PlugClass.__name__ if PlugClass else None)

    # Timeout used for cmd_queue.get() calls.  A finite value (rather than an
    # unlimited block) ensures that an orphaned worker — one whose parent app.py
    # has been replaced — eventually exits cleanly when the queue pipe closes
    # instead of spinning in a tight EOFError loop.
    _QUEUE_GET_TIMEOUT = 30  # seconds

    if PlugClass is None:
        err = "kasa library not available"
        log_error(err)
        log_kasa_diag('error', 'kasa_worker: PlugClass is None — plug control disabled')
        # Drain commands and return failure results so the controller doesn't hang.
        while True:
            try:
                cmd = cmd_queue.get(timeout=_QUEUE_GET_TIMEOUT)
                if not isinstance(cmd, dict):
                    continue
                result_queue.put({
                    'mode': cmd.get('mode', 'unknown'),
                    'action': cmd.get('action', 'off'),
                    'success': False,
                    'url': cmd.get('url', ''),
                    'error': err
                })
            except _queue.Empty:
                pass
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
        t0 = time.time()
        log_kasa_diag('info', f'kasa_worker: executing {mode} {action.upper()}',
                      url=url, mode=mode, action=action)
        try:
            error = await kasa_control(url, action, mode)
        except Exception as e:
            error = str(e)
            log_error(f"{mode.upper()} kasa_control run failed: {error}")
        elapsed_ms = round((time.time() - t0) * 1000)
        if error is None:
            log_kasa_diag('info', f'kasa_worker: {mode} {action.upper()} succeeded',
                          url=url, mode=mode, action=action, elapsed_ms=elapsed_ms)
        else:
            log_kasa_diag('error', f'kasa_worker: {mode} {action.upper()} failed',
                          url=url, mode=mode, action=action,
                          error=error, elapsed_ms=elapsed_ms)
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
                # Block until at least one command arrives.
                # Use a timeout so a broken parent pipe (orphaned worker)
                # doesn't spin the CPU in a tight exception loop.
                try:
                    first = cmd_queue.get(timeout=_QUEUE_GET_TIMEOUT)
                except _queue.Empty:
                    continue
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
                    # Wrap batch execution with an overall timeout so the loop
                    # can never be blocked permanently even if individual command
                    # timeouts are somehow bypassed.  Per-command worst case:
                    # update(6s) + turn_on(10s) + sleep(1.5s) + verify(5s) ≈ 23s,
                    # times 3 retries ≈ 69s.  Allow 120s for a full batch.
                    async def _run_batch_with_timeout(b):
                        try:
                            await asyncio.wait_for(_run_batch(b), timeout=120)
                        except (asyncio.TimeoutError, asyncio.CancelledError) as batch_exc:
                            err_label = 'Batch execution timed out' if isinstance(batch_exc, asyncio.TimeoutError) else 'Batch cancelled'
                            log_error(f"kasa_worker: {err_label} — returning errors for remaining commands")
                            # Best-effort: put error results so pending flags are cleared
                            for cmd in b:
                                try:
                                    result_queue.put_nowait({
                                        'mode': cmd.get('mode', 'unknown'),
                                        'action': cmd.get('action', 'off'),
                                        'success': False,
                                        'url': cmd.get('url', ''),
                                        'error': err_label,
                                    })
                                except Exception:
                                    pass
                    loop.run_until_complete(_run_batch_with_timeout(batch))

            except BaseException as e:
                # Re-raise signals and process exits; catch everything else
                # (including asyncio.CancelledError which is BaseException in Python ≥ 3.8)
                # so that a transient async failure never terminates the worker process.
                if isinstance(e, (SystemExit, KeyboardInterrupt)):
                    raise
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

async def kasa_query_state(url, credentials=None):
    """
    Query the current state of a plug without changing it.

    Args:
        url: IP address or hostname of the plug.
        credentials: kasa.Credentials instance for devices that require
            authentication (e.g. EP25 hardware v2.6+), or None for older
            devices.

    Returns:
      (is_on, error) tuple where:
        - is_on is True/False if successful, None if failed
        - error is None on success, error string on failure
    """
    if PlugClass is None and not HAS_DEVICE_CONNECT:
        return None, "kasa plug class not available"

    device = None
    needs_disconnect = False
    _connect_timeout = 6
    try:
        if credentials is not None and HAS_DEVICE_CONNECT:
            config = _DeviceConfig(host=url, credentials=credentials, timeout=_connect_timeout)
            device = await asyncio.wait_for(_Device.connect(config=config),
                                            timeout=_connect_timeout + 2)
            needs_disconnect = True
        elif PlugClass is not None:
            device = PlugClass(url)
        else:
            return None, "kasa plug class not available"
        await asyncio.wait_for(device.update(), timeout=6)
        is_on = getattr(device, "is_on", None)
        if is_on is None:
            return None, "Unable to determine plug state"
        return is_on, None
    except Exception as e:
        err = f"Failed to query plug at {url}: {e or type(e).__name__}"
        log_error(err)
        return None, err
    finally:
        if needs_disconnect and device is not None:
            try:
                await device.disconnect()
            except Exception:
                pass

async def kasa_control(url, action, mode, credentials=None):
    """
    Perform the plug action and verify resulting state with retry logic.

    Args:
        url: IP address or hostname of the plug.
        action: 'on' or 'off'.
        mode: Human-readable role label used in error messages.
        credentials: kasa.Credentials instance for devices that require
            authentication (e.g. EP25 hardware v2.6+), or None for older
            devices.

    Implements retry logic as recommended for TP-Link Kasa smart plugs to handle
    network instability and transient failures. Retries up to 3 times with
    exponential backoff delays.
    
    Returns:
      None on success
      error string on failure
    """
    if PlugClass is None and not HAS_DEVICE_CONNECT:
        return "kasa plug class not available"

    # Retry configuration: up to 3 attempts with delays
    max_retries = 3
    retry_delays = [0, 1, 2]  # First attempt immediate, then 1s, then 2s
    
    last_error = None
    
    for attempt in range(max_retries):
        if attempt > 0:
            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            await asyncio.sleep(delay)

        device = None
        needs_disconnect = False
        _connect_timeout = 6
        try:
            if credentials is not None and HAS_DEVICE_CONNECT:
                config = _DeviceConfig(host=url, credentials=credentials, timeout=_connect_timeout)
                device = await asyncio.wait_for(_Device.connect(config=config),
                                                timeout=_connect_timeout + 2)
                needs_disconnect = True
            elif PlugClass is not None:
                device = PlugClass(url)
            else:
                last_error = "kasa plug class not available"
                break
            # Update device state before sending the command - wakes the plug
            # and ensures we have a fresh baseline for state verification.
            await asyncio.wait_for(device.update(), timeout=6)
            
        except Exception as e:
            last_error = f"Failed to contact plug at {url}: {e or type(e).__name__}"
            if needs_disconnect and device is not None:
                try:
                    await device.disconnect()
                except Exception:
                    pass
            if attempt < max_retries - 1:
                continue
            else:
                log_error(last_error)
                return last_error

        try:
            # Send the command.
            # asyncio.wait_for is required here: plug.turn_on/turn_off open a TCP
            # connection to the plug and can hang indefinitely when the plug accepts
            # the connection but never sends a response (firmware stall, network
            # issue).  Without a timeout this blocks the entire kasa_worker event
            # loop, preventing ALL subsequent commands from ever being processed.
            if action == 'on':
                await asyncio.wait_for(device.turn_on(), timeout=10)
            else:
                await asyncio.wait_for(device.turn_off(), timeout=10)

            # Pause to let state change propagate before verifying.
            # Some plugs (especially older models) take 1-2 s to reflect the
            # new state in their update response; 0.5 s was too short and
            # produced false "State mismatch" errors on otherwise healthy plugs.
            await asyncio.sleep(1.5)

            # Refresh state to verify command succeeded
            try:
                await asyncio.wait_for(device.update(), timeout=5)
            except Exception as e:
                # non-fatal: we'll still attempt to read is_on if available
                log_error(f"WARNING: State verification update failed for {mode} plug at {url}: {e}")

            is_on = getattr(device, "is_on", None)
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
        finally:
            if needs_disconnect and device is not None:
                try:
                    await device.disconnect()
                except Exception:
                    pass
    
    # Should not reach here, but return last error if we do
    return last_error or "Unknown error in kasa_control"
