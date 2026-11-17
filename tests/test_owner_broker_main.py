"""The real Broker entry point with the real Host, across two real processes.

Only actions that open no window are sent: reads, and a command the policy
denies before review. The journal switches stay off, so AppData is not used.
"""
import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import secrets
import subprocess
import sys
import unittest
import ctypes
import time
from ctypes import wintypes
from unittest.mock import patch

from tests._inherited_test_temp import inherited_test_temp
from tests._test_owner_lease import new_test_prefix

ROOT = Path(__file__).resolve().parents[1]
RID = "1" * 32
EXE = r"C:\Windows\System32\where.exe"


@unittest.skipUnless(os.name == "nt", "Windows Broker entry point")
class OwnerBrokerPipeNameTests(unittest.TestCase):
    def test_bad_overrides_stop_before_guard_lease_or_storage(self):
        from sora_commander_next import owner_broker_main as broker
        prefix = "\\\\.\\pipe\\SoraCommanderNext-"
        bad_names = ("\\\\.\\pipe\\Other-Broker", prefix, prefix + "..",
                     prefix + "Test\\child", prefix + "x" * 97, "")
        for name in bad_names:
            for switches in ([], ["--list-unknown"], ["--resolve-unknown", "X"]):
                with self.subTest(name=name, switches=switches), ExitStack() as stack:
                    for target in ("OwnerSessionGuard", "owner_journal_lease",
                                   "ensure_owner_storage", "LaunchLedger",
                                   "build_backend_host", "broker_is_running"):
                        stack.enter_context(patch.object(
                            broker, target, side_effect=AssertionError(target + " called")))
                    stack.enter_context(patch.object(
                        broker.sys.stdin, "isatty",
                        side_effect=AssertionError("piped-input check called")))
                    stdout, stderr = io.StringIO(), io.StringIO()
                    stack.enter_context(redirect_stdout(stdout))
                    stack.enter_context(redirect_stderr(stderr))
                    self.assertEqual(broker.main(["--pipe-name", name] + switches), 2)
                    self.assertEqual(stdout.getvalue(),
                        "[HOLD] --pipe-name must be a Sora Commander pipe name. "
                        "Broker not started.\n")
                    self.assertEqual(stderr.getvalue(), "")

    def test_good_override_reaches_mocked_lease_in_all_modes(self):
        from sora_commander_next import owner_broker_main as broker
        name = "\\\\.\\pipe\\SoraCommanderNext-Test-" + "a" * 24
        for switches in ([], ["--list-unknown"], ["--resolve-unknown", "X"]):
            with self.subTest(switches=switches), ExitStack() as stack:
                guard = stack.enter_context(patch.object(broker, "OwnerSessionGuard"))
                lease = stack.enter_context(patch.object(
                    broker, "owner_journal_lease",
                    side_effect=broker.OwnerJournalLeaseError("synthetic lease refusal")))
                stack.enter_context(patch.object(broker.sys.stdin, "isatty", return_value=True))
                for target in ("ensure_owner_storage", "LaunchLedger", "build_backend_host",
                               "broker_is_running", "_run_with_lease"):
                    stack.enter_context(patch.object(
                        broker, target, side_effect=AssertionError(target + " called")))
                stdout = stack.enter_context(redirect_stdout(io.StringIO()))
                self.assertEqual(broker.main(["--pipe-name", name] + switches), 6)
                guard.assert_called_once_with()
                guard.return_value.verify.assert_called_once_with()
                lease.assert_called_once_with()
                self.assertEqual(stdout.getvalue(),
                    "[HOLD] synthetic lease refusal; Broker not started.\n")

    def test_public_validator_preserves_current_pipe_names_and_tail_limits(self):
        from sora_commander_next.owner_broker_pipe import (
            _valid_name, broker_pipe_name, valid_broker_pipe_name)
        prefix = "\\\\.\\pipe\\SoraCommanderNext-"
        self.assertIs(_valid_name, valid_broker_pipe_name)
        for name in (broker_pipe_name("S-1-5-21-1-2-3-1001", 1),
                     prefix + "Test-" + "a" * 24,
                     prefix + "Absent-" + "b" * 24, prefix + "X",
                     prefix + "Az09-" + "x" * 91):
            with self.subTest(name=name):
                self.assertTrue(valid_broker_pipe_name(name))
        for name in (prefix, prefix + "x" * 97, prefix + "..", prefix + "Test\\child",
                     "\\\\.\\pipe\\Other-Broker", "", None, 1):
            with self.subTest(name=name):
                self.assertFalse(valid_broker_pipe_name(name))


def _guard_or_none():
    try:
        from sora_commander_next.windows_owner import OwnerSessionGuard
        return OwnerSessionGuard()
    except (ImportError, PermissionError):
        return None


@unittest.skipUnless(os.name == "nt" and _guard_or_none(), "unlocked Windows session required")
class OwnerBrokerMainTests(unittest.TestCase):
    def setUp(self):
        from sora_commander_next.mcp_adapter import build_server
        from sora_commander_next.owner_broker_client_main import build_client_host
        from sora_commander_next.windows_owner import OwnerSessionGuard
        self.lease_prefix = new_test_prefix()
        self.name = "\\\\.\\pipe\\SoraCommanderNext-Test-" + secrets.token_hex(12)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("SORA_NEXT_")}
        env.update(PYTHONPATH=str(ROOT / "src"), SORA_NEXT_OWNER_PROFILE="tests")
        self.child = subprocess.Popen(
            [sys.executable, "-B", "-m", "tests._owner_broker_main_test_entry",
             self.lease_prefix,
             "--pipe-name", self.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=str(ROOT))
        self.addCleanup(self.finish_child)
        line = self.child.stdout.readline()
        if not line.startswith("[READY]"):
            self.fail(f"Broker did not start: {line!r} {self.child.stderr.read()[-600:]!r}")
        self.host = build_client_host(
            OwnerSessionGuard(), ROOT,
            {"SORA_NEXT_OWNER_PROFILE": "tests", "SORA_NEXT_EDIT_PILOT": "1",
             "SORA_NEXT_CREATE_PILOT": "1"},
            pipe_name=self.name)
        self.server = build_server(self.host)

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
        if not self.child.stdin.closed:
            self.child.stdin.close()

    def call(self, name, arguments=None):
        return asyncio.run(self.server.call_tool(name, arguments or {}))[1]

    def test_client_offers_the_full_tool_list(self):
        from sora_commander_next.mcp_adapter import _local_registered_catalog
        self.assertEqual(_local_registered_catalog(self.server, self.host)
                         ["registered_tool_count"], 54)

    def test_status_comes_from_the_real_host_in_the_broker(self):
        status = self.call("status")
        self.assertEqual(status["status"], "OK")
        self.assertIs(status["remote_ready"], False)
        self.assertEqual(status["approval"], "LOCAL_DEMO_ONLY")
        self.assertEqual(status["host_role"], "DEV_BROKER_CLIENT")

    def test_sessions_and_recovery_are_empty_and_read_only(self):
        self.assertEqual(self.call("list_sessions")["count"], 0)
        self.assertNotIn(self.call("recovery_overview").get("error"),
                         ("BrokerBackendFailed", "BrokerBackendReplyInvalid"))

    def test_policy_denies_outside_its_root_with_no_window_or_job(self):
        result = self.call("run_command", {"executable": EXE, "argv": ["x"], "cwd": "C:\\",
                                           "request_id": RID})
        self.assertEqual(result["status"], "DENY")
        self.assertEqual(result["request_id"], RID)
        self.assertEqual(self.call("list_sessions")["count"], 0)

    def test_second_broker_refuses_the_same_pipe(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("SORA_NEXT_")}
        env.update(PYTHONPATH=str(ROOT / "src"), SORA_NEXT_OWNER_PROFILE="tests")
        result = subprocess.run(
            [sys.executable, "-B", "-m", "tests._owner_broker_main_test_entry",
             self.lease_prefix,
             "--pipe-name", self.name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=30,
            cwd=str(ROOT))
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        self.assertIn("Owner journal", result.stdout)
        self.assertEqual(self.call("status")["status"], "OK")

    def test_ledger_commands_refuse_while_a_broker_runs(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("SORA_NEXT_")}
        env.update(PYTHONPATH=str(ROOT / "src"))
        for arguments in (["--list-unknown"], ["--resolve-unknown", RID]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tests._owner_broker_main_test_entry",
                     self.lease_prefix,
                     "--pipe-name", self.name, *arguments],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
                    timeout=30, cwd=str(ROOT))
                # Both maintenance commands now take the Owner journal lease first,
                # which the running test Broker holds. (On Windows a DEVNULL stdin
                # reports isatty() True, so --resolve-unknown reaches the lease too.)
                self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
                self.assertIn("[HOLD]", result.stdout)

    def test_piped_yes_cannot_clear_a_launch(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("SORA_NEXT_")}
        env.update(PYTHONPATH=str(ROOT / "src"))
        result = subprocess.run(
            [sys.executable, "-B", "-m", "tests._owner_broker_main_test_entry",
             self.lease_prefix,
             "--pipe-name", self.name, "--resolve-unknown", RID],
            input="yes\n", capture_output=True, text=True, env=env, timeout=30,
            cwd=str(ROOT))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("console", result.stdout)

    def test_end_of_input_stops_the_broker_cleanly(self):
        self.child.stdin.close()
        self.assertEqual(self.child.wait(timeout=20), 0)
        self.assertIn("[STOPPED]", self.child.stdout.read())
        self.assertEqual(self.call("status")["status"], "UNAVAILABLE")


@unittest.skipUnless(os.name == "nt", "Windows only")
class BrokerModeStopTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(inherited_test_temp()))
        self.prefix = "SoraCommanderNext-Test-Launcher-" + secrets.token_hex(12)
        source = (ROOT / "tools" / "start-broker-mode.cmd").read_text(encoding="utf-8")
        # Isolate OS discovery only. Preserve the complete batch control flow,
        # 30-second deadline, polling interval, and exit-code handling.
        self.assertEqual(source.count("SoraCommanderNext-Broker-*"), 2)
        self.assertEqual(source.count("Get-Process -Name 'tunnel-client'"), 1)
        source = source.replace("SoraCommanderNext-Broker-*", self.prefix + "*")
        source = source.replace("Get-Process -Name 'tunnel-client'",
                                "Get-Process -Name 'scn-" + secrets.token_hex(5) + "'")
        self.launcher = self.home / "start-broker-mode.cmd"
        self.launcher.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))
        for name, marker in (("start-owner-broker-dev.cmd", "broker-called"),
                             ("start-sora-commander-broker-dev.cmd", "tunnel-called")):
            (self.home / name).write_bytes(
                f'@echo off\r\n>"%~dp0{marker}" echo called\r\nexit /b 0\r\n'.encode("ascii"))

    def run_launcher(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("SORA_NEXT_") and key != "CONTROL_PLANE_API_KEY"}
        started = time.monotonic()
        result = subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c", str(self.launcher)],
            input="\n", capture_output=True, text=True, env=env,
            cwd=str(self.home), timeout=50, creationflags=subprocess.CREATE_NO_WINDOW)
        return result, time.monotonic() - started

    def test_unresponsive_broker_stops_without_starting_tunnel(self):
        result, elapsed = self.run_launcher()
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("[HOLD] The Broker pipe was not ready within 30 seconds.", result.stdout)
        self.assertGreaterEqual(elapsed, 30)
        self.assertTrue((self.home / "broker-called").is_file())
        self.assertFalse((self.home / "tunnel-called").exists())

    def test_existing_pipe_stops_without_starting_either_component(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateNamedPipeW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                           wintypes.DWORD, wintypes.LPVOID]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = create("\\\\.\\pipe\\" + self.prefix, 3 | 0x00080000,
                        0, 1, 1024, 1024, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result, _ = self.run_launcher()
        finally:
            if not close(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn("[HOLD] A Broker is already running.", result.stdout)
        self.assertFalse((self.home / "broker-called").exists())
        self.assertFalse((self.home / "tunnel-called").exists())


class OwnerBrokerLauncherTests(unittest.TestCase):
    def text(self, name):
        return (ROOT / "tools" / name).read_text(encoding="utf-8")

    def test_broker_launcher_holds_while_a_tunnel_runs_and_has_no_key(self):
        text = self.text("start-owner-broker-dev.cmd")
        self.assertIn("Get-Process -Name 'tunnel-client'", text)
        self.assertIn('set "CONTROL_PLANE_API_KEY="', text)
        self.assertNotIn("Watch-NextTunnel", text)
        self.assertEqual(text.count("sora_commander_next.owner_broker_main"), 1)
        self.assertIn('if /I not "%~1"=="--check" pause', text)

    def test_tunnel_launcher_only_replaces_the_mcp_command(self):
        text = self.text("start-sora-commander-broker-dev.cmd")
        self.assertIn('--control-plane.api-key "%KEYREF%"', text)
        self.assertIn('--mcp.command "cmd.exe /d /c %MCP_FORWARD%"', text)
        self.assertIn('set "CONTROL_PLANE_API_KEY="', text)
        self.assertEqual(text.count('"%TUNNEL%" run '), 1)
        self.assertNotIn("Watch-NextTunnel", text)

    def test_client_launcher_starts_no_broker_and_sets_no_review_or_journal(self):
        text = self.text("next-development-broker-client.cmd")
        self.assertIn("sora_commander_next.owner_broker_client_main", text)
        self.assertNotIn("owner_broker_main", text)
        for name in ("REVIEW_PILOT", "JOURNAL_PILOT", "RESULT_ARCHIVE_PILOT"):
            self.assertNotIn(name, text)

    def test_broker_switches_match_the_usual_launcher(self):
        def switches(name):
            return {line.strip() for line in self.text(name).splitlines()
                    if line.startswith('set "SORA_NEXT_')}
        self.assertEqual(switches("start-owner-broker-dev.cmd"),
                         switches("next-development.cmd"))

    def test_one_step_launcher_starts_the_broker_first_and_waits_for_its_pipe(self):
        text = self.text("start-broker-mode.cmd")
        broker = text.index('start "Sora Owner Broker" cmd.exe /d /c ""%BROKER%""')
        wait = text.index("'*\\SoraCommanderNext-Broker-*'){exit 0}")
        tunnel = text.index('call "%TUNNEL%"\n')
        self.assertLess(broker, wait)
        self.assertLess(wait, tunnel)
        self.assertIn("if errorlevel 4 (", text)
        self.assertIn("Get-Process -Name 'tunnel-client'", text)
        self.assertIn('set "CONTROL_PLANE_API_KEY="', text)
        # Only the two existing launchers start anything.
        for direct in ("owner_broker_main", "tunnel-client.exe", "--control-plane"):
            self.assertNotIn(direct, text)

    def test_one_step_launcher_names_the_real_pipe_prefix(self):
        from sora_commander_next.owner_broker_pipe import broker_pipe_name
        name = broker_pipe_name("S-1-5-21-1-2-3-1001", 1)
        self.assertTrue(name.split("\\")[-1].startswith("SoraCommanderNext-Broker-"))
        self.assertFalse("SoraCommanderNext-Test-x".startswith("SoraCommanderNext-Broker-"))

    def test_launchers_use_windows_line_endings(self):
        for name in ("start-owner-broker-dev.cmd", "start-sora-commander-broker-dev.cmd",
                     "next-development-broker-client.cmd", "start-broker-mode.cmd"):
            with self.subTest(name=name):
                data = (ROOT / "tools" / name).read_bytes()
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

    def test_live_launchers_are_unchanged_by_this_mode(self):
        for name in ("next-development.cmd", "start-sora-commander-local.cmd"):
            self.assertNotIn("owner_broker", self.text(name))


if __name__ == "__main__":
    unittest.main()
