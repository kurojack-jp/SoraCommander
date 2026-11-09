"""DEV_ONLY B04 Named Pipe HANDLE-role boundary with injectable Win32 ABI.

Do NOT use GetNamedPipeServerProcessId as a client-side identity proof:
Microsoft documents a HANDLE created by CreateNamedPipe (server end) for that
function. GetNamedPipeInfo can distinguish PIPE_SERVER_END vs PIPE_CLIENT_END,
but neither an asserted role nor a server-side PID observation constitutes
an authenticated client channel, protected installation, or publisher trust.

No WinDLL loading, CreateNamedPipe, connecting, closing, transport traffic,
service/Owner/DB/job operation or authorization is implemented.
"""
import ctypes
from ctypes import c_int32, c_uint32, c_void_p, POINTER
from dataclasses import dataclass, field
from time import monotonic_ns

DWORD = c_uint32
BOOL = c_int32
HANDLE = c_void_p
PIPE_SERVER_END = 0x00000001
PIPE_TYPE_MESSAGE = 0x00000004
ALLOWED_PIPE_FLAGS = PIPE_SERVER_END | PIPE_TYPE_MESSAGE
MAX_TIMEOUT_MS = 3000


class PipePeerBoundaryError(RuntimeError):
    def __init__(self, code):
        super().__init__(code + ": NOT_AUTHENTICATED")
        self.code = code


def _nonzero_handle(value):
    maximum = (1 << (ctypes.sizeof(HANDLE) * 8)) - 1
    if type(value) is not int or not 0 < value < maximum:
        raise PipePeerBoundaryError("INVALID_BORROWED_PIPE_HANDLE")
    return value


def _pid(value):
    if type(value) is not int or not 1 <= value <= 0xffffffff:
        raise PipePeerBoundaryError("INVALID_PIPE_PEER_PID")
    return value


def _bind(lib, symbol, args):
    method = getattr(lib, symbol)
    method.argtypes = args
    method.restype = BOOL
    return method


class ExplicitPipeQueryBackend:
    """ctypes signatures over an EXPLICITLY injected fake DLL / callback.

    Construction is inert. Never automatically loads kernel32 and never owns
    the pipe HANDLE. A real DLL must pass a separate independent acceptance.
    """
    def __init__(self, kernel32, get_last_error):
        if not callable(get_last_error):
            raise PipePeerBoundaryError("MISSING_LAST_ERROR")
        self._error = get_last_error
        self._info = _bind(kernel32, "GetNamedPipeInfo",
            [HANDLE, POINTER(DWORD), POINTER(DWORD), POINTER(DWORD), POINTER(DWORD)])
        self._server = _bind(kernel32, "GetNamedPipeServerProcessId",
            [HANDLE, POINTER(DWORD)])
        self._client = _bind(kernel32, "GetNamedPipeClientProcessId",
            [HANDLE, POINTER(DWORD)])

    @staticmethod
    def _checked_return(value):
        if type(value) is not int or not -(1<<31) <= value < (1<<31):
            raise PipePeerBoundaryError("INVALID_NATIVE_BOOL")
        if value == 0:
            raise PipePeerBoundaryError("NATIVE_PIPE_QUERY_FAILED")

    def pipe_flags(self, handle):
        _nonzero_handle(handle)
        result = DWORD()
        self._checked_return(self._info(HANDLE(handle), ctypes.byref(result),
                                        None, None, None))
        if result.value & ~ALLOWED_PIPE_FLAGS:
            raise PipePeerBoundaryError("UNKNOWN_PIPE_HANDLE_FLAGS")
        return result.value

    def server_pid(self, handle):
        _nonzero_handle(handle)
        value = DWORD()
        self._checked_return(self._server(HANDLE(handle), ctypes.byref(value)))
        return _pid(value.value)

    def client_pid(self, handle):
        _nonzero_handle(handle)
        value = DWORD()
        self._checked_return(self._client(HANDLE(handle), ctypes.byref(value)))
        return _pid(value.value)


@dataclass(frozen=True, slots=True)
class ServerEndPipeSnapshot:
    server_pid: int
    client_pid: int
    connection_epoch: int
    handle_role: str
    server_pid_matches_expected_data: bool
    elapsed_ms: int
    authentication: str = field(default="NOT_AUTHENTICATED", init=False)
    admission: str = field(default="UNAVAILABLE", init=False)
    server_end_from_create_named_pipe_proven: bool = field(default=False, init=False)
    pipe_security_descriptor_verified: bool = field(default=False, init=False)
    peer_owner_token_verified: bool = field(default=False, init=False)
    connected_client_server_identity_verified: bool = field(default=False, init=False)
    protected_publisher_verified: bool = field(default=False, init=False)
    missing_evidence: tuple = field(default=(
        "HANDLE_CREATED_BY_CREATE_NAMED_PIPE_NOT_INDEPENDENTLY_PROVEN",
        "GET_NAMED_PIPE_SERVER_PROCESS_ID_ON_CLIENT_HANDLE_NOT_DOCUMENTED",
        "SERVER_SIDE_OBSERVATION_NOT_AUTHENTICATED_TO_REMOTE_BROKER",
        "PIPE_DACL_SQOS_REJECT_REMOTE_AND_TOKEN_NOT_OS_VERIFIED",
        "SCM_PROCESS_LIFETIME_NOT_LINKED_TO_SERVER_CREATED_PIPE_HANDLE",
        "PUBLISHER_WINVERIFYTRUST_AND_INSTALL_ANCHOR_MISSING",
    ), init=False)


def observe_pipe_server_end_data_only(*, borrowed_server_handle,
                                      expected_server_pid, connection_epoch,
                                      _backend=None, _clock_ms=None,
                                      timeout_ms=1000):
    """Two snapshots on the SAME borrowed server-end handle; NO authority.

    Must be called by a future server that already owns a true CreateNamedPipe
    HANDLE. The role flag by itself does not prove handle provenance. This
    function does NOT implement a Broker/client-side server authentication.
    """
    _nonzero_handle(borrowed_server_handle)
    _pid(expected_server_pid)
    if type(connection_epoch) is not int or not 1 <= connection_epoch < 1<<63:
        raise PipePeerBoundaryError("INVALID_EPOCH")
    if _backend is None or type(_backend) is not ExplicitPipeQueryBackend:
        raise PipePeerBoundaryError("PIPE_NATIVE_BACKEND_NOT_ACCEPTED")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise PipePeerBoundaryError("INVALID_TIMEOUT")
    clock = _clock_ms or (lambda: monotonic_ns()//1_000_000)
    prior = -1
    start = None

    def tick():
        nonlocal prior, start
        moment = clock()
        if type(moment) is not int or not 0 <= moment < (1 << 63) or moment < prior:
            raise PipePeerBoundaryError("INVALID_OR_ROLLED_CLOCK")
        if start is None:
            start = moment
        if moment - start >= timeout_ms:
            raise PipePeerBoundaryError("PIPE_OBSERVATION_DEADLINE")
        prior = moment
        return moment

    def snap():
        tick()
        flags = _backend.pipe_flags(borrowed_server_handle)
        if not flags & PIPE_SERVER_END:
            raise PipePeerBoundaryError("CLIENT_PIPE_HANDLE_NOT_SUPPORTED_FOR_PID_PROOF")
        tick()
        server = _backend.server_pid(borrowed_server_handle)
        tick()
        client = _backend.client_pid(borrowed_server_handle)
        tick()
        if server != expected_server_pid or server == client:
            raise PipePeerBoundaryError("SERVER_PID_MISMATCH_OR_SELF_PEER")
        return (server, client, flags)
    before = snap()
    after = snap()
    if before != after:
        raise PipePeerBoundaryError("PIPE_PEER_OR_ROLE_CHANGED")
    done = tick()
    return ServerEndPipeSnapshot(before[0], before[1], connection_epoch,
                                  "SERVER_END_DATA_ONLY", True, done - start)


def reject_client_end_as_server_identity(*, borrowed_client_handle, _backend=None):
    """Explicitly deny use of GetNamedPipeServerProcessId on a client handle.

    Even a fake backend claiming the server PID matches cannot satisfy the
    documented CreateNamedPipe HANDLE origin requirement.
    """
    _nonzero_handle(borrowed_client_handle)
    raise PipePeerBoundaryError("CLIENT_HANDLE_SERVER_PID_API_NOT_ACCEPTED")
