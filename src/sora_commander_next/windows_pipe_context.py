"""Windows named-pipe impersonation scope; no listener or startup.

Only a trusted listener may supply an already-connected pipe HANDLE after
reading the request from that connection. RevertToSelf is mandatory.
"""
import ctypes
from ctypes import wintypes
import os

if os.name != "nt":
    raise ImportError("Windows named-pipe context requires Windows")

from .windows_pipe_client import verify_impersonated_client
from .windows_owner import OwnerSessionGuard
from .broker_transport_gate import BrokerTransportGate

_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_impersonate = _adv.ImpersonateNamedPipeClient
_impersonate.argtypes = [wintypes.HANDLE]
_impersonate.restype = wintypes.BOOL
_revert = _adv.RevertToSelf
_revert.argtypes = []
_revert.restype = wintypes.BOOL


class PipeImpersonationError(PermissionError):
    pass


class WindowsPipeRequestContext:
    def __init__(self, *, owner_guard, dispatcher, max_frame=None):
        if not isinstance(owner_guard, OwnerSessionGuard):
            raise TypeError("trusted Windows Owner guard required")
        if not callable(getattr(dispatcher, "handle", None)):
            raise TypeError("dispatcher required")
        self._owner_guard = owner_guard
        self._dispatcher = dispatcher
        self._gate_limits = {} if max_frame is None else {"max_frame": max_frame}

    def handle(self, pipe_handle, raw):
        """Impersonate to inspect client, revert, recheck Owner, dispatch.

        The caller owns and closes pipe_handle. Never accept it from a
        message. This function does not establish that the handle is a pipe;
        the future listener must guarantee that.
        """
        self._owner_guard.verify()
        if not isinstance(pipe_handle, wintypes.HANDLE) or not pipe_handle.value:
            raise PipeImpersonationError("connected pipe handle required")
        if not _impersonate(pipe_handle):
            raise PipeImpersonationError("pipe impersonation failed")
        verified = False
        try:
            verified = verify_impersonated_client(self._owner_guard._identity) is True
        finally:
            try:
                reverted = _revert()
            except BaseException:
                # If the Windows call itself fails unexpectedly, the current
                # impersonation context cannot be proven safe either.
                os._exit(71)
            if not reverted:
                # After an unsuccessful RevertToSelf this thread might still
                # run under the CLIENT security token. Raising here is NOT
                # sufficient: a parent loop may catch it and continue.
                # Microsoft requires the whole process to shut down.
                # os._exit bypasses Python exception handlers/finalizers.
                os._exit(71)
        self._owner_guard.verify()
        if not verified:
            raise PipeImpersonationError("client identity denied")
        # No persistent authorization flag or remote-supplied identity.
        gate = BrokerTransportGate(verify_client=lambda: True, dispatcher=self._dispatcher,
                                   **self._gate_limits)
        return gate.handle_verified_connection(raw)
