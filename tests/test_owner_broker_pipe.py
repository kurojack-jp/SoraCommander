"""Two real Windows processes over the real pipe, with a fake backend.

No job, Owner window or tunnel is started. The child has no launch path.
"""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
RID = "1" * 32
SID = "a" * 48
EXE = r"C:\Windows\System32\where.exe"


@unittest.skipUnless(os.name == "nt", "Windows named pipe ownership")
class OwnerBrokerPipeOwnershipTests(unittest.TestCase):
    def test_real_pipe_owner_is_current_user_with_unchanged_dacl(self):
        from sora_commander_next import owner_broker_pipe as pipe
        from sora_commander_next.windows_owner import OwnerSessionGuard
        guard = OwnerSessionGuard()
        name = "\\\\.\\pipe\\SoraCommanderNext-Test-" + secrets.token_hex(12)
        service = Mock()
        service.handle.side_effect = AssertionError("ownership test must not dispatch")
        server = pipe.OwnerBrokerPipeServer(owner_guard=guard, service=service,
                                            pipe_name=name, workers=1)
        try:
            # Create real instances through the production SDDL path, but never
            # connect, impersonate, dispatch a request or open an Owner window.
            with patch.object(server, "_serve"), \
                    patch.object(pipe, "_ConvertSddl", wraps=pipe._ConvertSddl) as convert:
                server.start()
                self.assertEqual(convert.call_args.args[0],
                    f"O:{guard.sid}D:P(A;;GA;;;{guard.sid})(A;;GA;;;SY)")
                self.assertEqual(pipe._pipe_owner_sid(server._handles[0]), guard.sid)
        finally:
            self.assertTrue(server.stop(), "synthetic pipe did not stop")
        service.handle.assert_not_called()


def _guard_or_none():
    try:
        from sora_commander_next.windows_owner import OwnerSessionGuard
        return OwnerSessionGuard()
    except (ImportError, PermissionError):
        return None


@unittest.skipUnless(os.name == "nt" and _guard_or_none(), "unlocked Windows session required")
class OwnerBrokerPipeProcessTests(unittest.TestCase):
    def setUp(self):
        from sora_commander_next.broker_host import BrokerHost
        from sora_commander_next.owner_broker_pipe import OwnerBrokerPipeClient
        self.name = "\\\\.\\pipe\\SoraCommanderNext-Test-" + secrets.token_hex(12)
        env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT}")
        self.child = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "tests" / "_broker_pipe_child.py"), self.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)
        self.addCleanup(self.finish_child)
        line = self.child.stdout.readline().strip()
        if line != "READY":
            self.fail(f"child did not start: {line!r} {self.child.stderr.read()[:400]!r}")
        self.client = OwnerBrokerPipeClient(pipe_name=self.name, reply_seconds=30)
        self.host = BrokerHost(self.client)

    def finish_child(self):
        if self.child.poll() is None:
            self.child.stdin.close()
            try:
                self.child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=10)
        self.child.stdout.close()
        self.child.stderr.close()
        if self.child.stdin and not self.child.stdin.closed:
            self.child.stdin.close()

    def test_request_crosses_processes_and_is_authenticated(self):
        result = self.host.request_status(RID)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["request_id"], RID)

    def test_each_call_reaches_the_backend_once(self):
        self.assertEqual(self.host.request_status(RID)["calls"], 1)
        self.assertEqual(self.host.request_status(RID)["calls"], 2)

    def test_large_reply_arrives_whole(self):
        result = self.host.read_process_output(SID)
        self.assertEqual(result["stdout"], "x" * 200000)

    def test_large_request_arrives_for_a_dev_client(self):
        from sora_commander_next.owner_broker_dev import DevBrokerHost
        client = DevBrokerHost(self.client)
        result = client.dev_mutate("create_file", path=r"D:\x\a.txt", content="あ" * 21000)
        self.assertEqual(result["status"], "OK")

    def test_slow_call_does_not_block_another_call(self):
        slow = {}
        thread = threading.Thread(target=lambda: slow.update(
            self.host.await_process_output(SID, wait_seconds=3.0)))
        thread.start()
        try:
            time.sleep(0.5)
            started = time.monotonic()
            self.assertEqual(self.host.list_sessions()["status"], "OK")
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(slow, {})
        finally:
            thread.join(15)
        self.assertIs(slow["waited"], True)

    def test_broker_death_during_a_call_is_unknown_and_not_retried(self):
        result = self.host.stop_process(SID)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertIs(result["auto_retry"], False)
        self.assertEqual(self.child.wait(timeout=10), 9)
        # Nothing is listening now: known not sent, and never a local launch.
        after = self.host.request_status(RID)
        self.assertEqual(after["status"], "UNAVAILABLE")

    def test_invalid_frame_gets_a_denial_and_the_server_keeps_serving(self):
        from sora_commander_next.broker_host import decode_frame
        reply = decode_frame(self.client.request(b"{}"))
        self.assertEqual(reply["result"]["error"], "InvalidBrokerRequest")
        self.assertEqual(self.host.request_status(RID)["status"], "OK")

    def test_client_that_sends_nothing_does_not_stop_the_server(self):
        from sora_commander_next import owner_broker_pipe as pipe
        handle = pipe._CreateFile(self.name, pipe._CLIENT_ACCESS, 0, None, 3,
                                  pipe._CLIENT_FLAGS, None)
        self.assertNotIn(handle, (None, pipe._INVALID))
        pipe._Close(handle)
        self.assertEqual(self.host.request_status(RID)["status"], "OK")

    def test_oversized_frame_length_is_refused_and_serving_continues(self):
        from sora_commander_next import owner_broker_pipe as pipe
        handle = pipe._CreateFile(self.name, pipe._CLIENT_ACCESS, 0, None, 3,
                                  pipe._CLIENT_FLAGS, None)
        self.assertNotIn(handle, (None, pipe._INVALID))
        try:
            pipe._write_all(handle, pipe._HEADER.pack(1 << 30), time.monotonic() + 5)
        finally:
            pipe._Close(handle)
        self.assertEqual(self.host.request_status(RID)["status"], "OK")

    def test_client_that_allows_only_identification_is_refused_before_dispatch(self):
        from sora_commander_next import owner_broker_pipe as pipe
        from sora_commander_next.broker_host import encode_frame
        raw = encode_frame({"v": 1, "action": "request_status", "call_id": "2" * 32,
                            "arguments": {"request_id": RID}})
        flags = 0x40000000 | 0x00100000 | (1 << 16)  # overlapped, SQOS, identification
        handle = pipe._CreateFile(self.name, pipe._CLIENT_ACCESS, 0, None, 3, flags, None)
        self.assertNotIn(handle, (None, pipe._INVALID))
        try:
            pipe._write_frame(handle, raw, time.monotonic() + 5, pipe.MAX_REPLY_BYTES)
            with self.assertRaises(OSError):
                pipe._read_frame(handle, time.monotonic() + 5, pipe.MAX_REPLY_BYTES)
        finally:
            pipe._Close(handle)
        # The refused request never reached the backend: this is its first call.
        self.assertEqual(self.host.request_status(RID)["calls"], 1)

    def test_pipe_owned_by_another_identity_gets_nothing(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        with patch("sora_commander_next.owner_broker_pipe.current_windows_owner",
                   return_value=SimpleNamespace(sid="S-1-5-21-1-2-3-999")):
            result = self.host.run_command(executable=EXE, argv=[], cwd="C:\\",
                                           request_id=RID)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.host.request_status(RID)["calls"], 1)

    def test_second_server_cannot_take_the_name(self):
        from sora_commander_next.owner_broker_pipe import OwnerBrokerPipeServer
        from sora_commander_next.owner_broker_service import OwnerBrokerService
        from sora_commander_next.windows_owner import OwnerSessionGuard

        class Empty:
            pass

        class AnyBackend(Empty):
            def __getattr__(self, name):
                return lambda **arguments: {"status": "OK"}

        server = OwnerBrokerPipeServer(owner_guard=OwnerSessionGuard(),
                                       service=OwnerBrokerService(AnyBackend()),
                                       pipe_name=self.name, workers=1)
        with self.assertRaises(OSError):
            server.start()

    def test_stop_ends_the_child_cleanly(self):
        self.child.stdin.write("\n")
        self.child.stdin.flush()
        self.assertEqual(self.child.stdout.readline().strip(), "STOPPED")
        self.assertEqual(self.child.wait(timeout=10), 0)


@unittest.skipUnless(os.name == "nt", "Windows only")
class OwnerBrokerPipeLocalTests(unittest.TestCase):
    def test_absent_broker_is_unavailable_with_no_fallback(self):
        from sora_commander_next.broker_host import BrokerHost
        from sora_commander_next.owner_broker_pipe import OwnerBrokerPipeClient
        host = BrokerHost(OwnerBrokerPipeClient(
            pipe_name="\\\\.\\pipe\\SoraCommanderNext-Absent-" + secrets.token_hex(12)))
        result = host.run_command(executable=EXE, argv=[], cwd="C:\\", request_id=RID)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["request_id"], RID)

    def test_names_outside_the_prefix_are_refused(self):
        from sora_commander_next.owner_broker_pipe import OwnerBrokerPipeClient
        for name in ("\\\\.\\pipe\\Other", "\\\\server\\pipe\\SoraCommanderNext-X",
                     "\\\\.\\pipe\\SoraCommanderNext-a\\b", "\\\\.\\pipe\\SoraCommanderNext-",
                     None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                OwnerBrokerPipeClient(pipe_name=name)

    def test_server_requires_the_real_guard(self):
        from sora_commander_next.owner_broker_pipe import OwnerBrokerPipeServer
        with self.assertRaises(TypeError):
            OwnerBrokerPipeServer(owner_guard=object(), service=object(),
                                  pipe_name="\\\\.\\pipe\\SoraCommanderNext-X")

    def test_pipe_name_depends_on_owner_and_session(self):
        from sora_commander_next.owner_broker_pipe import _valid_name, broker_pipe_name
        first = broker_pipe_name("S-1-5-21-1", 1)
        self.assertTrue(_valid_name(first))
        self.assertNotEqual(first, broker_pipe_name("S-1-5-21-2", 1))
        self.assertNotEqual(first, broker_pipe_name("S-1-5-21-1", 2))


if __name__ == "__main__":
    unittest.main()
