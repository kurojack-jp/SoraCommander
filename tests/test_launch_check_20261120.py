"""Launch preflight checks with synthetic tables and local Windows API checks."""

from contextlib import redirect_stderr, redirect_stdout
import ctypes
import io
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from tools import scn_launch_check as check


ROOT = Path(check.__file__).absolute().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows launch checks")
class LaunchCheckTests(unittest.TestCase):
    def invoke(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = check.main(args)
        return code, out.getvalue().splitlines()

    def test_toolhelp_snapshot_matches_complete_executable_name(self):
        for name, expected in (("TUNNEL-Client.EXE", 3), ("old-tunnel-client.exe", 0)):
            with self.subTest(name=name):
                api = MagicMock()
                api.CreateToolhelp32Snapshot.return_value = 19

                def first(handle, pointer):
                    entry = ctypes.cast(pointer, ctypes.POINTER(check.ProcessEntry)).contents
                    self.assertEqual(entry.dwSize, ctypes.sizeof(check.ProcessEntry))
                    entry.szExeFile = "other.exe"
                    return True

                def next_entry(handle, pointer):
                    entry = ctypes.cast(pointer, ctypes.POINTER(check.ProcessEntry)).contents
                    if entry.szExeFile == "other.exe":
                        entry.szExeFile = name
                        return True
                    return False

                api.Process32FirstW.side_effect = first
                api.Process32NextW.side_effect = next_entry
                with (patch.object(ctypes, "WinDLL", return_value=api),
                      patch.object(ctypes, "get_last_error", return_value=18)):
                    self.assertEqual(self.invoke("--tunnel-absent"), (expected, []))
                api.CreateToolhelp32Snapshot.assert_called_once_with(2, 0)
                api.CloseHandle.assert_called_once_with(19)

    def test_toolhelp_iteration_error_is_not_end_of_list(self):
        api = MagicMock()
        api.CreateToolhelp32Snapshot.return_value = 19

        def first(handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(check.ProcessEntry)).contents.szExeFile = "other.exe"
            return True

        api.Process32FirstW.side_effect = first
        api.Process32NextW.return_value = False
        with (patch.object(ctypes, "WinDLL", return_value=api),
              patch.object(ctypes, "get_last_error", return_value=5)):
            code, lines = self.invoke("--tunnel-absent")
        self.assertEqual(code, 7)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("[HOLD] Could not check tunnel processes."))
        api.CloseHandle.assert_called_once_with(19)

    def test_pipe_names_and_malformed_listing(self):
        for names, code in (([], 0), (["other-SoraCommanderNext-Broker-test"], 0),
                            (["SORACOMMANDERNEXT-BROKER-test"], 5), (None, 7), ([b"bad"], 7)):
            with self.subTest(names=names), patch.object(check.os, "listdir", return_value=names) as ls:
                self.assertEqual(self.invoke("--broker-absent"),
                                 (code, ["[HOLD] Could not check Broker pipes."] if code == 7 else []))
                ls.assert_called_once_with("\\\\.\\pipe\\")

    def test_wait_polls_every_half_second_until_pipe_or_deadline(self):
        for found, code, calls in ((True, 0, 1), (False, 4, 2)):
            with self.subTest(found=found):
                now = [0.0]

                def sleep(interval):
                    self.assertEqual(interval, 0.5)
                    now[0] += interval

                with (patch.object(check.time, "monotonic", side_effect=lambda: now[0]),
                      patch.object(check.time, "sleep", side_effect=sleep) as sleeper,
                      patch.object(check, "broker_present", side_effect=[False, found])):
                    self.assertEqual(self.invoke("--wait-broker", "1"), (code, []))
                    self.assertEqual(sleeper.call_count, calls)

    def test_port_probe_uses_exclusive_ipv4_bind_and_closes(self):
        api = check.socket
        for error, code in ((None, 0), (10048, 3), (10013, 3), (10050, 7)):
            with self.subTest(error=error), patch.object(api, "socket") as create:
                probe = create.return_value.__enter__.return_value
                if error:
                    probe.bind.side_effect = OSError(error, "synthetic bind failure")
                actual, lines = self.invoke("--port-free", "8080")
                self.assertEqual(actual, code)
                self.assertEqual(len(lines), int(code == 7))
                create.assert_called_once_with(api.AF_INET, api.SOCK_STREAM)
                probe.setsockopt.assert_called_once_with(api.SOL_SOCKET, api.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind.assert_called_once_with((api.inet_ntoa(bytes((127, 0, 0, 1))), 8080))
                create.return_value.__exit__.assert_called_once()
                probe.listen.assert_not_called()

    def test_listener_tables_use_native_layout_and_retry_buffer_growth(self):
        port = 8765
        network_port = int.from_bytes(port.to_bytes(2, "big"), "little")
        # SDK row layouts packed independently of the implementation's ctypes types.
        def table(family, selected):
            rows = []
            for state, local in ((2, 0), (5, network_port), (2, network_port if selected else 0)):
                if family == socket.AF_INET:
                    rows.append(struct.pack("<6I", state, 123, local, 0, 0, 42))
                else:
                    rows.append(struct.pack("<16sII16sIIII", bytes(range(16)), 1, local,
                                            bytes(16), 0, 0, state, 42))
            return struct.pack("<I", len(rows)) + b"".join(rows)

        for listener, error, expected in ((socket.AF_INET, 0, 3), (socket.AF_INET6, 0, 3),
                                           (None, 0, 0), (socket.AF_INET, 5, 7)):
            with self.subTest(listener=listener, error=error):
                api = MagicMock()
                calls = []

                def query(buffer, size_ptr, sort, family, kind, reserved):
                    self.assertEqual((sort, kind, reserved), (False, 3, 0))
                    size = ctypes.cast(size_ptr, ctypes.POINTER(ctypes.c_uint32)).contents
                    data = table(family, family == listener)
                    calls.append((family, size.value))
                    if buffer is None:
                        size.value = 4
                        return 122
                    if size.value < len(data):
                        size.value = len(data)
                        return 122
                    if family == socket.AF_INET6 and error:
                        return error
                    ctypes.memmove(buffer, data, len(data))
                    return 0

                api.GetExtendedTcpTable.side_effect = query
                with patch.object(ctypes, "WinDLL", return_value=api):
                    code, lines = self.invoke("--port-listening", str(port))
                self.assertEqual(code, expected)
                self.assertEqual(lines, ["[HOLD] Could not check TCP listeners. [WinError 5]"] if error else [])
                self.assertEqual(calls, [(family, size) for family in (socket.AF_INET, socket.AF_INET6)
                                         for size in (0, 4, len(table(family, False)))])

    def test_listener_finds_disposable_ipv4_and_ipv6_listeners(self):
        for family, address in ((socket.AF_INET, bytes((127, 0, 0, 1))),
                                (socket.AF_INET6, bytes(15) + b"\x01")):
            with socket.socket(family, socket.SOCK_STREAM) as listener:
                listener.bind((socket.inet_ntop(family, address), 0))
                listener.listen(1)
                self.assertEqual(self.invoke("--port-listening", str(listener.getsockname()[1])), (3, []))

    def test_query_failures_emit_one_hold(self):
        for args, target in ((["--tunnel-absent"], "tunnel_present"),
                             (["--broker-absent"], "broker_present"),
                             (["--wait-broker", "120"], "broker_present"),
                             (["--port-free", "65535"], "port_free"),
                             (["--port-listening", "65535"], "port_listening")):
            with self.subTest(args=args), patch.object(check, target, side_effect=OSError("test\nerror")):
                code, lines = self.invoke(*args)
                self.assertEqual((code, len(lines)), (7, 1))
                self.assertTrue(lines[0].startswith("[HOLD] Could not check "))

    def test_unexpected_query_result_fails_closed(self):
        with patch.object(check, "tunnel_present", return_value=None):
            self.assertEqual(self.invoke("--tunnel-absent"), (7, ["[HOLD] Could not check tunnel processes."]))

    def test_combined_checks_stop_in_fixed_order(self):
        args = ["--port-listening", "2", "--port-free", "1", "--wait-broker", "120",
                "--broker-absent", "--tunnel-absent"]
        with (patch.object(check, "tunnel_present", return_value=True) as tunnel,
              patch.object(check, "broker_present", return_value=True) as broker,
              patch.object(check, "wait_broker", return_value=False) as wait,
              patch.object(check, "port_free", return_value=False) as port,
              patch.object(check, "port_listening", return_value=True) as listening):
            self.assertEqual(self.invoke(*args), (3, []))
            broker.assert_not_called()
            tunnel.return_value = False
            self.assertEqual(self.invoke(*args), (5, []))
            wait.assert_not_called()
            broker.return_value = False
            self.assertEqual(self.invoke(*args), (4, []))
            port.assert_not_called()
            wait.return_value = True
            self.assertEqual(self.invoke(*args), (3, []))
            port.return_value = True
            listening.assert_not_called()
            self.assertEqual(self.invoke(*args), (3, []))
            listening.return_value = False
            self.assertEqual(self.invoke(*args), (0, []))

    def test_invalid_arguments_never_run_queries(self):
        for args in ([], ["--tunnel"], ["--unknown"], ["--wait-broker"], ["--wait-broker", "0"],
                     ["--wait-broker", "121"], ["--wait-broker", "1.5"], ["--port-free", "0"],
                     ["--port-free", "65536"], ["--port-free", "abc"], ["--port-listening", "0"],
                     ["--port-listening", "65536"], ["--port-listening", "abc"]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as failure:
                self.invoke(*args)
            self.assertEqual(failure.exception.code, 2)

    def test_checkout_rule_runs_before_queries(self):
        with (patch.object(check, "__file__", str(ROOT / "bad&checkout/tools/helper.py")),
              patch.object(check, "tunnel_present") as query):
            code, lines = self.invoke("--tunnel-absent")
            self.assertEqual((code, len(lines)), (6, 1))
            self.assertIn("unsupported character", lines[0])
            query.assert_not_called()

    def test_live_process_and_pipe_queries_only(self):
        # This read-only invocation needs no profile or filesystem fixture.
        profile = str(ROOT / "temp" / "unused-launch-check-profile")
        result = subprocess.run(
            [sys.executable, "-I", "-B", check.__file__, "--tunnel-absent", "--broker-absent"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "USERPROFILE": profile, "LOCALAPPDATA": profile},
        )
        self.assertIn(result.returncode, (0, 3, 5), result.stdout + result.stderr)


class PublicLauncherTests(unittest.TestCase):
    def test_public_batch_launchers_do_not_name_a_powershell_executable(self):
        # Also works in the exported tree, which omits the development builder.
        names = {
            "set-tunnel-key.cmd", "next-development.cmd", "next-development-broker-client.cmd",
            "start-broker-mode.cmd", "install-broker-autostart.cmd", "uninstall-broker-autostart.cmd",
            "start-owner-broker-dev.cmd", "start-sora-commander-broker-dev.cmd",
            "start-sora-commander-local.cmd", "setup.cmd", "update.cmd", "start-muse-bridge.cmd",
            "start-broker.cmd", "start-broker-tunnel.cmd", "start-usual-mode.cmd",
            "mcp-broker-client.cmd", "mcp-direct.cmd",
            "start-muse-bridge-tailscale.cmd", "install-muse-bridge-autostart.cmd",
            "uninstall-muse-bridge-autostart.cmd",
        }
        if (ROOT / "tools/build_public_tree.py").is_file():
            from tools.build_public_tree import TOOLS
            self.assertEqual({Path(path).name for path in TOOLS if path.endswith(".cmd")}, names)
        for name in names:
            with self.subTest(launcher=name):
                text = (ROOT / "tools" / name).read_text(encoding="utf-8").casefold()
                self.assertNotIn("powershell.exe", text)
                self.assertNotIn("pwsh.exe", text)


class AutostartLauncherVenvTests(unittest.TestCase):
    def test_missing_venv_is_one_hold_line(self):
        from tests._inherited_test_temp import inherited_test_temp
        names = ("install-broker-autostart.cmd", "uninstall-broker-autostart.cmd",
                 "install-muse-bridge-autostart.cmd", "uninstall-muse-bridge-autostart.cmd")
        with inherited_test_temp() as scratch:
            tools = Path(scratch) / "checkout" / "tools"
            tools.mkdir(parents=True)
            for name in names:
                (tools / name).write_bytes((ROOT / "tools" / name).read_bytes())
            for name in names:
                with self.subTest(launcher=name):
                    result = subprocess.run(
                        [os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), "/d", "/c", ".\\" + name],
                        cwd=tools, capture_output=True, text=True, timeout=30,
                        env={**os.environ, "USERPROFILE": str(tools / "profile"),
                             "LOCALAPPDATA": str(tools / "local")},
                        stdin=subprocess.DEVNULL)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(r"[HOLD] Run tools\setup.cmd first (.venv is missing).", result.stdout)
