"""Opt-in local Host crash supervisor. Never runs/import-starts a service or replays work.

A caller explicitly runs this in a separate trusted parent process. A restarted
Host is a FRESH process, without recovered sessions or approvals. HTTP readiness,
secret provisioning, tunnel connections and Windows autostart are out of scope.
"""
import os
import re
import subprocess
from threading import Event, Lock
from time import monotonic

if os.name == "nt":
    from .windows_job import WindowsJob


class HostSupervisor:
    def __init__(self, argv, cwd, *, max_restarts=2, poll_seconds=0.05,
                 backoff_seconds=0.1, startup_token=None,
                 health_probe=None, health_interval=0.2,
                 startup_grace=8.0, health_failures=3):
        if (not isinstance(argv, (list, tuple)) or not argv
                or any(type(x) is not str or not x for x in argv)
                or not os.path.isabs(argv[0]) or not os.path.isabs(cwd)
                or not os.path.isdir(cwd)):
            raise ValueError("explicit absolute executable and cwd required")
        if type(max_restarts) is not int or not 0 <= max_restarts <= 3:
            raise ValueError("invalid restart limit")
        if not 0.01 <= poll_seconds <= 1 or not 0.01 <= backoff_seconds <= 2:
            raise ValueError("invalid poll or backoff")
        # Optional local-only protocol: provide a fresh loopback bearer token
        # via an anonymous stdin pipe. Never argv/env/file/snapshot/log.
        if startup_token is not None:
            if (type(startup_token) is not bytes
                    or re.fullmatch(rb"[0-9a-f]{64}", startup_token) is None
                    or len(set(startup_token)) < 12
                    or any(startup_token == startup_token[:n] * (64 // n)
                           for n in (1, 2, 4, 8, 16, 32))):
                raise ValueError("invalid local startup token")
        if health_probe is not None and not callable(health_probe):
            raise ValueError("health probe must be locally callable")
        if (type(health_interval) not in (int, float) or not 0.05 <= health_interval <= 2
                or type(startup_grace) not in (int, float)
                or not 0.5 <= startup_grace <= 30
                or type(health_failures) is not int or not 1 <= health_failures <= 10):
            raise ValueError("invalid health probe budget")
        self._health_probe = health_probe
        self._health_interval = health_interval
        self._startup_grace = startup_grace
        self._health_failures = health_failures
        self._startup_token = startup_token
        self._argv, self._cwd = tuple(argv), cwd
        self._max_restarts = max_restarts
        self._poll, self._backoff = poll_seconds, backoff_seconds
        self._stop = Event()
        self._lock = Lock()
        self._started = False
        self._child = None
        self._host_job = None
        self._state = "NOT_STARTED"
        self._generation = 0
        self._restarts = 0
        self._last_exit = None
        self._last_restart_reason = None

    def snapshot(self):
        with self._lock:
            return {"state": self._state, "generation": self._generation,
                    "restarts": self._restarts,
                    "host_pid": self._child.pid if self._child is not None
                    and self._child.poll() is None else None,
                    "last_host_exit_code": self._last_exit,
                    "last_restart_reason": self._last_restart_reason,
                    "replay": False, "remote_ready": False}

    def stop(self):
        """Local-only shutdown signal; joining run() is the caller's duty."""
        self._stop.set()

    def _reap_host(self, child, tree=None):
        # The supervisor owns its *Host* process tree, not command records.
        # This also covers Windows venv launchers with a separate interpreter.
        try:
            if tree is not None:
                tree.terminate()
            elif child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("unable to confirm host shutdown")

    def run(self):
        """Blocking; run only once and only when locally requested."""
        with self._lock:
            if self._started:
                raise RuntimeError("supervisor already started")
            self._started = True
        try:
            while not self._stop.is_set():
                tree, child = None, None
                try:
                    tree = WindowsJob() if os.name == "nt" else None
                    child = subprocess.Popen(
                        self._argv, cwd=self._cwd, shell=False,
                        stdin=subprocess.PIPE if self._startup_token is not None else subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        close_fds=True,
                        creationflags=0x00000004 if tree is not None else 0,
                    )
                    if tree is not None:
                        tree.attach_and_resume(child)
                    if self._startup_token is not None:
                        try:
                            child.stdin.write(self._startup_token + b"\n")
                            child.stdin.flush()
                            # Keep pipe open while this generation runs. The
                            # server can stop itself if the supervisor exits.
                        except (OSError, ValueError):
                            self._reap_host(child, tree)
                            raise
                except (OSError, ValueError):
                    if tree is not None:
                        tree.close()
                    if child is not None:
                        try:
                            self._reap_host(child)
                        except RuntimeError:
                            with self._lock:
                                self._state = "STOP_ERROR"
                            raise
                        if child.stdin is not None:
                            child.stdin.close()
                    with self._lock:
                        self._state = "START_ERROR"
                    return
                with self._lock:
                    self._child = child
                    self._host_job = tree
                    self._generation += 1
                    self._state = "STARTING" if self._health_probe is not None else "RUNNING"
                started_at = monotonic()
                was_healthy, failures, forced_reason = False, 0, None
                while child.poll() is None and not self._stop.is_set():
                    if self._health_probe is not None:
                        # Probe executes in the trusted supervisor only and is
                        # bounded by the caller's local HTTP timeout.
                        try:
                            verdict = self._health_probe()
                        except Exception:
                            verdict = "FAIL"
                        if verdict == "DENY":
                            forced_reason = "AUTH_FAILED"
                            break
                        if verdict == "OK":
                            was_healthy, failures = True, 0
                            with self._lock:
                                self._state = "RUNNING"
                        else:
                            if was_healthy:
                                failures += 1
                            elif monotonic() - started_at >= self._startup_grace:
                                failures = self._health_failures
                            if failures >= self._health_failures:
                                forced_reason = "HEALTH_FAILURE"
                                break
                    self._stop.wait(self._health_interval if self._health_probe else self._poll)
                if self._stop.is_set():
                    self._reap_host(child, tree)
                    if tree is not None:
                        tree.close()
                    if child.stdin is not None:
                        child.stdin.close()
                    with self._lock:
                        self._last_exit = child.poll()
                        self._state = "STOPPED"
                    return
                if forced_reason is not None:
                    # Restart only after the previous Host tree is fully
                    # terminated. No command/approval/session recovery.
                    self._reap_host(child, tree)
                code = child.wait(timeout=1)
                if tree is not None:
                    # Kill any launcher descendants before a fresh Host starts.
                    tree.close()
                if child.stdin is not None:
                    child.stdin.close()
                with self._lock:
                    self._last_exit = code
                    self._last_restart_reason = forced_reason or "HOST_EXIT"
                if forced_reason == "AUTH_FAILED":
                    with self._lock:
                        self._state = "AUTH_FAILED"
                    return
                if code == 0 and forced_reason is None:
                    with self._lock:
                        self._state = "CLEAN_EXIT"
                    return
                with self._lock:
                    if self._restarts >= self._max_restarts:
                        self._state = "CRASH_LIMIT"
                        return
                    self._restarts += 1
                    self._state = "RESTARTING"
                    delay = min(2, self._backoff * (2 ** (self._restarts - 1)))
                self._stop.wait(delay)
            with self._lock:
                self._state = "STOPPED"
        except Exception:
            with self._lock:
                self._state = "STOP_ERROR"
            raise
        finally:
            with self._lock:
                self._child = None
                self._host_job = None
