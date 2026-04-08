"""kasa_manager/_worker.py — spawned subprocess entry point.

Design goals
~~~~~~~~~~~~
* Spawned (not forked): starts with a clean process image; inherits no parent
  file descriptors, multiprocessing._children registry, or asyncio state.
* Single process for all plugs across all controllers: eliminates the
  per-controller cascade-kill failure mode seen with the old architecture.
* Per-URL asyncio task isolation: a hung task for one plug does not block
  commands sent to other plugs.
* Two result queues:
    result_queue       — on/off command results (read by kasa_result_listener)
    query_result_queue — query results (read by KasaManager.query_sync)
"""

import os
import asyncio
import time
import queue as _queue

# Module-level timeout constants (seconds).
_MP_QUEUE_READ_TIMEOUT = 30   # blocking get() on the multiprocessing command queue
_DRAIN_TIMEOUT = 30           # blocking get() when draining queue in unavailable mode

# ── Defensive timezone environment ────────────────────────────────────────────
os.environ.setdefault("TZ", "UTC")
try:
    time.tzset()
except Exception:
    pass

# ── Monkey-patch zoneinfo for abbreviated timezone keys used by python-kasa ──
try:
    import zoneinfo
    from zoneinfo import ZoneInfo as _ZoneInfo

    _ZONE_ABBREV_MAP = {
        "EST": "America/New_York",
        "EDT": "America/New_York",
        "CST": "America/Chicago",
        "CDT": "America/Chicago",
        "MST": "America/Denver",
        "MDT": "America/Denver",
        "PST": "America/Los_Angeles",
        "PDT": "America/Los_Angeles",
        "UTC": "UTC",
    }

    class _ZoneInfoAlias:
        def __new__(cls, key):
            return _ZoneInfo(_ZONE_ABBREV_MAP.get(key, key))

    zoneinfo.ZoneInfo = _ZoneInfoAlias
except Exception:
    pass

# ── Logger (best-effort; falls back to print) ─────────────────────────────────
try:
    from logger import log_error, log_kasa_diag  # type: ignore
except Exception:
    def log_error(msg, **_kw):
        try:
            print(f"[kasa_manager_worker][ERROR] {msg}")
        except Exception:
            pass

    def log_kasa_diag(level, msg, **kw):
        try:
            extra = (" " + str(kw)) if kw else ""
            print(f"[kasa_manager_worker][{level.upper()}] {msg}{extra}")
        except Exception:
            pass


# ── Worker entry point ────────────────────────────────────────────────────────
def worker_main(cmd_queue, result_queue, query_result_queue,
                kasa_username="", kasa_password=""):
    """Entry point for the spawned kasa-manager worker subprocess.

    Parameters
    ----------
    cmd_queue          : multiprocessing.Queue — receives PlugCommand dicts
    result_queue       : multiprocessing.Queue — sends on/off PlugResult dicts
    query_result_queue : multiprocessing.Queue — sends query PlugResult dicts
    kasa_username      : str — TP-Link account email (required for devices
                         using the KLAP protocol, e.g. EP25 hardware v2.6+)
    kasa_password      : str — TP-Link account password
    """

    log_kasa_diag("info", "kasa_manager worker starting",
                  pid=os.getpid(),
                  has_credentials=bool(kasa_username and kasa_password))

    # Build a Credentials object if credentials were supplied.
    _credentials = None
    if kasa_username and kasa_password:
        try:
            from kasa import Credentials as _Credentials  # type: ignore
            _credentials = _Credentials(username=kasa_username, password=kasa_password)
            log_kasa_diag("info", "kasa_manager worker: credentials loaded",
                          username=kasa_username)
        except Exception as cred_exc:
            log_error(f"kasa_manager worker: failed to build Credentials: {cred_exc}")

    # Import the plug helpers here (inside the spawned process) so that the
    # ZoneInfo patch above is already in effect when python-kasa loads.
    from kasa_manager._plug import plug_query, plug_control, KASA_AVAILABLE

    if not KASA_AVAILABLE:
        log_error("kasa_manager worker: python-kasa not available — draining commands and returning errors")
        _drain_unavailable(cmd_queue, result_queue, query_result_queue)
        return

    try:
        asyncio.run(_async_main(cmd_queue, result_queue, query_result_queue,
                                plug_query, plug_control, _credentials))
    except Exception as exc:
        log_error(f"kasa_manager worker top-level exception: {exc}")
        os._exit(1)


def _drain_unavailable(cmd_queue, result_queue, query_result_queue):
    """When kasa is unavailable, drain the command queue and return errors."""
    err = "kasa library not available"
    while True:
        try:
            cmd = cmd_queue.get(timeout=_DRAIN_TIMEOUT)
            if not isinstance(cmd, dict):
                continue
            result = {
                "request_id":    cmd.get("request_id", ""),
                "controller_id": cmd.get("controller_id", -1),
                "role":          cmd.get("role", ""),
                "url":           cmd.get("url", ""),
                "action":        cmd.get("action", "unknown"),
                "success":       False,
                "error":         err,
                "elapsed_ms":    0,
                "state":         None,
            }
            if cmd.get("action") == "query":
                query_result_queue.put(result)
            else:
                result_queue.put(result)
        except _queue.Empty:
            pass
        except Exception:
            time.sleep(0.5)


async def _async_main(cmd_queue, result_queue, query_result_queue, plug_query, plug_control,
                      credentials=None):
    """Main asyncio coroutine.  Bridges the blocking multiprocessing queue to
    per-URL asyncio task queues so commands for different plugs run in parallel
    while commands for the same plug are serialised.
    """

    loop = asyncio.get_event_loop()

    # Asyncio queue fed by the reader thread below.
    aio_cmd_q: asyncio.Queue = asyncio.Queue()

    # Per-URL asyncio queues and driver tasks.
    url_queues: dict[str, asyncio.Queue] = {}
    url_tasks: dict[str, asyncio.Task] = {}

    # ── Reader thread: blocking multiprocessing.Queue → asyncio.Queue ─────
    def _mp_reader():
        while True:
            try:
                cmd = cmd_queue.get(timeout=_MP_QUEUE_READ_TIMEOUT)
                loop.call_soon_threadsafe(aio_cmd_q.put_nowait, cmd)
            except _queue.Empty:
                continue
            except Exception as exc:
                try:
                    log_error(f"kasa_manager worker reader thread error: {exc}")
                except Exception:
                    pass
                break

    import threading
    reader = threading.Thread(target=_mp_reader, daemon=True, name="kasa-mp-reader")
    reader.start()

    # ── Per-URL driver coroutine ───────────────────────────────────────────
    async def _url_driver(url: str, url_q: asyncio.Queue):
        """Process commands for one URL sequentially."""
        while True:
            cmd = await url_q.get()
            await _execute(cmd)
            url_q.task_done()

    # ── Command executor ──────────────────────────────────────────────────
    async def _execute(cmd: dict):
        action        = cmd.get("action", "off")
        url           = cmd.get("url", "")
        controller_id = cmd.get("controller_id", -1)
        role          = cmd.get("role", "")
        request_id    = cmd.get("request_id", "")
        port          = cmd.get("port")     # int or None
        timeout       = cmd.get("timeout", 7.0)  # per-request timeout (seconds)

        if not url:
            _put_error(result_queue, query_result_queue, cmd, "No URL provided")
            return

        t0 = time.time()

        if action == "query":
            log_kasa_diag("info", "kasa_manager worker: querying plug",
                          url=url, port=port, controller_id=controller_id, role=role)
            is_on, error = await plug_query(url, credentials=credentials,
                                            timeout=timeout, port=port)
            elapsed_ms = round((time.time() - t0) * 1000)
            state = ("on" if is_on else "off") if is_on is not None else None
            if error is None:
                log_kasa_diag("info", "kasa_manager worker: query OK",
                              url=url, state=state, elapsed_ms=elapsed_ms)
            else:
                log_kasa_diag("error", "kasa_manager worker: query FAILED",
                              url=url, port=port, error=error, elapsed_ms=elapsed_ms)
            query_result_queue.put({
                "request_id":    request_id,
                "controller_id": controller_id,
                "role":          role,
                "url":           url,
                "action":        "query",
                "success":       error is None,
                "error":         error,
                "elapsed_ms":    elapsed_ms,
                "state":         state,
            })
        else:
            log_kasa_diag("info", f"kasa_manager worker: executing {role} {action.upper()}",
                          url=url, controller_id=controller_id, role=role)
            error = await plug_control(url, action, mode=role, credentials=credentials, port=port)
            elapsed_ms = round((time.time() - t0) * 1000)
            if error is None:
                log_kasa_diag("info", f"kasa_manager worker: {role} {action.upper()} succeeded",
                              url=url, elapsed_ms=elapsed_ms)
            else:
                log_kasa_diag("error", f"kasa_manager worker: {role} {action.upper()} FAILED",
                              url=url, error=error, elapsed_ms=elapsed_ms)
            result_queue.put({
                "request_id":    request_id,
                "controller_id": controller_id,
                "role":          role,
                "url":           url,
                "action":        action,
                "success":       error is None,
                "error":         error,
                "elapsed_ms":    elapsed_ms,
                "state":         None,
            })

    # ── Dispatcher loop ───────────────────────────────────────────────────
    while True:
        cmd = await aio_cmd_q.get()
        if not isinstance(cmd, dict):
            continue
        url = cmd.get("url", "")
        if not url:
            await _execute(cmd)
            continue
        if url not in url_queues:
            url_queues[url] = asyncio.Queue()
            url_tasks[url] = asyncio.create_task(_url_driver(url, url_queues[url]))
        await url_queues[url].put(cmd)


def _put_error(result_queue, query_result_queue, cmd: dict, error: str):
    result = {
        "request_id":    cmd.get("request_id", ""),
        "controller_id": cmd.get("controller_id", -1),
        "role":          cmd.get("role", ""),
        "url":           cmd.get("url", ""),
        "action":        cmd.get("action", "unknown"),
        "success":       False,
        "error":         error,
        "elapsed_ms":    0,
        "state":         None,
    }
    if cmd.get("action") == "query":
        query_result_queue.put(result)
    else:
        result_queue.put(result)
