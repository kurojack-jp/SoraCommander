"""Local Windows Owner v2 review CANDIDATE. Never dispatches a command.

This module is opt-in and not reachable from MCP/HTTP. It displays an exact
command via a private GUI child, checks the real local Windows Owner logon and
WTS session event latch, and returns only inert REVIEW DATA. The development
workspace is not a protected trust boundary. Only a future accepted protected
broker may issue a real grant or start a job from Owner review.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Thread
from time import monotonic

from .approval_v2_contract import operation_for_command
from .commands import Command
from .owner_approval_dialog_v2 import parse_review, verify_review_reply_data_only

_DIALOG = Path(__file__).resolve().with_name("owner_approval_dialog_v2.py")


@dataclass(frozen=True, slots=True)
class LocalReviewData:
    """Inert claimed Owner choice, NOT an execution grant or identity credential."""
    status: str
    choice: str | None = None
    owner_sid: str | None = None
    spec_sha256: str | None = None
    review_sha256: str | None = None
    review_id: str | None = None
    request_id: str | None = None


def make_review_frame(request: Command, *, owner_sid: str, machine_id: str,
                      allow_trusted=False, seconds=45.0, nonce=None, review_id=None):
    """Format a fully displayed, bounded one-time challenge (DATA ONLY)."""
    if type(request) is not Command:
        raise TypeError("validated command required")
    if type(allow_trusted) is not bool or type(seconds) not in (float, int) or not 0 < seconds <= 60:
        raise ValueError("explicit bounded review choice required")
    operation = operation_for_command(
        request, owner_id=owner_sid, machine_id=machine_id)
    frame = {
        "nonce": nonce if nonce is not None else secrets.token_hex(32),
        "review_id": review_id if review_id is not None else secrets.token_hex(16),
        "spec_sha256": operation.digest, "owner_label": owner_sid,
        "executable": operation.executable, "argv": list(operation.argv),
        "cwd": operation.cwd, "seconds": seconds,
        "timeout_ms": round(request.timeout * 1000),
        "output_limit": request.output_limit,
        "allow_trusted": allow_trusted, "unattended": False,
    }
    raw = json.dumps(frame, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("ascii") + b"\n"
    # Parse the serialized bytes using the same strict child-side contract.
    parse_review(raw)
    return raw, operation


class LocalOwnerV2Pilot:
    """Future protected Broker integration input, not a live Owner authority."""

    def __init__(self):
        from .windows_owner import OwnerSessionGuard
        # Construct a genuine local Owner guard; never accept remote identities.
        self._guard = OwnerSessionGuard()
        self._guard.verify()

    def review_once_local(self, request, *, allow_trusted=False):
        """Explicit local GUI review. No persistent grant or process dispatch."""
        child = job = latch = None
        try:
            from .windows_job import WindowsJob
            from .windows_session_events import OwnerSessionEventLatch
            import socket

            self._guard.verify()
            # Names the local PC for scope comparison; not cryptographic attestation.
            machine_id = socket.gethostname()
            raw, operation = make_review_frame(
                request, owner_sid=self._guard.sid, machine_id=machine_id,
                allow_trusted=allow_trusted, seconds=45.0)
            frame = parse_review(raw)
            if not _DIALOG.is_absolute() or not _DIALOG.is_file():
                raise PermissionError("local Owner dialog candidate unavailable")
            latch = OwnerSessionEventLatch(self._guard._identity.session_id).start()
            self._guard.verify()
            latch.verify()
            job = WindowsJob()
            child = subprocess.Popen(
                [sys.executable, "-I", "-X", "utf8", str(_DIALOG)],
                cwd=str(_DIALOG.parent), shell=False, creationflags=0x00000004,
                close_fds=True, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            job.attach_and_resume(child)
            self._guard.verify_process(child.pid)
            latch.verify()

            result = {}
            def collect():
                try:
                    result["reply"], _ = child.communicate(input=raw, timeout=45.5)
                except Exception:
                    result["error"] = True
            reader = Thread(target=collect, daemon=True, name="SoraV2OwnerReply")
            reader.start()
            deadline = monotonic() + 45.5
            while reader.is_alive():
                self._guard.verify()
                latch.verify()
                # A fast click can make the verified child exit before the
                # pipe reader finishes. Recheck its token only while alive;
                # exit status, exact nonce and the WTS latch are checked
                # separately after communicate() completes.
                if child.poll() is None:
                    try:
                        self._guard.verify_process(child.pid)
                    except PermissionError:
                        # Distinguish ordinary child exit during an instant
                        # response from a genuine live token mismatch.
                        if child.poll() is None:
                            raise
                reader.join(timeout=0.12)
                if monotonic() >= deadline:
                    raise PermissionError("Owner review timed out")
            self._guard.verify()
            latch.verify()
            if (result.get("error") or type(result.get("reply")) is not bytes
                    or child.returncode != 0):
                raise PermissionError("Owner dialog did not return a valid response")
            choice = verify_review_reply_data_only(frame, result["reply"])
            latch.verify()
            self._guard.verify()
            # A verified rejection has a review identity too. Persisting its
            # digest as an inert denial receipt prevents the same challenge
            # from being reused as an alleged later approval.
            return LocalReviewData(
                status=("DENIED_DATA_ONLY" if choice == "DENY"
                        else "LOCAL_CHOICE_DATA_ONLY"),
                choice=None if choice == "DENY" else choice,
                owner_sid=self._guard.sid, spec_sha256=operation.digest,
                review_sha256=sha256(raw).hexdigest(),
                review_id=frame["review_id"], request_id=request.request_id)
        except Exception:
            return LocalReviewData(status="ERROR")
        finally:
            if latch is not None:
                try:
                    latch.close()
                except PermissionError:
                    pass
            if job is not None:
                try:
                    job.close()
                except OSError:
                    pass
            if child is not None:
                try:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                for pipe in (child.stdin, child.stdout):
                    if pipe is not None and not pipe.closed:
                        pipe.close()
