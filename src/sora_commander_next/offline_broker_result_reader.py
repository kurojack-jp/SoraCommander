"""DEV-only Owner-local, read-only result adapters for already-owned jobs.

No listener, client authentication, grants, admission, job launching or remote
dispatch. Both sources must stay inside their own trusted Owner process.
This is NOT safe to expose over MCP or IPC before protected Broker acceptance.
"""
import base64
import binascii

from .broker_runtime import BrokerRuntime
from .offline_broker_result_wire import (
    MAX_RAW_OUTPUT, ResultWireRejected, decode_read_request,
    encode_read_result,
)


def _encode_checked_snapshot(rid, sid, offset, snapshot):
    """Pure bounded snapshot-to-wire formatter. Not an authentication step."""
    if (type(snapshot) is not dict
            or snapshot.get("request_id") != rid
            or snapshot.get("session_id") != sid
            or snapshot.get("offset") != offset):
        raise ResultWireRejected("result lookup not confirmed")
    chunks = snapshot.get("chunks")
    captured = snapshot.get("captured_bytes")
    final_offset = snapshot.get("next_offset")
    if (type(chunks) is not list or type(captured) is not int
            or type(final_offset) is not int
            or not 0 <= offset <= final_offset == captured <= 65536):
        raise ResultWireRejected("inconsistent captured output bounds")
    cursor = offset
    stdout, stderr = bytearray(), bytearray()
    budget = MAX_RAW_OUTPUT
    for chunk in chunks:
        if type(chunk) is not dict or set(chunk) != {
                "offset", "stream", "data_base64"}:
            raise ResultWireRejected("invalid output chunk")
        if (type(chunk["offset"]) is not int or chunk["offset"] != cursor
                or chunk["stream"] not in ("stdout", "stderr")
                or type(chunk["data_base64"]) is not str):
            raise ResultWireRejected("out-of-order output chunk")
        try:
            content = base64.b64decode(chunk["data_base64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ResultWireRejected("malformed output chunk") from exc
        if not content or cursor + len(content) > captured:
            raise ResultWireRejected("invalid output extent")
        to_copy = min(budget, len(content))
        if chunk["stream"] == "stdout":
            stdout.extend(content[:to_copy])
        else:
            stderr.extend(content[:to_copy])
        budget -= to_copy
        cursor += len(content)
    if cursor != captured:
        raise ResultWireRejected("missing output chunk")
    next_offset = offset + len(stdout) + len(stderr)
    if type(snapshot.get("output_incomplete")) is not bool:
        raise ResultWireRejected("missing incomplete flag")
    if type(snapshot.get("overflow")) is not bool:
        raise ResultWireRejected("missing overflow flag")
    return encode_read_result(
        request_id=rid, session_id=sid,
        status=snapshot.get("status"),
        exit_code=snapshot.get("exit_code"),
        stdout=bytes(stdout), stderr=bytes(stderr),
        offset=offset, next_offset=next_offset,
        captured_bytes=captured,
        output_incomplete=snapshot["output_incomplete"],
        archived_truncated=snapshot["overflow"],
    )


class OfflineLocalResultReader:
    """Original BrokerRuntime in-process Owner-guarded v2 result reader."""

    def __init__(self, runtime):
        if type(runtime) is not BrokerRuntime:
            raise TypeError("trusted BrokerRuntime instance required")
        self._runtime = runtime

    def read_local(self, raw):
        """Return one bounded page for a correlated Broker-owned job."""
        request = decode_read_request(raw)
        rid, sid, offset = (request["request_id"], request["session_id"],
                            request["offset"])
        self._runtime.guard.verify()
        with self._runtime._lock:
            known_sid = self._runtime.sessions.get(rid)
        if known_sid is None or sid != known_sid:
            raise ResultWireRejected("unknown or mismatched owned session")
        snapshot = self._runtime.query_result(rid, offset=offset)
        self._runtime.guard.verify()
        output = _encode_checked_snapshot(rid, sid, offset, snapshot)
        self._runtime.guard.verify()
        return output


class OfflineLocalOwnerResultReader:
    """Read-only pages for a verified, private DEV Owner GUI job.

    The local Owner authority already established its ONE verified session
    through the real OwnerApprovalBroker GUI. The returned v2 data are NOT
    remotely authenticated and never constitute execution permission.
    """

    def __init__(self, owner):
        from .offline_owner_authority import DevOwnerAuthority
        if type(owner) is not DevOwnerAuthority:
            raise TypeError("DEV Owner process authority required")
        self._owner = owner

    def read_local(self, raw):
        request = decode_read_request(raw)
        rid, sid, offset = (request["request_id"], request["session_id"],
                            request["offset"])
        owner = self._owner
        try:
            snapshot = owner.read_verified_local(rid, sid, offset)
        except LookupError as exc:
            # The only positive correlation comes from the local Owner
            # authority, not from the supplied identifiers.
            raise ResultWireRejected("Owner job not available") from exc
        output = _encode_checked_snapshot(rid, sid, offset, snapshot)
        owner.verify_local()
        return output
