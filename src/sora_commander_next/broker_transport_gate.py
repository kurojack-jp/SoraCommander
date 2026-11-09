"""Transport admission boundary. No listener or caller identity inference.

The future Windows named-pipe adapter must supply a verifier that checks the
IMPERSONATED client token, including SID, logon LUID and session. This gate
never accepts caller-supplied identity claims or a boolean authentication flag.
"""
from .broker_protocol import MAX_FRAME, encode_response


class TransportIdentityDenied(PermissionError):
    pass


class BrokerTransportGate:
    def __init__(self, *, verify_client, dispatcher, max_frame=MAX_FRAME):
        if not callable(verify_client) or not callable(getattr(dispatcher, "handle", None)):
            raise TypeError("verified client callback and dispatcher required")
        self._verify_client = verify_client
        self._dispatcher = dispatcher
        self._max_frame = max_frame

    def handle_verified_connection(self, raw):
        """Call only from an OS-authenticated connection context.

        verify_client must raise on missing/invalid impersonation token, stale
        session, or lock. Its return value must be the exact True singleton.
        """
        denied = {"version": 1, "status": "DENIED", "request_id": None}
        if type(raw) is not bytes or not 0 < len(raw) <= self._max_frame:
            return encode_response(denied)
        try:
            if self._verify_client() is not True:
                raise TransportIdentityDenied("unverified IPC client")
        except Exception:
            return encode_response(denied)
        try:
            return self._dispatcher.handle(raw)
        except Exception:
            return encode_response({"version": 1, "status": "ERROR", "request_id": None})
