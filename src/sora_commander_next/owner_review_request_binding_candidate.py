"""DEV-only exact command frame -> unified Owner presentation binding.

Uses the existing command and v2 frame validators. The same freshly parsed
frame supplies both the canonical review body and its summary/caution facts.
No Owner decision, UI, authority, file mutation or command launch lives here.
Do not use the returned digest as authentication or approval.
"""
from dataclasses import dataclass
from hashlib import sha256
import json

from .owner_approval_dialog import parse_request
from .owner_approval_dialog_v2 import parse_review, review_text
from .owner_review_presentation_candidate import build_presentation_candidate


@dataclass(frozen=True, slots=True)
class BoundRequestPresentation:
    kind: str
    frame_sha256: str
    review_identity: str
    presentation: object


def _active_command_body(request):
    """Preserve the current active command child wording exactly.

    This is a candidate mirror of owner_approval_dialog.main's data field;
    future activation requires an explicit parity review against that child.
    """
    return (f"Executable: {request['executable']!r}\n"
            f"argv (literal): {request['argv']!r}\n"
            f"Working directory: {request['cwd']!r}\n"
            f"Timeout: {request['timeout']} s\n"
            f"Output limit: {request['output_limit']} bytes\n"
            f"Review ID: {request['pending_id']!r}\n"
            f"Expires in at most {request['seconds']:.0f} seconds.\n"
            "Unknown command effects: inspect ALL fields; approval can have side effects.\n"
            "Not a remote MCP approval endpoint.\n")


def _no_duplicate_json_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Owner command frame field")
        result[key] = value
    return result


def bind_command_frame(frame, *, locale):
    """Data-only binding from exactly one active command transport frame."""
    # A duplicate-key frame can be interpreted differently by other consumers.
    # Reject it here; the active command parser/Host remains unchanged.
    request = parse_request(frame)
    json.loads(frame.decode("utf-8"), object_pairs_hook=_no_duplicate_json_fields)
    # The parser makes a new dict/list, owned only by this call.
    body = _active_command_body(request)
    facts = {"executable": request["executable"],
             "cwd": request["cwd"],
             "argv": list(request["argv"])}
    view = build_presentation_candidate(
        locale=locale, kind="command", original_body=body, facts=facts)
    return BoundRequestPresentation(
        kind="command", frame_sha256=sha256(frame).hexdigest(),
        review_identity=request["pending_id"], presentation=view)


def bind_v2_frame(frame, *, locale):
    """Data-only binding from exactly one inactive v2 candidate frame."""
    request = parse_review(frame)
    # v2 review_text is the SAME canonical formatter the candidate child uses.
    body = review_text(request)
    facts = {"executable": request["executable"], "cwd": request["cwd"],
             "argv": list(request["argv"]),
             "allow_trusted": request["allow_trusted"]}
    view = build_presentation_candidate(
        locale=locale, kind="v2_candidate", original_body=body, facts=facts)
    return BoundRequestPresentation(
        kind="v2_candidate", frame_sha256=sha256(frame).hexdigest(),
        review_identity=request["review_id"], presentation=view)
