"""kasa_manager — public API.

Replaces the old per-controller kasa_queues[], kasa_result_queues[],
kasa_procs[], and _kasa_proc_locks[] arrays in app.py.

Key design differences from the old architecture
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* spawn, not fork: the worker subprocess starts with a clean process image so
  it cannot inherit parent's multiprocessing._children, open sockets, or
  asyncio state that caused the SIGTERM cascade in the old design.
* One subprocess for all plugs: eliminates the "all three workers die together"
  failure mode.  If the single worker dies the parent restarts it within 2 s
  and all pending commands are re-queued.
* Per-URL asyncio task isolation inside the worker: a hung plug cannot block
  commands sent to other plugs.
* Clean command/result protocol over multiprocessing.Queue (plain dicts).

Thread-safety
~~~~~~~~~~~~~
KasaManager is safe to call from multiple threads (the GIL protects the
multiprocessing.Queue puts, and the _lock guards the Process reference).
"""

from __future__ import annotations

import threading
import time
import uuid
from multiprocessing import Process, Queue
from typing import Optional

try:
    from logger import log_error, log_kasa_diag  # type: ignore
except Exception:
    def log_error(msg, **_kw):
        try:
            print(f"[KasaManager][ERROR] {msg}")
        except Exception:
            pass

    def log_kasa_diag(level, msg, **kw):
        try:
            extra = (" " + str(kw)) if kw else ""
            print(f"[KasaManager][{level.upper()}] {msg}{extra}")
        except Exception:
            pass


class KasaManager:
    """Owns the full lifecycle of all Kasa plugs (0-6 plugs, 0-3 controllers).

    Usage (from app.py)
    -------------------
    # At module level:
    kasa_manager = KasaManager()

    # In __main__ (after set_start_method('spawn')):
    kasa_manager.start()

    # In kasa_result_listener thread:
    result = kasa_manager.result_queue.get(timeout=5)

    # To send on/off commands:
    kasa_manager.send(controller_id=0, role='heating', url='192.168.1.x', action='on')

    # To query plug state synchronously (startup sync):
    is_on, error = kasa_manager.query_sync(url='192.168.1.x', timeout=20)

    # Watchdog:
    if not kasa_manager.is_alive():
        kasa_manager.restart()

    # Shutdown:
    kasa_manager.stop()
    """

    def __init__(self):
        self._cmd_queue = None          # parent -> worker
        self._result_queue = None       # worker -> parent (on/off results)
        self._query_queue = None        # worker -> parent (query results)
        self._proc = None               # type: Optional[Process]
        self._lock = threading.Lock()   # guards _proc
        self._kasa_username = ""        # TP-Link account email (for auth-required devices)
        self._kasa_password = ""        # TP-Link account password

    # -- Lifecycle ---------------------------------------------------------

    def start(self, kasa_username: str = "", kasa_password: str = ""):
        """Spawn the worker subprocess.  Call once from __main__ after
        multiprocessing.set_start_method('spawn') has been set.

        Args:
            kasa_username: TP-Link account email address.  Required for newer
                Kasa devices (e.g. EP25 hardware v2.6+) that use the KLAP
                protocol and will reject unauthenticated connections.
            kasa_password: TP-Link account password.
        """
        with self._lock:
            if self._proc is not None and self._proc.is_alive():
                return  # already running

            # Update stored credentials if new ones are provided.
            if kasa_username:
                self._kasa_username = kasa_username
            if kasa_password:
                self._kasa_password = kasa_password

            self._cmd_queue    = Queue()
            self._result_queue = Queue()
            self._query_queue  = Queue()

            from kasa_manager._worker import worker_main
            proc = Process(
                target=worker_main,
                args=(self._cmd_queue, self._result_queue, self._query_queue,
                      self._kasa_username, self._kasa_password),
                daemon=True,
                name="kasa-manager-worker",
            )
            proc.start()
            self._proc = proc
            log_kasa_diag("info", "KasaManager: worker subprocess started",
                          pid=proc.pid,
                          has_credentials=bool(self._kasa_username and self._kasa_password))

    def stop(self):
        """Terminate the worker subprocess."""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.is_alive():
            try:
                proc.terminate()
                proc.join(timeout=5)
            except Exception as exc:
                log_error(f"KasaManager: error during worker stop: {exc}")

    def restart(self, kasa_username: str = "", kasa_password: str = ""):
        """Stop and restart the worker subprocess.

        Args:
            kasa_username: Updated TP-Link account email (pass empty string to
                keep the previously stored value).
            kasa_password: Updated TP-Link account password (pass empty string
                to keep the previously stored value).
        """
        log_kasa_diag("info", "KasaManager: restarting worker subprocess")
        old_pid = self._proc.pid if self._proc else None
        self.stop()
        time.sleep(0.5)  # brief pause so the OS cleans up the old process
        self.start(kasa_username=kasa_username, kasa_password=kasa_password)
        new_pid = self._proc.pid if self._proc else None
        log_kasa_diag("info", "KasaManager: worker restarted",
                      old_pid=old_pid, new_pid=new_pid)

    def restart_if_dead(self):
        """Check whether the worker has died and restart it if so.

        Returns True if a restart was performed, False if the worker is healthy.
        """
        with self._lock:
            proc = self._proc
        if proc is None:
            return False
        if proc.exitcode is None:
            return False  # still running
        log_kasa_diag("error",
                      "KasaManager: worker subprocess died -- restarting",
                      pid=proc.pid, exitcode=proc.exitcode)
        self.restart()
        return True

    # -- Commands ----------------------------------------------------------

    def send(self, controller_id, role, url, action, port=None):
        """Queue an on/off plug command.  Non-blocking."""
        if self._cmd_queue is None:
            log_error("KasaManager.send called before start()")
            return
        self._cmd_queue.put({
            "controller_id": controller_id,
            "role":          role,
            "url":           url,
            "action":        action,
            "request_id":    str(uuid.uuid4()),
            "port":          port,
        })

    def query_sync(self, url, controller_id=-1, role="", timeout=20.0, port=None):
        """Send a query command and block until the result arrives.

        Returns:
            (is_on, error) -- is_on is True/False on success, None on failure.
                              error is None on success or an error string.
        """
        if self._cmd_queue is None or self._query_queue is None:
            return None, "KasaManager not started"

        request_id = str(uuid.uuid4())
        self._cmd_queue.put({
            "controller_id": controller_id,
            "role":          role,
            "url":           url,
            "action":        "query",
            "request_id":    request_id,
            "port":          port,
        })

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "timeout"
            try:
                result = self._query_queue.get(timeout=min(1.0, remaining))
            except Exception:
                continue
            if result.get("request_id") == request_id:
                state = result.get("state")
                is_on = True if state == "on" else False if state == "off" else None
                return is_on, result.get("error")
            # Different request_id (shouldn't happen, but handle gracefully).
            # Re-queue the result so it isn't lost.
            try:
                self._query_queue.put_nowait(result)
            except Exception:
                pass

    # -- Status ------------------------------------------------------------

    def is_alive(self):
        """Return True if the worker subprocess is running."""
        with self._lock:
            return self._proc is not None and self._proc.is_alive()

    @property
    def worker_pid(self):
        """PID of the worker subprocess, or None if not running."""
        with self._lock:
            return self._proc.pid if self._proc and self._proc.is_alive() else None

    @property
    def result_queue(self):
        """The queue on which the worker places on/off command results.
        Read by the kasa_result_listener thread in app.py.
        """
        return self._result_queue
