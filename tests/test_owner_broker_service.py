"""In-memory tests of the Host-to-Broker wire. No pipe, window or child process."""
import ast
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from sora_commander_next.broker_host import BrokerHost, decode_frame, encode_frame
from sora_commander_next.owner_broker_service import OwnerBrokerService, _SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
RID = "1" * 32
SID = "a" * 48
EXE = r"C:\Windows\System32\where.exe"
CWD = r"D:\Allowed"


class Backend:
    def __init__(self):
        self.calls = []
        self.result = {"status": "OK"}
        self.error = None

    def __getattr__(self, name):
        if name not in _SCHEMAS:
            raise AttributeError(name)

        def call(**arguments):
            self.calls.append((name, arguments))
            if self.error is not None:
                raise self.error
            return self.result
        return call


class Transport:
    def __init__(self, service):
        self.service = service
        self.frames = []
        self.closed = False

    def request(self, raw):
        self.frames.append(raw)
        return self.service.handle(raw)

    def close(self):
        self.closed = True


def frame(action, call_id="2" * 32, **arguments):
    return encode_frame({"v": 1, "action": action, "call_id": call_id,
                         "arguments": arguments})


class OwnerBrokerServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.service = OwnerBrokerService(self.backend)
        self.transport = Transport(self.service)
        self.host = BrokerHost(self.transport)

    def result(self, raw):
        return decode_frame(self.service.handle(raw))["result"]

    def test_every_host_action_reaches_the_backend_once(self):
        calls = [
            ("activity", self.host.activity),
            ("run_command", lambda: self.host.run_command(
                executable=EXE, argv=["x"], cwd=CWD, request_id=RID)),
            ("run_command_wait", lambda: self.host.run_command_wait(
                executable=EXE, argv=[], cwd=CWD, request_id=RID, timeout=5.0,
                output_limit=8192)),
            ("await_command_result", lambda: self.host.await_command_result("p1")),
            ("review_status", lambda: self.host.review_status("p1")),
            ("request_status", lambda: self.host.request_status(RID)),
            ("read_process_output", lambda: self.host.read_process_output(SID, 4)),
            ("await_process_output", lambda: self.host.await_process_output(SID)),
            ("stop_process", lambda: self.host.stop_process(SID)),
            ("stop_process_wait", lambda: self.host.stop_process_wait(SID)),
            ("list_sessions", self.host.list_sessions),
            ("previous_sessions", self.host.previous_sessions),
            ("previous_output", self.host.previous_output),
            ("status", lambda: self.host._call("status")),
            ("recovery_overview", lambda: self.host._call("recovery_overview")),
            ("create_directory", lambda: self.host._call("create_directory", path=CWD)),
            ("create_file", lambda: self.host._call("create_file", path=CWD, content="c")),
            ("move_file", lambda: self.host._call("move_file", source=CWD, destination=EXE)),
            ("request_source_patch_dev", lambda: self.host._call(
                "request_source_patch_dev", path=CWD, old_text="a", new_text="b",
                expected_sha256="a" * 64)),
            ("request_text_edit", lambda: self.host._call(
                "request_text_edit", path=CWD, content="c", expected_sha256="a" * 64)),
            ("request_text_create", lambda: self.host._call(
                "request_text_create", path=CWD, content="c")),
            ("edit_review_status", lambda: self.host._call("edit_review_status", pending_id="p1")),
        ]
        self.assertEqual({name for name, _ in calls}, set(_SCHEMAS))
        for name, call in calls:
            with self.subTest(name=name):
                self.backend.calls.clear()
                if name == "activity":
                    from sora_commander_next.activity import reply
                    self.backend.result = reply("recent", action="CHOOSE_LABELLED_ID")
                else:
                    self.backend.result = {"status": "OK"}
                self.assertEqual(call()["status"], "OK")
                self.assertEqual([item[0] for item in self.backend.calls], [name])

    def test_arguments_arrive_exactly(self):
        self.host.run_command(executable=EXE, argv=["a b", ""], cwd=CWD,
                              request_id=RID, timeout=7.0)
        self.assertEqual(self.backend.calls, [("run_command", {
            "executable": EXE, "argv": ["a b", ""], "cwd": CWD, "request_id": RID,
            "timeout": 7.0, "background": False})])

    def test_no_wire_action_can_approve_or_run_anything_else(self):
        for action in ("approve", "approve_local", "review_once", "close", "jobs",
                       "request_source_create", "request_source_patch", "send_input",
                       "dev_mutate", "_mutate", "__class__"):
            with self.subTest(action=action):
                result = self.result(frame(action))
                self.assertEqual(result["error"], "ActionNotAllowed")
        self.assertEqual(self.backend.calls, [])

    def test_unknown_or_missing_arguments_never_reach_the_backend(self):
        good = {"executable": EXE, "argv": [], "cwd": CWD, "request_id": RID,
                "background": False}
        for arguments in ({**good, "interactive": True}, {**good, "approved": True},
                          {k: v for k, v in good.items() if k != "request_id"}):
            with self.subTest(fields=sorted(arguments)):
                result = self.result(frame("run_command", **arguments))
                self.assertEqual(result["error"], "InvalidBrokerArguments")
        self.assertEqual(self.backend.calls, [])

    def test_wrong_types_never_reach_the_backend(self):
        good = {"executable": EXE, "argv": [], "cwd": CWD, "request_id": RID,
                "background": False}
        for change in ({"argv": "x"}, {"argv": [1]}, {"background": 0},
                       {"request_id": "A" * 32}, {"executable": ""},
                       {"timeout": True}, {"output_limit": 1.5},
                       {"cwd": "D:\\x\0y"}):
            with self.subTest(change=sorted(change)):
                result = self.result(frame("run_command", **{**good, **change}))
                self.assertEqual(result["error"], "InvalidBrokerArguments")
        for action, arguments in (("read_process_output", {"session_id": "x"}),
                                  ("read_process_output", {"session_id": SID, "offset": -1}),
                                  ("stop_process", {"session_id": RID}),
                                  ("review_status", {"pending_id": ""})):
            with self.subTest(action=action, fields=sorted(arguments)):
                self.assertEqual(self.result(frame(action, **arguments))["error"],
                                 "InvalidBrokerArguments")
        self.assertEqual(self.backend.calls, [])

    def test_same_frame_twice_is_one_backend_call(self):
        raw = frame("request_status", request_id=RID)
        self.assertEqual(self.result(raw)["status"], "OK")
        self.assertEqual(self.result(raw)["error"], "DuplicateBrokerCall")
        self.assertEqual(len(self.backend.calls), 1)

    def test_same_frame_on_racing_workers_is_one_backend_call(self):
        class SlowSet(set):
            def __contains__(self, item):
                found = super().__contains__(item)
                time.sleep(0.05)  # Widen the gap between the check and the add.
                return found

        self.service._seen_calls = SlowSet()
        raw = frame("request_status", request_id=RID)
        start = threading.Barrier(8)
        results = []

        def worker():
            start.wait()
            results.append(self.result(raw))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(sorted(item.get("error", "") for item in results),
                         [""] + ["DuplicateBrokerCall"] * 7)

    def test_a_waiting_backend_call_does_not_block_other_calls(self):
        release = threading.Event()
        entered = threading.Event()
        real = self.backend.__getattr__("request_status")

        def waits(**arguments):
            entered.set()
            self.assertTrue(release.wait(10))
            return real(**arguments)

        with patch.object(self.backend, "request_status", waits, create=True):
            thread = threading.Thread(
                target=self.result, args=(frame("request_status", request_id=RID),))
            thread.start()
            self.assertTrue(entered.wait(10))
            other = []
            second = threading.Thread(target=lambda: other.append(
                self.result(frame("list_sessions", call_id="3" * 32))))
            second.start()
            second.join(5)
            finished = not second.is_alive()
            release.set()
            thread.join(10)
            second.join(10)
        self.assertTrue(finished, "a waiting call held up another call")
        self.assertEqual(other[0]["status"], "OK")

    def test_rejected_frame_does_not_consume_its_call_id(self):
        bad = frame("request_status", request_id="x")
        good = frame("request_status", request_id=RID)
        self.assertEqual(self.result(bad)["error"], "InvalidBrokerArguments")
        self.assertEqual(self.result(good)["status"], "OK")

    def test_bad_sha_never_reaches_a_writer(self):
        for action, arguments in (
                ("request_text_edit", {"path": CWD, "content": "c", "expected_sha256": "A" * 64}),
                ("request_source_patch_dev", {"path": CWD, "old_text": "a", "new_text": "b",
                                              "expected_sha256": "short"})):
            with self.subTest(action=action):
                self.assertEqual(self.result(frame(action, **arguments))["error"],
                                 "InvalidBrokerArguments")
        self.assertEqual(self.backend.calls, [])

    def test_full_call_guard_denies_without_evicting(self):
        with patch("sora_commander_next.owner_broker_service._MAX_SEEN_CALLS", 1):
            self.assertEqual(self.result(frame("list_sessions"))["status"], "OK")
            result = self.result(frame("list_sessions", call_id="3" * 32))
            self.assertEqual(result["error"], "BrokerCallGuardFull")
            self.assertEqual(self.result(frame("list_sessions"))["error"],
                             "DuplicateBrokerCall")
        self.assertEqual(len(self.backend.calls), 1)

    def test_backend_failure_is_unknown_without_exception_text(self):
        self.backend.error = RuntimeError(r"secret D:\private path")
        result = self.host.run_command(executable=EXE, argv=[], cwd=CWD, request_id=RID)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(result["request_id"], RID)
        self.assertIs(result["auto_retry"], False)
        self.assertNotIn("secret", str(result))
        self.assertEqual(len(self.transport.frames), 1)

    def test_invalid_backend_reply_is_unknown(self):
        for reply in (None, [], {}, {"status": 1}):
            with self.subTest(reply=reply):
                self.backend.result = reply
                result = self.host.request_status(RID)
                self.assertEqual(result["status"], "OUTCOME_UNKNOWN")

    def test_oversized_reply_is_unknown_not_a_denial(self):
        self.backend.result = {"status": "RUNNING", "request_id": RID,
                               "stdout": "x" * 300000}
        result = self.host.run_command(executable=EXE, argv=[], cwd=CWD, request_id=RID)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(result["error"], "BrokerReplyTooLarge")
        self.assertEqual(result["request_id"], RID)

    def test_reply_above_the_request_limit_fits(self):
        self.backend.result = {"status": "RUNNING", "stdout": "x" * 200000}
        self.assertEqual(len(self.host.request_status(RID)["stdout"]), 200000)

    def test_request_limit_stays_small(self):
        result = self.host.run_command(executable=EXE, argv=["x" * 9000], cwd=CWD,
                                       request_id=RID)
        self.assertEqual(result["error"], "SmallFrameScopeExceededOrInvalid")
        self.assertEqual(self.backend.calls, [])

    def test_invalid_frames_are_denied(self):
        for raw in (b"", b"[]", b"{", b'{"v":1}', "é".encode("utf-8"),
                    encode_frame({"v": 2, "action": "list_sessions",
                                  "call_id": "2" * 32, "arguments": {}}),
                    encode_frame({"v": 1, "action": "list_sessions",
                                  "call_id": "short", "arguments": {}}),
                    encode_frame({"v": True, "action": "list_sessions",
                                  "call_id": "2" * 32, "arguments": {}}),
                    encode_frame({"v": 1, "action": "list_sessions", "call_id": "2" * 32,
                                  "arguments": {}, "approved": True}),
                    b'{"action":"list_sessions","arguments":{},"call_id":"'
                    + b"2" * 32 + b'","v":1,"v":1}',
                    b'{ "v":1 }'):
            with self.subTest(raw=raw[:24]):
                self.assertEqual(self.result(raw)["error"], "InvalidBrokerRequest")
        self.assertEqual(self.backend.calls, [])

    def test_host_treats_an_unbound_reply_as_unknown(self):
        self.transport.request = lambda raw: self.service.handle(b"{")
        result = self.host.list_sessions()
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertIs(result["auto_retry"], False)
        self.assertEqual(self.backend.calls, [])

    def test_backend_must_expose_every_action(self):
        with self.assertRaises(TypeError):
            OwnerBrokerService(object())

    def test_module_cannot_start_or_approve_anything_itself(self):
        path = ROOT / "src" / "sora_commander_next" / "owner_broker_service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, {"math", "re", "threading", "broker_host", "activity", "no_answer"})
        # The added dependency must itself stay entirely data-only.
        pure = ast.parse((path.parent / "no_answer.py").read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(pure)))

    def test_actions_exclude_interactive_and_source_writes(self):
        fields = set().union(*(required | optional for required, optional in _SCHEMAS.values()))
        self.assertNotIn("interactive", fields)
        self.assertFalse({name for name in _SCHEMAS if "approve" in name})
        self.assertFalse({"request_source_create", "request_source_patch", "send_input"}
                         & set(_SCHEMAS))


class RealHostBackendTests(unittest.TestCase):
    """The existing Host as backend. Its empty policy denies before any launch."""

    def test_denied_command_starts_nothing(self):
        from sora_commander_next.host import Host
        from sora_commander_next.owner_broker_dev import OwnerBrokerBackend
        backend = Host()
        self.addCleanup(backend.close)
        host = BrokerHost(Transport(OwnerBrokerService(OwnerBrokerBackend(backend))))
        with patch("subprocess.Popen", side_effect=AssertionError("no launch")) as popen:
            result = host.run_command(executable=EXE, argv=[], cwd=CWD, request_id=RID)
            sessions = host.list_sessions()
            status = host.request_status(RID)
        self.assertEqual(result["status"], "DENY")
        self.assertEqual(result["request_id"], RID)
        self.assertEqual(sessions["status"], "OK")
        self.assertEqual(sessions["count"], 0)
        self.assertEqual(status["request_id"], RID)
        popen.assert_not_called()

    def test_every_read_reply_of_the_real_host_is_accepted(self):
        from sora_commander_next.host import Host
        from sora_commander_next.owner_broker_dev import OwnerBrokerBackend
        backend = Host()
        self.addCleanup(backend.close)
        host = BrokerHost(Transport(OwnerBrokerService(OwnerBrokerBackend(backend))))
        with patch("subprocess.Popen", side_effect=AssertionError("no launch")) as popen:
            replies = {
                "status": host._call("status"),
                "recovery_overview": host._call("recovery_overview"),
                "review_status": host.review_status("p" * 32),
                "await_command_result": host.await_command_result("p" * 32, wait_seconds=0),
                "request_status": host.request_status(RID),
                "read_process_output": host.read_process_output(SID),
                "await_process_output": host.await_process_output(SID, wait_seconds=0),
                "stop_process": host.stop_process(SID),
                "stop_process_wait": host.stop_process_wait(SID, wait_seconds=0),
                "list_sessions": host.list_sessions(),
                "previous_sessions": host.previous_sessions(),
                "previous_output": host.previous_output(),
            }
        popen.assert_not_called()
        for name, reply in replies.items():
            with self.subTest(name=name):
                self.assertNotEqual(reply.get("error"), "BrokerBackendReplyInvalid", reply)
                self.assertNotEqual(reply.get("error"), "BrokerBackendFailed", reply)
                self.assertNotEqual(reply.get("error"), "BrokerReplyUnconfirmed", reply)


if __name__ == "__main__":
    unittest.main()
