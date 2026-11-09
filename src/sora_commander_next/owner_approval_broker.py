"""Explicit, local-only one-shot Owner GUI subprocess broker.

Only anonymous child stdin/stdout pipes; no socket, named pipe, HTTP endpoint,
remote tool or inherited approval tokens. The UI requires a real human click.
Not launched by importing this module or by any registered MCP tool.
"""
import hmac
import json
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Thread
from time import monotonic

from .windows_job import WindowsJob
from .windows_owner_controller import WindowsOwnerController, review_diagnostics
from .windows_session_events import OwnerSessionEventLatch
from .owner_dialog_integrity import DIALOG_INTEGRITY

DIALOG_SCRIPT=Path(__file__).resolve().with_name("owner_command_dialog_localized.py")
# Display only: language.txt, else the Windows display language (en/ja).
from .owner_ui_language import owner_ui_language


class OwnerApprovalBroker:
    def __init__(self, controller):
        if type(controller) is not WindowsOwnerController:
            raise TypeError("explicit Windows Owner controller required")
        controller._verify()
        self._controller=controller

    def review_once(self, pending_id):
        """Block for an *explicit* local UI click, or fail-closed. No auto grant."""
        child=None
        job=None
        latch=None
        dispatched=False
        deadline_expired=False
        try:
            controller=self._controller
            controller._verify()
            matches=[p for p in controller.pending_local()
                     if p.pending_id==pending_id]
            if type(pending_id) is not str or len(matches)!=1:
                raise PermissionError("pending review unavailable")
            pending=matches[0]
            remaining=pending.expires_at-controller.host.approval.clock()
            if remaining<=0:
                raise PermissionError("review expired")
            # Never let UI outlive the pending record, nor indefinitely wait.
            budget=min(45.0,remaining)
            nonce=secrets.token_hex(32)
            req=pending.request
            payload=json.dumps({
                "nonce":nonce, "executable":req.executable,
                "argv":list(req.argv), "cwd":req.cwd,
                "seconds":budget,"pending_id":pending_id,
                "timeout":req.timeout,"output_limit":req.output_limit,
                "locale":owner_ui_language(),
            },ensure_ascii=True,separators=(",",":")).encode("ascii")+b"\n"
            if len(payload)>131072:
                raise PermissionError("review too large for safe UI display")

            # Register notifications *before* launching a dialog. On any
            # Owner session-change message, deny this one review forever.
            latch=OwnerSessionEventLatch(controller._guard._identity.session_id).start()
            controller._verify()
            latch.verify()

            # The path and executable are set solely by this trusted local
            # broker code, never by MCP, pending requests or plugin input.
            path=DIALOG_SCRIPT
            if not path.is_file() or not path.is_absolute():
                raise PermissionError("trusted Owner dialog missing")
            DIALOG_INTEGRITY.verify()
            job=WindowsJob()
            child=subprocess.Popen(
                [sys.executable,"-I","-X","utf8",str(path)],
                cwd=str(path.parent),shell=False,
                creationflags=0x00000004,close_fds=True,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
            job.attach_and_resume(child)
            # Inspect the child token *before* trusting any decision.
            controller._guard.verify_process(child.pid)
            latch.verify()
            # Do not block waiting for a UI click while the workstation
            # locks or the Owner logon/session changes. Observe the OS
            # independently throughout the dialog lifetime.
            answer={}
            def collect():
                try:
                    answer["output"],_=child.communicate(input=payload,timeout=budget+.5)
                except subprocess.TimeoutExpired:
                    answer["timeout"]=True
                except Exception:
                    answer["error"]=True
            reader=Thread(target=collect,daemon=True)
            reader.start()
            deadline=monotonic()+budget+.5
            while reader.is_alive():
                latch.verify()
                controller._guard.verify()
                controller._guard.verify_process(child.pid)
                reader.join(timeout=0.12)
                if monotonic()>=deadline:
                    deadline_expired=True
                    raise PermissionError("Owner confirmation timed out")
            if answer.get("timeout"):
                deadline_expired=True
                raise PermissionError("Owner confirmation timed out")
            if answer.get("error") or "output" not in answer:
                raise PermissionError("Owner UI did not answer")
            output=answer["output"]
            if child.returncode!=0 or len(output)>512:
                raise PermissionError("invalid Owner dialog exit")
            response=json.loads(output.decode("ascii"))
            if (type(response) is not dict
                    or set(response)!={"nonce","decision"}
                    or type(response["nonce"]) is not str
                    or not hmac.compare_digest(response["nonce"],nonce)
                    or response["decision"] not in ("APPROVE","DENY","EXPIRED")):
                raise PermissionError("untrusted dialog response")
            controller._guard.verify()
            latch.verify()
            if response["decision"]=="EXPIRED":
                # No launch has been dispatched. Retire the old review
                # atomically before reporting a verified timeout.
                expired=controller.reject_local(pending_id)
                if expired.get("status")!="REJECTED":
                    raise PermissionError("expired review could not be retired")
                return {"status":"TIMED_OUT", **review_diagnostics(
                    'FIRST_GUI', 'EXPIRED', 'EXPIRED', 'NOT_STARTED')}
            if response["decision"]=="DENY":
                # Reject must consume the saved review as well as avoid
                # launching any job. Otherwise the same pending_id could
                # later be approved through another local controller call.
                latch.verify()
                rejected=controller.reject_local(pending_id)
                if rejected.get("status")!="REJECTED":
                    raise PermissionError("Owner rejection could not be finalized")
                return {"status":"DENIED", **review_diagnostics(
                    'FIRST_GUI', 'DENIED', 'REJECTED', 'NOT_STARTED')}
            dispatched=True
            approved=controller.approve_local(pending_id, session_latch=latch)
            # A final Owner-session or policy failure reported by the
            # controller is also an abnormal review exit: revoke any
            # remaining pending record via the DENY-only failure path.
            if approved.get("status")=="ERROR":
                try:
                    with controller.host.policy.lock:
                        controller.host.approval.reject_local(pending_id)
                except Exception:
                    pass  # Preserve the controller's already-fixed outcome.
            return approved
        except Exception:
            # A lock, disconnect, timeout or failed UI must invalidate the
            # pending REVIEW itself, not merely kill the visible dialog.
            # This is a DENY-only cleanup under the host Policy lock; it
            # does not grant any authority or bypass checks to approve.
            retired=False
            try:
                if type(pending_id) is str:
                    with self._controller.host.policy.lock:
                        try:
                            self._controller.host.approval.reject_local(pending_id)
                            retired=True
                        except KeyError:
                            pass  # Already expired, rejected or consumed.
            except Exception:
                # We still return ERROR. LocalApproval normally cannot fail
                # except when the record is already gone.
                pass
            if dispatched:
                return {'status': 'OUTCOME_UNKNOWN', 'error': 'OwnerConfirmationUnavailable',
                        **review_diagnostics('LAUNCH', 'LAUNCH_UNKNOWN', 'UNKNOWN', 'UNKNOWN')}
            if deadline_expired and retired:
                # Verified parent-side expiry BEFORE any command launch.
                # UNKNOWN or failed revocation must never be mislabeled.
                return {"status":"TIMED_OUT", **review_diagnostics(
                    'FIRST_GUI', 'EXPIRED', 'EXPIRED', 'NOT_STARTED')}
            return {"status":"ERROR","error":"OwnerConfirmationUnavailable",
                    **review_diagnostics('FIRST_GUI', 'CONFIRMATION_UNAVAILABLE',
                                         'NOT_CONSUMED', 'NOT_STARTED')}
        finally:
            if latch is not None:
                try:
                    latch.close()
                except PermissionError:
                    pass
            # Closing the entire local dialog job reaps the Python launcher
            # and descendants even if the dialog hung or crashed.
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
                except (OSError,subprocess.TimeoutExpired):
                    pass
                for pipe in (child.stdin,child.stdout):
                    if pipe is not None and not pipe.closed:
                        pipe.close()
