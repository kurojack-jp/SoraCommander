"""B04 Named Pipe role/peer ABI test using a fake kernel32 only.

No CreateNamedPipe, CreateFile, native WinDLL, Token/SCM call, IPC, file-write
fixture, protected ledger operation or subprocess spawn in these tests.
"""
import ctypes
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch
import unittest

from sora_commander_next.service_pipe_handle_role_candidate import (
    DWORD, BOOL, HANDLE, PIPE_SERVER_END, PIPE_TYPE_MESSAGE,
    PipePeerBoundaryError, ExplicitPipeQueryBackend,
    observe_pipe_server_end_data_only, reject_client_end_as_server_identity,
)


def set_dword(pointer, value):
    ctypes.cast(pointer, ctypes.POINTER(DWORD))[0] = value


class FakeFunc:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None
    def __call__(self, *args):
        return self.callback(*args)


class FakeKernel:
    def __init__(self):
        self.events=[]
        self.flags=PIPE_SERVER_END
        self.server_pid=42
        self.client_pid=103
        self.flip_server=False
        self.flip_client=False
        self.flip_flags=False
        self.fail_function=None
        self.malformed_boolean="NO_OVERRIDE"
        self.query_count={}
        self.GetNamedPipeInfo=FakeFunc(self.info)
        self.GetNamedPipeServerProcessId=FakeFunc(self.server)
        self.GetNamedPipeClientProcessId=FakeFunc(self.client)
    def _track(self,name,handle):
        self.events.append((name,handle.value))
        self.query_count[name]=self.query_count.get(name,0)+1
        if name==self.fail_function:
            return True
        return False
    def info(self,handle,flags,outbuf,inbuf,instances):
        failed=self._track("info",handle)
        if failed:return 0
        self_pos=self.query_count["info"]
        val=self.flags ^ (PIPE_SERVER_END if self.flip_flags and self_pos == 2 else 0)
        set_dword(flags,val)
        return 1 if self.malformed_boolean == "NO_OVERRIDE" else self.malformed_boolean
    def server(self,handle,pid):
        failed=self._track("server",handle)
        if failed:return 0
        value=self.server_pid+(1 if self.flip_server and self.query_count["server"]==2 else 0)
        set_dword(pid,value)
        return 1 if self.malformed_boolean == "NO_OVERRIDE" else self.malformed_boolean
    def client(self,handle,pid):
        failed=self._track("client",handle)
        if failed:return 0
        value=self.client_pid+(1 if self.flip_client and self.query_count["client"]==2 else 0)
        set_dword(pid,value)
        return 1 if self.malformed_boolean == "NO_OVERRIDE" else self.malformed_boolean


class PipeRoleCandidateTests(unittest.TestCase):
    def setUp(self):
        for name in ("ctypes.WinDLL","subprocess.Popen","os.system","sqlite3.connect"):
            g=patch(name,side_effect=AssertionError("REAL OS OPERATION FORBIDDEN"),create=True)
            g.start()
            self.addCleanup(g.stop)
        self.k=FakeKernel()
        self.backend=ExplicitPipeQueryBackend(self.k,lambda:5)
        self.time=100
    def clock(self):
        self.time+=1
        return self.time
    def observe(self, **kwargs):
        return observe_pipe_server_end_data_only(
            borrowed_server_handle=200, expected_server_pid=42,
            connection_epoch=10, _backend=self.backend,
            _clock_ms=kwargs.pop("_clock_ms",self.clock), **kwargs)

    def test_correct_kernel32_abi_and_one_borrowed_handle(self):
        self.assertEqual(self.k.GetNamedPipeInfo.argtypes,
            [HANDLE,ctypes.POINTER(DWORD),ctypes.POINTER(DWORD),
             ctypes.POINTER(DWORD),ctypes.POINTER(DWORD)])
        self.assertEqual(self.k.GetNamedPipeServerProcessId.argtypes,
                         [HANDLE,ctypes.POINTER(DWORD)])
        self.assertEqual(self.k.GetNamedPipeClientProcessId.argtypes,
                         [HANDLE,ctypes.POINTER(DWORD)])
        self.assertIs(self.k.GetNamedPipeInfo.restype,BOOL)
        actual=self.observe()
        self.assertEqual(actual.server_pid,42)
        self.assertEqual(actual.client_pid,103)
        self.assertEqual(self.k.events,[("info",200),("server",200),("client",200)]*2)

    def test_data_always_not_authenticated(self):
        obs=self.observe()
        self.assertEqual(obs.authentication,"NOT_AUTHENTICATED")
        self.assertEqual(obs.admission,"UNAVAILABLE")
        self.assertTrue(obs.server_pid_matches_expected_data)
        self.assertEqual(obs.handle_role,"SERVER_END_DATA_ONLY")
        for name in ("server_end_from_create_named_pipe_proven",
                     "pipe_security_descriptor_verified","peer_owner_token_verified",
                     "connected_client_server_identity_verified",
                     "protected_publisher_verified"):
            self.assertFalse(getattr(obs,name))

    def test_client_end_refuses_without_invoking_peer_pid_api(self):
        self.k.flags=0
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            self.observe()
        self.assertEqual(ctx.exception.code,"CLIENT_PIPE_HANDLE_NOT_SUPPORTED_FOR_PID_PROOF")
        self.assertEqual(self.k.events,[("info",200)])

    def test_borrowed_client_handle_api_is_unconditionally_denied(self):
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            reject_client_end_as_server_identity(borrowed_client_handle=200,
                                                _backend=self.backend)
        self.assertEqual(ctx.exception.code,"CLIENT_HANDLE_SERVER_PID_API_NOT_ACCEPTED")
        self.assertEqual(self.k.events,[])

    def test_claimed_server_role_does_not_upgrade_client(self):
        for hint in (True, "SERVER_CREATED", 42):
            with self.subTest(hint=hint),self.assertRaises(TypeError):
                reject_client_end_as_server_identity(
                    borrowed_client_handle=200, _backend=self.backend, trusted=hint)
        self.assertEqual(self.k.events,[])

    def test_role_flip_between_snapshots_denied(self):
        self.k.flip_flags=True
        with self.assertRaises(PipePeerBoundaryError):
            self.observe()
        self.assertEqual(self.k.query_count["server"],1)

    def test_server_pid_change_between_snapshots_denied(self):
        self.k.flip_server=True
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            self.observe()
        self.assertEqual(ctx.exception.code,"SERVER_PID_MISMATCH_OR_SELF_PEER")

    def test_client_pid_change_between_snapshots_denied(self):
        self.k.flip_client=True
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            self.observe()
        self.assertEqual(ctx.exception.code,"PIPE_PEER_OR_ROLE_CHANGED")

    def test_expected_service_pid_mismatch_denied(self):
        self.k.server_pid=901
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            self.observe()
        self.assertEqual(ctx.exception.code,"SERVER_PID_MISMATCH_OR_SELF_PEER")

    def test_same_pid_as_client_denied(self):
        self.k.client_pid=42
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            self.observe()
        self.assertEqual(ctx.exception.code,"SERVER_PID_MISMATCH_OR_SELF_PEER")

    def test_message_mode_server_end_allowed_as_data(self):
        self.k.flags=PIPE_SERVER_END|PIPE_TYPE_MESSAGE
        self.assertEqual(self.observe().server_pid,42)

    def test_unknown_handle_flags_fail_closed(self):
        self.k.flags=PIPE_SERVER_END|0x80000000
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            self.observe()
        self.assertEqual(ctx.exception.code,"UNKNOWN_PIPE_HANDLE_FLAGS")

    def test_each_native_call_fail_closed(self):
        for name in ("info","server","client"):
            with self.subTest(name=name):
                k=FakeKernel();k.fail_function=name
                be=ExplicitPipeQueryBackend(k,lambda:5)
                with self.assertRaises(PipePeerBoundaryError):
                    observe_pipe_server_end_data_only(
                        borrowed_server_handle=200,expected_server_pid=42,
                        connection_epoch=10,_backend=be,_clock_ms=self.clock)
                self.assertEqual(k.query_count[name],1)

    def test_malformed_native_bool_is_denied(self):
        for bad in (True,False,1.0,None,-(1<<32), 1<<32):
            with self.subTest(value=bad):
                k=FakeKernel();k.malformed_boolean=bad
                be=ExplicitPipeQueryBackend(k,lambda:5)
                with self.assertRaises(PipePeerBoundaryError):
                    observe_pipe_server_end_data_only(
                        borrowed_server_handle=200,expected_server_pid=42,
                        connection_epoch=10,_backend=be,_clock_ms=self.clock)

    def test_invalid_borrowed_handles_before_kernel_calls(self):
        for val in (None,True,False,0,-1,"2",1<<64):
            with self.subTest(val=val),self.assertRaises(PipePeerBoundaryError):
                observe_pipe_server_end_data_only(
                    borrowed_server_handle=val,expected_server_pid=42,
                    connection_epoch=10,_backend=self.backend)
        self.assertEqual(self.k.events,[])

    def test_missing_backend_never_calls_native_dll(self):
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            observe_pipe_server_end_data_only(
                borrowed_server_handle=200,expected_server_pid=42,connection_epoch=10)
        self.assertEqual(ctx.exception.code,"PIPE_NATIVE_BACKEND_NOT_ACCEPTED")
        self.assertEqual(self.k.events,[])

    def test_cannot_use_claimed_auth_flag(self):
        with self.assertRaises(TypeError):
            self.observe(trusted=True)

    def test_invalid_expected_pid(self):
        for val in (0,False,1<<32,-1,"42"):
            with self.subTest(val=val),self.assertRaises(PipePeerBoundaryError):
                observe_pipe_server_end_data_only(
                    borrowed_server_handle=200,expected_server_pid=val,
                    connection_epoch=10,_backend=self.backend)
        self.assertEqual(self.k.events,[])

    def test_invalid_connection_epoch(self):
        for val in (0,True,False,-1,1<<63,1.5,"10"):
            with self.subTest(val=val),self.assertRaises(PipePeerBoundaryError):
                observe_pipe_server_end_data_only(
                    borrowed_server_handle=200,expected_server_pid=42,
                    connection_epoch=val,_backend=self.backend)
        self.assertEqual(self.k.events,[])

    def test_invalid_timeout(self):
        for val in (0,-1,3001,True,1.0,None):
            with self.subTest(val=val),self.assertRaises(PipePeerBoundaryError):
                self.observe(timeout_ms=val)
        self.assertEqual(self.k.events,[])

    def test_clock_rollback(self):
        readings=iter((100,99))
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            self.observe(_clock_ms=lambda:next(readings))
        self.assertEqual(ctx.exception.code,"INVALID_OR_ROLLED_CLOCK")

    def test_timeout_before_second_snapshot(self):
        readings=iter((100,101,102,103,104,105,106,107))
        with self.assertRaises(PipePeerBoundaryError) as ctx:
            self.observe(_clock_ms=lambda:next(readings), timeout_ms=4)
        self.assertEqual(ctx.exception.code,"PIPE_OBSERVATION_DEADLINE")

    def test_result_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.observe().authentication="AUTHENTICATED"

    def test_abi_module_no_native_loader_or_sender(self):
        source=(Path(__file__).resolve().parents[1]/
                "src/sora_commander_next/service_pipe_handle_role_candidate.py").read_text("utf-8")
        for bad in ("ctypes.WinDLL(", "ctypes.windll.", "CreateNamedPipe(", "subprocess.",
                    "Popen(", "sqlite3.connect", "CreateProcessAsUser", "COMMITTED_FRESH"):
            self.assertNotIn(bad,source)


if __name__=="__main__":
    unittest.main()
