"""Guarded synthetic tests for the Broker mode Windows logon task."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path, PureWindowsPath
import subprocess
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from sora_commander_next import windows_job
from tests._inherited_test_temp import inherited_test_temp
from tools import scn_broker_autostart as autostart
from tools import scn_update


NAMESPACE = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
TASK = "SoraCommander Broker mode"
SYSTEM_ROOT = r"C:\SyntheticWindows"
CHECKOUT = PureWindowsPath(r"C:\SyntheticLab")
ACCOUNT = r"EXAMPLE&LAB\SyntheticAccount"


class TaskXmlTests(unittest.TestCase):
    def test_interactive_logon_for_one_user_without_password_or_restart(self):
        xml = autostart.task_xml(CHECKOUT, ACCOUNT)
        task = ET.fromstring(xml.encode("utf-16"))

        def get(path):
            return task.findtext(path, namespaces=NAMESPACE)

        self.assertEqual(len(task.find("t:Triggers", NAMESPACE)), 1)
        self.assertEqual(get("t:Triggers/t:LogonTrigger/t:UserId"), ACCOUNT)
        principal = "t:Principals/t:Principal/"
        self.assertEqual(get(principal + "t:UserId"), ACCOUNT)
        self.assertEqual(get(principal + "t:LogonType"), "InteractiveToken")
        self.assertEqual(get(principal + "t:RunLevel"), "LeastPrivilege")
        self.assertEqual(get("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(get("t:Settings/t:ExecutionTimeLimit"), "PT0S")
        self.assertEqual(get("t:Settings/t:StartWhenAvailable"), "false")
        self.assertNotIn("RestartOnFailure", xml)
        self.assertNotIn("Password", xml)
        self.assertEqual(len(task.find("t:Actions", NAMESPACE)), 1)
        action = "t:Actions/t:Exec/"
        self.assertEqual(get(action + "t:Command"), str(CHECKOUT / r".venv\Scripts\pythonw.exe"))
        self.assertEqual(get(action + "t:Arguments"),
                         f'-I -B "{CHECKOUT / "tools" / "scn_broker_autostart.py"}" run')
        self.assertEqual(get(action + "t:WorkingDirectory"), str(CHECKOUT))


class MockedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "LOCALAPPDATA": "synthetic-appdata", "USERPROFILE": "synthetic-profile",
            "SystemRoot": SYSTEM_ROOT,
        }, clear=True))
        self.run = self.enterContext(patch.object(subprocess, "run"))
        self.popen = self.enterContext(patch.object(subprocess, "Popen"))
        self.job = self.enterContext(patch.object(windows_job, "WindowsJob")).return_value

    def test_network_timeout_is_one_hold_and_does_not_spawn_children(self):
        now = 0

        def sleep(seconds):
            nonlocal now
            now += seconds

        self.run.return_value = SimpleNamespace(returncode=3)
        log = io.StringIO()
        with (patch.object(autostart.time, "sleep", side_effect=sleep),
              patch.object(autostart.time, "monotonic", side_effect=lambda: now),
              patch.object(autostart, "_ordinary", side_effect=FileNotFoundError),
              patch.object(Path, "open", return_value=nullcontext(log))):
            self.assertEqual(autostart._run(Path("synthetic.log"), Path("synthetic.log.1")), 2)
        self.assertEqual(now, 180)
        self.assertEqual(self.run.call_count, 36)
        for probe in self.run.call_args_list:
            self.assertEqual(probe.args[0], [autostart.sys.executable, "-I", "-B",
                                             str(Path(autostart.__file__).absolute()), "--query-route"])
            self.assertLessEqual(probe.kwargs["timeout"], 5)
            self.assertEqual(probe.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(probe.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual([line for line in log.getvalue().splitlines() if line.startswith("[HOLD]")],
                         ["[HOLD] Network route not available after 180 seconds"])
        self.popen.assert_not_called()

    def test_network_readiness_after_wait_starts_mode_once(self):
        self.run.side_effect = [
            subprocess.TimeoutExpired("synthetic-probe", 5),
            SimpleNamespace(returncode=0),
        ]
        log = io.StringIO()
        with (patch.object(autostart.time, "sleep") as sleep,
              patch.object(autostart, "_ordinary", side_effect=FileNotFoundError),
              patch.object(Path, "open", return_value=nullcontext(log)),
              patch.object(autostart, "_start_mode", return_value=4) as start):
            self.assertEqual(autostart._run(Path("synthetic.log"), Path("synthetic.log.1")), 4)
        self.assertEqual(self.run.call_count, 2)
        sleep.assert_called_once()
        start.assert_called_once_with(log)

    def test_route_query_matches_active_default_route_to_connected_interface_and_family(self):
        def row(index, family):
            values = {"InterfaceIndex": index, "AddressFamily": family}
            return SimpleNamespace(Properties_=lambda name: SimpleNamespace(Value=values[name]))

        client = Mock()
        query = client.GetObject.return_value.ExecQuery
        for routes, interfaces, code in (
            ([row(7, 2)], [row(7, 2)], 0), ([row(7, 23)], [row(7, 23)], 0),
            ([row(7, 2)], [row(7, 23), row(9, 2)], 3), ([], [row(7, 2)], 3),
            ([row(7, 2)], [], 3), ([row(None, 2)], [row(None, 2)], 2),
        ):
            with self.subTest(code=code), patch.dict(autostart.sys.modules, {"win32com.client": client}):
                query.reset_mock()
                query.side_effect = [routes, interfaces]
                self.assertEqual(autostart._route_probe_exit(), code)
                self.assertIn("Store = 1 AND DestinationPrefix LIKE '%/0'", query.call_args_list[0].args[0])
                self.assertIn("WHERE ConnectionState = 1", query.call_args_list[1].args[0])
                client.GetObject.assert_called_with(r"winmgmts:\\.\root\StandardCimv2")

    def _run_with_clock(self, log):
        now = 0

        def sleep(seconds):
            nonlocal now
            now += seconds

        with (patch.object(autostart.time, "sleep", side_effect=sleep),
              patch.object(autostart.time, "monotonic", side_effect=lambda: now),
              patch.object(autostart, "_ordinary", side_effect=FileNotFoundError),
              patch.object(Path, "open", return_value=nullcontext(log)),
              patch.object(autostart, "_start_mode", return_value=4) as start):
            return autostart._run(Path("synthetic.log"), Path("synthetic.log.1")), start

    def test_route_query_error_retries_then_logs_one_hold_before_start(self):
        with patch.object(autostart, "_connected_default_route", side_effect=OSError("synthetic")):
            self.run.return_value = SimpleNamespace(returncode=autostart._route_probe_exit())
        log = io.StringIO()
        code, start = self._run_with_clock(log)
        self.assertEqual(code, 2)
        self.assertEqual([line for line in log.getvalue().splitlines() if line.startswith("[HOLD]")],
                         ["[HOLD] Could not inspect the Windows network route for 180 seconds"])
        self.assertEqual(self.run.call_count, 36)
        start.assert_not_called()
        self.popen.assert_not_called()

    def test_route_query_error_at_logon_then_route_starts_mode_once(self):
        self.run.side_effect = [SimpleNamespace(returncode=2), SimpleNamespace(returncode=0)]
        log = io.StringIO()
        code, start = self._run_with_clock(log)
        self.assertEqual(code, 4)
        self.assertEqual(self.run.call_count, 2)
        start.assert_called_once_with(log)

    def test_helper_has_no_shell_executable(self):
        source = Path(autostart.__file__).read_text(encoding="utf-8").casefold()
        for name in ("powershell.exe", "pwsh.exe"):
            self.assertNotIn(name, source)

    def test_scheduler_removes_only_the_broker_task(self):
        self.run.side_effect = [SimpleNamespace(returncode=code) for code in (0, 1, 0)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(autostart.main(["uninstall"]), 0)
        scheduler = str(PureWindowsPath(SYSTEM_ROOT) / "System32" / "schtasks.exe")
        self.assertEqual([item.args[0] for item in self.run.call_args_list], [
            [scheduler, "/Query", "/TN", TASK],
            [scheduler, "/End", "/TN", TASK],
            [scheduler, "/Delete", "/TN", TASK, "/F"],
        ])
        self.popen.assert_not_called()

    def test_unsafe_checkout_holds_before_log_or_task_operations(self):
        for action in ("install", "run"):
            with self.subTest(action=action), redirect_stderr(io.StringIO()) as output:
                with patch.object(autostart, "CHECKOUT", CHECKOUT / "invalid&path"):
                    with patch.object(autostart, "_log_paths") as logs:
                        self.assertEqual(autostart.main([action]), 6)
                    logs.assert_not_called()
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertIn("[HOLD]", output.getvalue())
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_tunnel_readiness_timeout_logs_one_hold_and_closes_only_its_job(self):
        closed = Event()

        def eof():
            closed.wait(5)
            return b""

        lines = iter([autostart.BROKER_READY + b"\r\n"])
        broker = Mock(stdin=Mock(close=Mock(side_effect=closed.set)),
                      stdout=Mock(readline=Mock(side_effect=lambda _: next(lines, None) or eof())))
        broker.poll.return_value = None
        tunnel = Mock()
        tunnel.wait.side_effect = subprocess.TimeoutExpired("synthetic-tunnel", 0.25)
        self.popen.side_effect = [broker, tunnel]
        log = io.StringIO()
        with (patch.object(autostart.sys, "path", autostart.sys.path.copy()),
              patch.object(autostart, "_wait_for_network"),
              patch.object(autostart, "TUNNEL_WAIT", 0.05),
              patch.object(autostart.socket, "create_connection",
                           side_effect=ConnectionRefusedError) as connect,
              patch.object(autostart, "_ordinary", side_effect=FileNotFoundError),
              patch.object(Path, "open", return_value=nullcontext(log))):
            self.assertEqual(autostart._run(Path("synthetic.log"), Path("synthetic.log.1")), 2)
        connect.assert_called()
        self.assertEqual([line for line in log.getvalue().splitlines() if line.startswith("[HOLD]")],
                         ["[HOLD] Tunnel health port not ready after 0.05 seconds"])
        self.assertEqual(self.popen.call_count, 2)
        for child in (broker, tunnel):
            child.kill.assert_not_called()
        self.job.close.assert_called_once()

    def test_broker_exit_six_is_clean_without_tunnel_or_killing_existing_broker(self):
        broker = Mock(stdin=Mock(), stdout=io.BytesIO(
            b"[HOLD] synthetic journal in use\r\n[INFO] Broker finished with code 6.\r\n"))
        broker.poll.return_value = 6
        self.popen.return_value = broker
        log = io.StringIO()
        with patch.object(autostart.sys, "path", autostart.sys.path.copy()):
            self.assertEqual(autostart._start_mode(log), 0)
        self.assertIn("Owner journal is already in use", log.getvalue())
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(self.popen.call_args.args[0][-1], autostart.BROKER_LAUNCHER)
        self.job.attach_and_resume.assert_called_once_with(broker)
        broker.kill.assert_not_called()
        self.job.close.assert_called_once()

    def test_broker_readiness_precedes_the_one_tunnel_start_and_output_stays_private(self):
        secrets = [b"synthetic-secret-key", b"synthetic-yaml-value", b"synthetic-token",
                   ".".join(("192", "0", "2", "51")).encode()]
        lines = iter([part + b"\r\n" for part in [*secrets, autostart.BROKER_READY]])
        closed = Event()

        def read_line(_size):
            line = next(lines, None)
            if line is None:
                closed.wait(5)
                return b""
            return line

        broker = Mock(stdin=Mock(close=Mock(side_effect=closed.set)),
                      stdout=Mock(readline=Mock(side_effect=read_line)))
        broker.poll.return_value = None
        tunnel = Mock()
        tunnel.wait.return_value = 19
        self.popen.side_effect = [broker, tunnel]
        log = io.StringIO()
        with patch.object(autostart.sys, "path", autostart.sys.path.copy()):
            self.assertEqual(autostart._start_mode(log), 19)
        self.assertEqual([item.args[0][-1] for item in self.popen.call_args_list],
                         [autostart.BROKER_LAUNCHER, autostart.TUNNEL_LAUNCHER])
        self.assertEqual(self.popen.call_args_list[0].kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(self.popen.call_args_list[1].kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.popen.call_args_list[1].kwargs["stdout"], subprocess.DEVNULL)
        for spawn in self.popen.call_args_list:
            self.assertEqual(spawn.kwargs["stderr"], subprocess.DEVNULL)
        for secret in secrets:
            self.assertNotIn(secret.decode(), log.getvalue())
        self.assertIn("Tunnel exit code: 19", log.getvalue())
        self.assertTrue(closed.is_set())
        self.job.close.assert_called_once()


class DiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(cls.enterClassContext(inherited_test_temp()))

    def setUp(self):
        self.appdata = self.scratch / self._testMethodName
        self.appdata.mkdir()
        self.log = self.appdata / "SoraCommanderNext" / autostart.LOG_NAME
        self.enterContext(patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.appdata), "USERPROFILE": str(self.appdata / "SyntheticProfile"),
            "USERNAME": "SyntheticAccount", "USERDOMAIN": "EXAMPLE",
        }))
        self.run = self.enterContext(patch.object(subprocess, "run"))
        self.popen = self.enterContext(patch.object(subprocess, "Popen"))

    @unittest.skipUnless(os.name == "nt", "Windows WMI is required")
    def test_live_read_only_process_and_route_queries(self):
        import win32com
        import win32com.gen_py as gen_py

        # The COM client may initialize its type-library cache on first import.
        cache = str(self.appdata / "gen_py")
        with patch.object(win32com, "__gen_path__", cache), patch.object(gen_py, "__path__", [cache]):
            rows = scn_update._process_rows()
            self.assertTrue(any(row["ProcessId"] == os.getpid() for row in rows))
            self.assertIs(type(autostart._connected_default_route()), bool)
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_install_creates_only_this_task_without_starting_it(self):
        observed = []

        def scheduler(argv, **kwargs):
            self.assertEqual(argv[:1], [str(PureWindowsPath(SYSTEM_ROOT) / "System32" / "schtasks.exe")])
            self.assertEqual(argv[2:4], ["/TN", TASK])
            if argv[1] == "/Query":
                return SimpleNamespace(returncode=1, stderr=b"ERROR: Synthetic task missing.")
            self.assertEqual(argv[1:5], ["/Create", "/TN", TASK, "/XML"])
            self.assertEqual(len(argv), 6)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            path = Path(argv[5])
            observed.append(path)
            self.assertEqual(path.parent, self.log.parent)
            self.assertEqual(path.read_text(encoding="utf-16"),
                             autostart.task_xml(CHECKOUT, r"EXAMPLE\SyntheticAccount"))
            return SimpleNamespace(returncode=0)

        self.run.side_effect = scheduler
        with (patch.dict(os.environ, {"SystemRoot": SYSTEM_ROOT}),
              patch.object(autostart, "CHECKOUT", Path(CHECKOUT)),
              patch.object(Path, "is_file", return_value=True),
              patch.object(autostart.ctypes, "FormatError", return_value="Synthetic task missing."),
              redirect_stdout(io.StringIO())):
            self.assertEqual(autostart.main(["install"]), 0)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.popen.assert_not_called()

    def test_log_rotates_only_above_one_mib_and_replaces_previous_log(self):
        self.log.parent.mkdir()
        rotated = self.log.with_suffix(".log.1")
        rotated.write_bytes(b"previous")
        old = b"L" * autostart.LOG_LIMIT
        self.log.write_bytes(old)
        with (patch.object(autostart, "_wait_for_network"),
              patch.object(autostart, "_start_mode", return_value=0)):
            self.assertEqual(autostart.main(["run"]), 0)
            self.assertEqual(rotated.read_bytes(), b"previous")
            oversized = self.log.read_bytes()
            self.assertTrue(oversized.startswith(old))
            self.assertEqual(autostart.main(["run"]), 0)
        self.assertEqual(rotated.read_bytes(), oversized)
        self.assertIn("autostart start", self.log.read_text(encoding="utf-8"))
        self.run.assert_not_called()
        self.popen.assert_not_called()
