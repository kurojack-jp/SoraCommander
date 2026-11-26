"""Synthetic stage 1 checks through the DEV entry, Host boundary and SDK wire."""

from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import stat
import sys
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError

from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from tests._inherited_test_temp import inherited_test_temp
from sora_commander_next import vm_control as control


_SESSION = "a" * 48
_PENDING = "b" * 32
_PRIVATE = 10 << 24
_MARKER = "private synthetic value"
_ROOT = Path(__file__).resolve().parents[1]


def _raw(base):
    return {
        "vm_name": "DEV VM", "address": str(IPv4Address(_PRIVATE + 1)),
        "bind_local": str(IPv4Address(_PRIVATE + 2)), "port": 19001,
        "key_file": str(base / "key"), "send_folder": str(base / "send"),
        "receive_folder": str(base / "receive"),
        "max_minutes": 1, "max_inputs": 3, "tools": ["screenshot", "wait"],
    }


def _config():
    fields = _raw(_ROOT)
    for field in ("key_file", "send_folder", "receive_folder"):
        fields[field] = Path(fields[field])
    fields["tools"] = tuple(fields["tools"])
    return control.VmConfig(**fields)


def _timeout(reason):
    return {"status": "TIMED_OUT", "reason": reason, "stage": "FIRST_GUI",
            "approval_state": "EXPIRED", "launch_outcome": "NOT_STARTED",
            "auto_retry": False, "required_action": "REPORT_AND_WAIT"}


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(inherited_test_temp())
        self.env = {"USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.home)}
        self.path = Path(self.home) / ".config" / "sora-commander" / "vm-control.json"
        self.raw = _raw(Path(self.home))
        for field in ("send_folder", "receive_folder"):
            Path(self.raw[field]).mkdir()

    def write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)

    def test_absent_config_and_first_registration_with_no_key(self):
        self.assertIsNone(control.load_vm_control({}))
        self.assertIsNone(control.load_vm_control(self.env))
        self.write(json.dumps(self.raw).encode())
        original = Path.lstat
        inspected = []

        def lstat(path):
            inspected.append(path)
            return original(path)

        with patch.object(Path, "lstat", lstat):
            config = control.load_vm_control(self.env)
        self.assertEqual(self.path, control.tasks_file(self.env).with_name("vm-control.json"))
        self.assertEqual(config.tools, ("screenshot", "wait"))
        self.assertEqual(config.address, self.raw["address"])
        self.assertIn(config.key_file, inspected)
        self.assertFalse(config.key_file.exists())
        self.assertEqual(config.key_file, Path(self.raw["key_file"]))

    def test_registration_refuses_nonlocal_and_overlapping_paths(self):
        send = Path(self.raw["send_folder"])
        child = send / "child"
        child.mkdir()
        for fields in (
            {"key_file": str(send / "missing-key")},
            {"key_file": str(send)},
            {"receive_folder": str(send).swapcase()},
            {"receive_folder": str(child)},
            {"send_folder": str(Path(self.home) / "missing")},
            {"key_file": str(Path(self.home) / "missing" / "key")},
            {"key_file": r"\\host\share\key"},
            {"send_folder": r"\\host\share\send"},
            {"receive_folder": r"\\host\share\receive"},
        ):
            with self.subTest(fields=tuple(fields)):
                self.write(json.dumps(self.raw | fields).encode())
                with self.assertRaisesRegex(control.VmConfigurationError, "^invalid or unreadable configuration$"):
                    control.load_vm_control(self.env)

    def test_config_contents_are_read_from_the_checked_handle(self):
        self.write(json.dumps(self.raw).encode())
        original = Path.open

        def forbidden_reopen(path, *args, **kwargs):
            self.assertNotEqual(path, self.path, "config was reopened by pathname")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", forbidden_reopen):
            self.assertIsNotNone(control.load_vm_control(self.env))

    def test_unknown_field(self):
        self.write(json.dumps({**self.raw, _MARKER: _MARKER}).encode())
        with self.assertRaisesRegex(control.VmConfigurationError, "^invalid or unreadable configuration$"):
            control.load_vm_control(self.env)

    def test_bad_configurations(self):
        data = json.dumps(self.raw).encode()
        cases = [b"[]", b"\xff", b"{", b" " * 4097, data[:-1] + b',"port":19001}',
                 data.replace(b"19001", b"NaN"),
                 data.replace(b"19001", b"Infinity"),
                 b"[" * 1500 + b"]" * 1500]
        for key in self.raw:
            cases.append(json.dumps({k: v for k, v in self.raw.items() if k != key}).encode())
        bad_values = {
            "vm_name": ["", "A\nB", 1, "A" * 101],
            "address": [str(IPv4Address(0)), str(IPv4Address(_PRIVATE)), [], 1],
            "bind_local": [self.raw["address"], str(IPv4Address(0)), "domain"],
            "port": [True, 1023, 65536, 19001.0, "19001"],
            "key_file": ["rel", self.raw["key_file"] + "\x00", 1],
            "send_folder": ["rel", None], "receive_folder": ["rel", []],
            "max_minutes": [False, 0, 31, 3.0],
            "max_inputs": [True, 0, 201, "5"],
            "tools": [[], ["wait"], ["screenshot", "screenshot"], ["screenshot", _MARKER],
                      ["screenshot", None], "screenshot"],
        }
        for key, bad in bad_values.items():
            cases.extend(json.dumps({**self.raw, key: value}).encode() for value in bad)
        for index, case in enumerate(cases):
            with self.subTest(case=index):
                self.write(case)
                with self.assertRaisesRegex(control.VmConfigurationError, "^invalid or unreadable configuration$"):
                    control.load_vm_control(self.env)

    def test_size_limit_inclusive(self):
        data = json.dumps(self.raw).encode().ljust(4096)
        self.write(data)
        self.assertIsNotNone(control.load_vm_control(self.env))
        self.write(data + b" ")
        with self.assertRaises(control.VmConfigurationError):
            control.load_vm_control(self.env)

    def test_links_reparse_points_hard_links_and_non_files(self):
        self.write(json.dumps(self.raw).encode())
        original = Path.lstat

        def with_stat(target, **overrides):
            def replacement(path):
                info = original(path)
                if path != target:
                    return info
                attrs = dict(st_mode=info.st_mode, st_nlink=info.st_nlink, st_file_attributes=0,
                             st_dev=info.st_dev, st_ino=info.st_ino)
                return SimpleNamespace(**(attrs | overrides))
            return replacement

        for target, attrs in [
            (self.path, {"st_mode": stat.S_IFLNK}),
            (self.path.parent, {"st_file_attributes": 0x400}),
            (self.path, {"st_nlink": 2}),
            (self.path, {"st_mode": stat.S_IFDIR}),
        ]:
            with self.subTest(rule=attrs), patch.object(Path, "lstat", with_stat(target, **attrs)):
                with self.assertRaises(control.VmConfigurationError):
                    control.load_vm_control(self.env)
        self.path.unlink()
        with patch.object(Path, "lstat", with_stat(self.path.parent, st_file_attributes=0x400)):
            with self.assertRaises(control.VmConfigurationError):
                control.load_vm_control(self.env)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.config = _config()
        self.clock = FakeClock()
        self.guard = Mock(spec=["verify"])
        self.guard.verify.return_value = True
        self.host = Mock(spec=["run_command_wait", "read_process_output", "request_status", "review_status"])
        self.host.run_command_wait.side_effect = self.launch
        self.host.read_process_output.side_effect = lambda _: self.running()
        self.host.review_status.return_value = {"status": "UNKNOWN"}
        self.host.request_status.side_effect = lambda request: {"request_id": request, "status": "UNKNOWN"}
        self.lease = control.VmLease(self.host, self.config, self.guard,
                                     clock=self.clock, pause=self.clock.advance)
        self.release_indicator = Mock()
        self.enterContext(patch.object(control.vm_lease, "hold_indicator", return_value=self.release_indicator))
        self.addCleanup(self.lease.close)
        self.payload = None

    def launch(self, **command):
        self.payload = command
        return self.running(stdout="")

    def running(self, **overrides):
        lease_id = self.payload["argv"][3]
        return {"status": "RUNNING", "request_id": self.payload["request_id"], "session_id": _SESSION,
                "stdout": f"LEASE {lease_id} STARTED\n", "exit_code": None} | overrides

    def test_exact_approval_command_only_once(self):
        result = self.lease.start(17)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(result["session_id"], _SESSION)
        self.host.run_command_wait.assert_called_once()
        command = self.host.run_command_wait.call_args.kwargs
        argv = command["argv"]
        self.assertRegex(argv[3], r"^[0-9a-f]{32}$")
        self.assertRegex(command["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(command, {
            "executable": str(Path(sys.executable).absolute()), "cwd": str(_ROOT),
            "argv": ["-I", str(_ROOT / "src" / "sora_commander_next" / "vm_lease.py"), "--lease-id", argv[3],
                     "--vm", self.config.vm_name, "--address", self.config.address, "--minutes", "1",
                     "--max-inputs", "3", "--tools", "screenshot,wait",
                     "--screenshots-are-sent-to-chatgpt", "yes", "--deadline", "60.0"],
            "timeout": 90, "output_limit": 512, "wait_seconds": 17, "request_id": command["request_id"],
        })
        self.host.read_process_output.assert_called_once_with(_SESSION)
        snapshot = self.lease.status()
        self.assertEqual(snapshot, {"state": "ACTIVE", "remaining_seconds": 60, "inputs_left": 3,
                                "tools": ["screenshot", "wait"], "reason": "READY", "agent": "NOT_CHECKED"})
        for value in (self.config.address, self.config.bind_local, str(self.config.key_file),
                      str(self.config.send_folder), str(self.config.receive_folder), argv[3],
                      command["request_id"], _SESSION):
            self.assertNotIn(value, str(snapshot))

    def test_non_start_results_preserved(self):
        cases = [
            ({"status": "DENIED", "reason": "DENIED"}, "NONE"),
            (_timeout("EXPIRED"), "NONE"), (_timeout("CLOSED"), "NONE"),
            ({"status": "TIMED_OUT", "reason": "EXPIRED"}, "STARTING"),
            ({"status": "PENDING", "pending_id": _PENDING}, "STARTING"),
            ({"status": "ERROR", "error": "OwnerReviewUnavailable", "launch_outcome": "NOT_STARTED"}, "NONE"),
            ({"status": "ERROR", "error": "LaunchNotStarted"}, "NONE"),
            ({"status": "ERROR"}, "STARTING"),
            ({"status": "BUSY", "error": "ConcurrencyLimit"}, "NONE"),
            ({"status": "DENY"}, "NONE"),
            ({"status": "DENY_REPLAY_GUARD_FULL"}, "NONE"),
            ({"status": "DENY_DUPLICATE_UNKNOWN"}, "NONE"),
            ({"status": "DENY_UNPROTECTED_WORKER"}, "NONE"),
            ({"status": "REVIEW", "pending_id": _PENDING}, "STARTING"),
            ({"status": "OUTCOME_UNKNOWN", "auto_retry": False}, "STARTING"),
            ({"status": "UNAVAILABLE", "error": "ProtectedBrokerUnavailable"}, "NONE"),
            ({"status": "UNAVAILABLE", "error": "ConnectionClosed"}, "NONE"),
            ({"status": "UNAVAILABLE", "error": "other"}, "STARTING"),
            ({"status": "UNAVAILABLE"}, "STARTING"),
            # The right stage alone does not prove an unanswered review.
            (_timeout("EXPIRED") | {"launch_outcome": "UNKNOWN"}, "STARTING"),
            # A session means something may have started, whatever the status says.
            ({"status": "DENIED", "session_id": _SESSION}, "STARTING"),
            ({"status": "BUSY", "session_id": _SESSION}, "STARTING"),
            ({"status": "PENDING", "pending_id": "not an id"}, "STARTING"),
        ]
        for reply, state in cases:
            with self.subTest(reply=reply):
                self.host.run_command_wait.side_effect = None
                self.host.run_command_wait.return_value = reply
                self.host.run_command_wait.reset_mock()
                lease = control.VmLease(self.host, self.config, self.guard, clock=self.clock)
                self.assertIs(lease.start(), reply)
                self.assertEqual(lease.status()["state"], state)
                if state == "STARTING":
                    self.assertEqual(lease.start(), {"status": "VM_SESSION_ACTIVE"})
                self.host.run_command_wait.assert_called_once()

    def test_pending_adopts_only_the_original_review_and_session(self):
        def pending(**command):
            self.payload = command
            return {"status": "PENDING", "pending_id": _PENDING}

        self.host.run_command_wait.side_effect = pending
        self.assertEqual(self.lease.start(0), {"status": "PENDING", "pending_id": _PENDING})
        self.host.review_status.return_value = self.running(stdout="")
        self.assertEqual(self.lease.status()["state"], "ACTIVE")
        self.host.review_status.assert_called_once_with(_PENDING)
        self.host.read_process_output.assert_called_once_with(_SESSION)
        self.host.request_status.assert_not_called()
        self.host.run_command_wait.assert_called_once()

    def test_unexpected_launch_payload_never_echoes_configuration_or_stderr(self):
        for detail in ({"stderr": _MARKER}, {"diagnostics": {"value": str(self.config.key_file)}},
                       {"error": self.config.address}, {"error": self.config.bind_local},
                       {"stdout": str(self.config.receive_folder)}):
            with self.subTest(kind=tuple(detail)):
                self.host.run_command_wait.side_effect = None
                self.host.run_command_wait.return_value = {"status": "ERROR", **detail}
                lease = control.VmLease(self.host, self.config, self.guard, clock=self.clock)
                self.assertEqual(lease.start(), {"status": "OUTCOME_UNKNOWN",
                                                 "error": "VmLaunchUnconfirmed", "auto_retry": False})
                self.assertEqual(lease.status()["state"], "STARTING")

    def test_unknown_launch_reconciles_with_its_original_request(self):
        def unreachable(**command):
            self.payload = command
            raise RuntimeError(_MARKER)

        self.host.run_command_wait.side_effect = unreachable
        self.assertEqual(self.lease.start(), {"status": "OUTCOME_UNKNOWN",
                                              "error": "VmLaunchUnconfirmed", "auto_retry": False})
        self.host.request_status.side_effect = lambda _: self.running()
        self.assertEqual(self.lease.status()["state"], "ACTIVE")
        self.host.request_status.assert_called_once_with(self.payload["request_id"])
        self.host.run_command_wait.assert_called_once()

    def test_an_unconfirmed_lease_stops_blocking_only_after_its_whole_lifetime(self):
        self.host.run_command_wait.side_effect = None
        self.host.run_command_wait.return_value = {"status": "OUTCOME_UNKNOWN", "auto_retry": False}
        self.lease.start()
        limit = self.config.max_minutes * 60 + 120
        self.clock.advance(limit - 1)
        self.assertEqual(self.lease.start(), {"status": "VM_SESSION_ACTIVE"})
        self.host.run_command_wait.assert_called_once()
        self.clock.advance(1)
        self.assertEqual(self.lease.start()["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.host.run_command_wait.call_count, 2)
        self.assertFalse(self.lease.check())

    def test_a_changed_pending_id_is_never_adopted(self):
        def pending(**command):
            self.payload = command
            return {"status": "PENDING", "pending_id": _PENDING}

        self.host.run_command_wait.side_effect = pending
        self.lease.start(0)
        self.host.review_status.return_value = self.running() | {"pending_id": "c" * 32}
        status = self.lease.status()
        self.assertEqual((status["state"], status["reason"]), ("STARTING", "LAUNCH_UNCONFIRMED"))
        self.host.read_process_output.assert_not_called()

    def test_incomplete_start_output_is_never_active(self):
        for change in ({"truncated": True}, {"output_incomplete": True}, {"truncated": None}):
            with self.subTest(change=change):
                self.host.read_process_output.side_effect = lambda _, change=change: self.running(**change)
                lease = control.VmLease(self.host, self.config, self.guard,
                                        clock=self.clock, pause=self.clock.advance)
                result = lease.start()
                self.assertEqual((result["status"], result["reason"]), ("STARTING", "START_LINE_MISSING"))
                self.assertFalse(lease.check())

    def test_start_and_status_show_fixed_fields_only(self):
        fields = {"state", "remaining_seconds", "inputs_left", "tools", "reason"}
        started = self.lease.start()
        self.assertEqual(set(started), fields | {"status", "session_id"})
        self.assertEqual(set(self.lease.status()), fields | {"agent"})
        for text in (json.dumps(started), json.dumps(self.lease.status())):
            for private in (self.config.address, self.config.bind_local, "key", "send", "receive", "python"):
                self.assertNotIn(private, text)

    def test_a_process_still_reported_running_keeps_blocking(self):
        self.lease.start()
        self.clock.advance(self.config.max_minutes * 60 + 1000)
        self.assertEqual(self.lease.start(), {"status": "VM_SESSION_ACTIVE"})
        self.host.run_command_wait.assert_called_once()
        self.host.read_process_output.side_effect = RuntimeError(_MARKER)
        self.lease.start()
        self.assertEqual(self.host.run_command_wait.call_count, 2)

    def test_start_line_is_exact_and_wait_is_bounded(self):
        wrong = f"LEASE {'c' * 32} STARTED\n"
        for stdout in ("", wrong, f"prefix {wrong}", f"{wrong}STOPPED\n"):
            with self.subTest(sample=stdout):
                self.host.read_process_output.side_effect = lambda _: self.running(stdout=stdout)
                lease = control.VmLease(self.host, self.config, self.guard,
                                         clock=self.clock, pause=self.clock.advance)
                before = self.clock.now
                result = lease.start(0)
                self.assertEqual((result["state"], result["reason"]), ("STARTING", "START_LINE_MISSING"))
                self.assertAlmostEqual(self.clock.now - before, 1)
                self.assertFalse(lease.check())
        self.host.read_process_output.side_effect = lambda _: self.running()
        self.assertEqual(lease.status()["state"], "ACTIVE")

    def test_request_id_mismatch_denies_existing_lease(self):
        self.assertEqual(self.lease.start()["status"], "ACTIVE")
        self.host.read_process_output.side_effect = lambda _: self.running(request_id="c" * 32)
        self.assertFalse(self.lease.check())
        self.assertEqual(self.lease.status()["reason"], "PROCESS_UNCONFIRMED")
        self.host.read_process_output.side_effect = lambda _: self.running()
        self.assertTrue(self.lease.check())

    def test_unavailable_malformed_and_unexpected_process_output(self):
        self.lease.start()
        for reply in (None, {"status": "UNAVAILABLE"}, {"status": []},
                      self.running(session_id="c" * 48), self.running(exit_code=0),
                      self.running(stdout=None),
                      self.running(status="CANCELLING"), RuntimeError(_MARKER)):
            with self.subTest(reply_type=type(reply)):
                self.host.read_process_output.side_effect = reply if isinstance(reply, Exception) else None
                self.host.read_process_output.return_value = reply
                self.assertFalse(self.lease.check())
                self.assertNotIn(_MARKER, str(self.lease.status()))
        self.host.run_command_wait.assert_called_once()

    def test_owner_verify_blocks_a_locked_session_without_ending_it(self):
        self.lease.start()
        self.guard.verify.side_effect = PermissionError(_MARKER)
        self.host.read_process_output.reset_mock()
        self.assertFalse(self.lease.check())
        self.host.read_process_output.assert_not_called()
        self.assertEqual(self.lease.status()["state"], "ACTIVE")
        self.guard.verify.side_effect = None
        self.assertTrue(self.lease.check())
        self.guard.verify.return_value = 1
        self.assertFalse(self.lease.check())

    def test_host_deadline_ends_even_while_process_says_running(self):
        self.lease.start()
        self.clock.advance(59)
        self.assertTrue(self.lease.check())
        self.clock.advance(1)
        self.assertFalse(self.lease.check())
        result = self.lease.status()
        self.assertEqual((result["state"], result["reason"], result["remaining_seconds"]),
                         ("ENDED", "HOST_DEADLINE", 0))
        self.assertEqual(self.lease.start(), {"status": "VM_SESSION_ACTIVE"})

    def test_delayed_approval_uses_the_deadline_supplied_to_the_window(self):
        def approve_late(**command):
            self.clock.advance(45)
            return self.launch(**command)

        self.host.run_command_wait.side_effect = approve_late
        result = self.lease.start()
        self.assertEqual((result["state"], result["remaining_seconds"]), ("ACTIVE", 15))
        self.assertEqual(float(self.payload["argv"][-1]), self.clock() + result["remaining_seconds"])
        self.clock.advance(15)
        self.assertFalse(self.lease.check())
        self.release_indicator.assert_called_once_with()

    def test_host_close_revokes_the_window_even_while_approval_is_pending(self):
        self.host.run_command_wait.side_effect = lambda **command: self.launch(**command) | {
            "status": "PENDING", "pending_id": _PENDING, "session_id": None}
        self.assertEqual(self.lease.start()["status"], "PENDING")
        self.lease.close()
        self.assertEqual(self.lease.status()["reason"], "HOST_CLOSED")
        self.assertEqual(self.lease.start()["status"], "VM_SESSION_ENDED")
        self.host.run_command_wait.assert_called_once()
        self.release_indicator.assert_called_once_with()

    def test_concurrent_start_and_active_start_are_refused(self):
        entered, release = Event(), Event()
        results = []

        def launch(**command):
            entered.set()
            release.wait(3)
            return self.launch(**command)

        self.host.run_command_wait.side_effect = launch
        worker = Thread(target=lambda: results.append(self.lease.start()))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.lease.status()["state"], "STARTING")
            self.assertEqual(self.lease.start(), {"status": "VM_SESSION_ACTIVE"})
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual([result["status"] for result in results], ["ACTIVE"])
        self.assertEqual(self.lease.start(), {"status": "VM_SESSION_ACTIVE"})
        self.host.run_command_wait.assert_called_once()

    def test_consumed_inputs_never_refunded(self):
        self.lease.start()
        for value in (0, True, -1, 4, 1.0):
            self.assertFalse(self.lease.consume_inputs(value))
        self.assertTrue(self.lease.consume_inputs(1))
        self.host.read_process_output.side_effect = RuntimeError(_MARKER)
        self.assertFalse(self.lease.consume_inputs(1))
        self.assertEqual(self.lease.status()["inputs_left"], 2)
        self.host.read_process_output.side_effect = lambda _: self.running()
        self.assertTrue(self.lease.consume_inputs(2))
        self.assertFalse(self.lease.consume_inputs(1))
        self.assertEqual(self.lease.status()["reason"], "INPUT_LIMIT")
        self.release_indicator.assert_called_once_with()

    def test_terminal_process_cannot_reactivate_the_old_lease(self):
        for status, tail, expected, code in [
            ("COMPLETED", "STOPPED", "STOPPED", 0),
            ("COMPLETED", "EXPIRED", "EXPIRED", 0),
            ("CANCELLED", "", "PROCESS_ENDED", 1),
            ("FAILED", "", "PROCESS_ENDED", 1),
            ("ERROR", "", "PROCESS_ENDED", 1),
        ]:
            with self.subTest(status=status, tail=tail):
                self.host.read_process_output.side_effect = lambda _: self.running()
                lease = control.VmLease(self.host, self.config, self.guard,
                                         clock=self.clock, pause=self.clock.advance)
                lease.start()
                self.host.read_process_output.side_effect = lambda _: self.running(
                    status=status, stdout=self.running()["stdout"] + tail + "\n", exit_code=code)
                self.assertFalse(lease.check())
                self.assertEqual(lease.status()["reason"], expected)
                self.host.read_process_output.side_effect = lambda _: self.running()
                self.assertFalse(lease.check())
                self.assertEqual(lease.status()["state"], "ENDED")
                original_id = self.payload["argv"][3]
                self.assertEqual(lease.start()["status"], "ACTIVE")
                self.assertNotEqual(original_id, self.payload["argv"][3])
        self.assertEqual(control.VmLease(self.host, self.config, self.guard).status()["state"], "NONE")


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_only_when_configured(self):
        host = Host()
        self.addCleanup(host.close)
        ordinary = build_server(host)
        candidate = build_server(host)
        control.register_vm_tools(candidate, host, None, None)
        self.assertEqual(await candidate.list_tools(), await ordinary.list_tools())
        self.assertEqual(await candidate.call_tool("tool_catalog", {}),
                         await ordinary.call_tool("tool_catalog", {}))
        with self.assertRaisesRegex(ToolError, "^Invalid tool request$"):
            await candidate.call_tool("vm_session_status", {})

    async def test_wire_preserves_existing_surface_and_adds_vm_tools(self):
        fake = Mock(spec=Host)
        fake.run_command_wait.return_value = {"status": "DENIED", "reason": "DENIED"}
        fake.ping.return_value = {"status": "OK"}
        ordinary = build_server(fake)
        candidate = build_server(fake)
        control.register_vm_tools(candidate, fake, _config(), Mock())
        existing = {tool.name: tool for tool in await ordinary.list_tools()}
        configured = {tool.name: tool for tool in await candidate.list_tools()}
        self.assertEqual(configured.keys() - existing.keys(),
                         {"vm_session_start", "vm_session_status", "vm_look", "vm_act", "vm_file_list",
                          "vm_file_put", "vm_file_get", "vm_file_status", "vm_file_cancel"})
        self.assertEqual({name: configured[name] for name in existing}, existing)
        self.assertEqual(await candidate.call_tool("ping", {}), await ordinary.call_tool("ping", {}))
        _, response = await candidate.call_tool("vm_session_start", {"wait_seconds": 0})
        self.assertEqual(response, fake.run_command_wait.return_value)
        fake.run_command_wait.assert_called_once()
        self.assertEqual(fake.run_command_wait.call_args.kwargs["wait_seconds"], 0)
        _, status = await candidate.call_tool("vm_session_status", {})
        self.assertEqual(status["state"], "NONE")
        self.assertEqual(status["agent"], "NOT_CHECKED")
        self.assertIsNone(configured["vm_look"].outputSchema)
        content = await candidate.call_tool("vm_look", {})
        self.assertEqual([json.loads(item.text) for item in content], [{"status": "VM_SESSION_REQUIRED"}])
        self.assertEqual(await candidate.call_tool("activity", {"unexpected": 1}),
                         await ordinary.call_tool("activity", {"unexpected": 1}))
        with self.assertRaisesRegex(ToolError, "^Invalid tool request$"):
            await candidate.call_tool("ping", {"unexpected": 1})

    async def test_wire_rejects_coercion_unknown_arguments_and_sanitizes_exceptions(self):
        fake = Mock(spec=Host)
        server = build_server(fake)
        control.register_vm_tools(server, fake, _config(), Mock())
        for arguments in ({"wait_seconds": True}, {"wait_seconds": "0"}, {"wait_seconds": None},
                          {"wait_seconds": float("nan")}, {"wait_seconds": float("inf")},
                          {"wait_seconds": -1}, {"wait_seconds": 91}, {_MARKER: _MARKER}, []):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ToolError, "^Invalid tool request$"):
                await server.call_tool("vm_session_start", arguments)
        with self.assertRaisesRegex(ToolError, "^Invalid tool request$"):
            await server.call_tool("vm_session_status", {_MARKER: True})
        fake.run_command_wait.assert_not_called()
        with patch.object(control.VmLease, "status", side_effect=RuntimeError(_MARKER)):
            with self.assertRaisesRegex(ToolError, "^Tool request failed$") as caught:
                await server.call_tool("vm_session_status", {})
        self.assertNotIn(_MARKER, str(caught.exception))
