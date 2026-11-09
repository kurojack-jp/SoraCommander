"""DEV ONLY: actual BrokerRuntime + JobManager in a separate Owner-bound process.

One fixed harmless synthetic self-approved test command. No remote input,
Owner GUI, grant, named pipe, listening endpoint, protected storage, or user
selected executable. This process is NOT a production Broker or consent flow.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time

from .broker_contract import BoundRequest, BrokerRequestDenied
from .broker_runtime import BrokerRuntime
from .jobs import JobManager
from .offline_broker_singleton import DevLeaseError, DevWindowsMutexLease
from .offline_broker_result_reader import OfflineLocalResultReader
from .offline_broker_result_wire import decode_read_result
from .windows_owner import OwnerSessionGuard

# Fixed fixture, not a command string supplied over a transport.
_FIXED_CODE = (
    "import sys,time;"
    "print('owner-broker-child-ok');"
    "sys.stderr.write('owner-broker-child-err\\n');"
    "time.sleep(1.2)"
)


def _marker(value):
    """A lost observer must not turn a finished DEV job into a false failure."""
    try:
        print(value, flush=True)
    except OSError:
        # Windows can raise EINVAL (22), not only BrokenPipeError, when the
        # observing pipe has closed. Output transport is not job outcome.
        # Redirect the CRT descriptor before Python's final stdout flush.
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)


def main(argv=None):
    parser = argparse.ArgumentParser(description="DEV-only one-shot BrokerRuntime job")
    parser.add_argument("--name", required=True, help="strict DEV mutex namespace only")
    args = parser.parse_args(argv)
    try:
        guard = OwnerSessionGuard()
        guard.verify()
        with DevWindowsMutexLease(args.name):
            guard.verify()
            # No archival data or protected real Owner directories are used.
            with tempfile.TemporaryDirectory(prefix="sora-dev-broker-runtime-") as cwd:
                manager = JobManager(max_concurrent=1, capacity=2, retention=30.0)
                broker = BrokerRuntime(
                    expected_executable=sys.executable, expected_cwd=cwd,
                    owner_binding=guard.sid, owner_guard=guard,
                    clock=time.monotonic, capacity=1, manager=manager)
                try:
                    rid = secrets.token_hex(16)
                    request = BoundRequest(
                        rid, sys.executable, ("-c", _FIXED_CODE), cwd,
                        hashlib.sha256(_FIXED_CODE.encode("utf-8")).hexdigest(),
                        guard.sid, time.monotonic() + 10, 4.0, 4096)
                    # SYNTHETIC fingerprint derived locally for this fixed test;
                    # not an Owner-approved grant or equivalent to GUI consent.
                    started = broker.start_approved(
                        request, approved_fingerprint=request.fingerprint(),
                        current_task_sha256=request.task_sha256)
                    if started.get("status") != "RUNNING":
                        return 5
                    _marker("DEV_BROKER_JOB_STARTED")
                    # Owner and JobManager live INSIDE this separate process.
                    session = broker.sessions[rid]
                    complete = manager.wait_for(session, 5.0)
                    guard.verify()
                    if complete.get("status") != "COMPLETED" or complete.get("exit_code") != 0:
                        return 6
                    result = broker.query_result(rid)
                    state = broker.query_status(rid)
                    if (result.get("session_id") != session
                            or result.get("exit_code") != 0
                            or "owner-broker-child-ok" not in result.get("stdout", "")
                            or "owner-broker-child-err" not in result.get("stderr", "")
                            or state.get("status") != "COMPLETED"
                            or len(manager.list_sessions()["sessions"]) != 1):
                        return 7
                    # No IPC: this read-only v2 wire is built *inside* the
                    # owning Broker process, never sent to an unauthenticated
                    # caller. Verify exact request/session and bounded output.
                    read_request = json.dumps({
                        "version": 2, "op": "read_result",
                        "request_id": rid, "session_id": session, "offset": 0,
                    }, separators=(",", ":")).encode("ascii")
                    frame = OfflineLocalResultReader(broker).read_local(read_request)
                    decoded = decode_read_result(frame)
                    if (decoded["status"] != "COMPLETED"
                            or decoded["exit_code"] != 0
                            or decoded["request_id"] != rid
                            or decoded["session_id"] != session
                            or b"owner-broker-child-ok" not in
                            base64.b64decode(decoded["stdout_base64"])
                            or b"owner-broker-child-err" not in
                            base64.b64decode(decoded["stderr_base64"])):
                        return 9
                    try:
                        broker.start_approved(
                            request, approved_fingerprint=request.fingerprint(),
                            current_task_sha256=request.task_sha256)
                    except BrokerRequestDenied:
                        pass
                    else:
                        return 8
                    guard.verify()
                    _marker("DEV_BROKER_RESULT_VERIFIED")
                    return 0
                finally:
                    broker.close()
    except DevLeaseError:
        print("DEV_BROKER_LEASE_DENIED", flush=True)
        return 3
    except (OSError, PermissionError):
        print("DEV_BROKER_OWNER_UNAVAILABLE", flush=True)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
