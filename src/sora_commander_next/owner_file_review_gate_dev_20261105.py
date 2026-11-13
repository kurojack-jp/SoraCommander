"""DEV_ONLY / NOT_REGISTERED: one local Owner window for one dev_rdc_compat proposal.

Used by dev_rdc_compat.mutate. No-answer replies reuse the C3/C4 typed proof.
APPROVE is a local answer only, not a grant: the caller keeps every write check.
Any doubt gives ERROR, with no fallback to another window.
"""
import hmac
import json
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Thread
from time import monotonic

from .owner_file_review_localized_dev_20261104 import bound_proposal
from .windows_job import WindowsJob
from .windows_session_events import OwnerSessionEventLatch
from .owner_dialog_integrity import DIALOG_INTEGRITY
from .file_review_outcome import _VerifiedFileReviewExpiry
from hashlib import sha256

DEV_ONLY = True
NOT_REGISTERED = True
DIALOG_SCRIPT = Path(__file__).resolve().with_name("owner_file_dialog_localized.py")
from .owner_ui_language import owner_ui_language  # same choice as the command window
MAX_REQUEST = 1048576  # also fixed in the child
MAX_SECONDS = 45.0
# The child draws its window and exits inside this grace. Without it the
# parent gives up first and a true expiry is reported as ERROR (C8, 2026-11-13).
CHILD_GRACE_SECONDS = 3.0


def request_frame(nonce, proposal, seconds, locale=None):
    """The one line sent to the child. Too large or malformed raises, and no window opens."""
    locale = owner_ui_language() if locale is None else locale
    bound = bound_proposal(proposal, review_id=secrets.token_hex(8), seconds=seconds, locale=locale)
    presentation = bound.presentation
    screen = presentation.screen
    frame = json.dumps({
        "nonce": nonce, "seconds": seconds, "locale": locale, "kind": screen.kind,
        "title": screen.title, "heading": screen.heading, "caution": screen.caution,
        "information": screen.information, "body": screen.body, "labels": list(presentation.labels),
    }, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(frame) > MAX_REQUEST:
        raise ValueError("file review too large for safe UI display")
    return frame


def review_once(guard, proposal, *, seconds=MAX_SECONDS, locale=None, review_token=None):
    child = job = latch = None
    try:
        if type(seconds) not in (int, float) or not 0 < seconds <= MAX_SECONDS:
            raise ValueError("invalid file review time")
        nonce = secrets.token_hex(32)
        payload = request_frame(nonce, proposal, seconds, locale)
        # Register notifications before the window. Any session change ends this review.
        latch = OwnerSessionEventLatch(guard._identity.session_id).start()
        guard.verify()
        latch.verify()
        if not DIALOG_SCRIPT.is_file() or not DIALOG_SCRIPT.is_absolute():
            raise PermissionError("trusted Owner dialog missing")
        DIALOG_INTEGRITY.verify()
        job = WindowsJob()
        child = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", str(DIALOG_SCRIPT)],
            cwd=str(DIALOG_SCRIPT.parent), shell=False, creationflags=0x00000004, close_fds=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        job.attach_and_resume(child)
        guard.verify_process(child.pid)
        latch.verify()
        answer = {}

        def collect():
            try:
                answer["output"], _ = child.communicate(input=payload, timeout=seconds + CHILD_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                answer["timeout"] = True
            except Exception:
                answer["error"] = True
        reader = Thread(target=collect, daemon=True)
        reader.start()
        deadline = monotonic() + seconds + CHILD_GRACE_SECONDS
        while reader.is_alive():
            latch.verify()
            guard.verify()
            guard.verify_process(child.pid)
            reader.join(timeout=0.12)
            if monotonic() >= deadline:
                raise PermissionError("Owner confirmation timed out")
        if answer.get("timeout"):
            raise PermissionError("Owner confirmation timed out")
        if answer.get("error") or "output" not in answer:
            raise PermissionError("Owner UI did not answer")
        output = answer["output"]
        if child.returncode != 0 or len(output) > 512:
            raise PermissionError("invalid Owner dialog exit")
        from .owner_file_dialog_localized import _unique
        response = json.loads(output.decode("ascii"), object_pairs_hook=_unique)
        if (type(response) is not dict
                or set(response) != {"nonce", "decision"}
                or type(response["nonce"]) is not str
                or not hmac.compare_digest(response["nonce"], nonce)
                or response["decision"] not in ("APPROVE", "DENY", "EXPIRED", "CLOSED")):
            raise PermissionError("untrusted dialog response")
        guard.verify()
        latch.verify()
        if response["decision"] in ("EXPIRED", "CLOSED"):
            # The integrity-checked child owns timer/event proof; the nonce binds
            # its reply to this one invocation. This gate has no writer.
            digest = sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
            return _VerifiedFileReviewExpiry(review_token if review_token is not None else nonce,
                proposal.get("path", proposal.get("source")),
                proposal.get("old_sha256"), digest, proposal["operation"],
                reason=response["decision"])
        return response["decision"]
    except Exception:
        return "ERROR"
    finally:
        if latch is not None:
            try:
                latch.close()
            except Exception:
                pass
        if job is not None:
            try:
                job.close()
            except Exception:
                pass
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
            except Exception:
                pass
            for pipe in (child.stdin, child.stdout):
                try:
                    if pipe is not None and not pipe.closed:
                        pipe.close()
                except Exception:
                    pass
