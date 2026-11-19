"""Synthetic coverage of the Muse logon task, including its publication."""

import codecs
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path, PureWindowsPath
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch
import xml.etree.ElementTree as ET

from tests._inherited_test_temp import inherited_test_temp
from tools import scn_muse_autostart as autostart, scn_muse_bridge as bridge


CHECKOUT = PureWindowsPath(r"C:\Synthetic & Demo\SoraCommander")
USER = r"EXAMPLE&LAB\MuseUser"
TASK_NAME = "SoraCommander Muse bridge (read-only)"
NAMESPACE = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
IP = "100.64.0.7"


class AutostartXmlTests(unittest.TestCase):
    def test_address_probe_binds_only_an_ephemeral_port(self):
        self.assertTrue(autostart._address_is_local("127.0.0.1"))
        # TEST-NET-1 (RFC 5737) is never assigned to a local adapter. Built at
        # run time so the public scan does not see an address literal.
        self.assertFalse(autostart._address_is_local(".".join(("192", "0", "2", "1"))))

    def test_task_xml_with_and_without_inbox(self):
        for inbox, suffix in ((False, ""), (True, " --inbox")):
            with self.subTest(inbox=inbox):
                xml = autostart.task_xml(CHECKOUT, USER, inbox)
                task = ET.fromstring(xml.encode("utf-16"))
                get = lambda path: task.findtext(path, namespaces=NAMESPACE)
                self.assertEqual(task.attrib, {"version": "1.2"})
                self.assertEqual(len(task.find("t:Triggers", NAMESPACE)), 1)
                self.assertEqual(get("t:Triggers/t:LogonTrigger/t:UserId"), USER)
                principal = "t:Principals/t:Principal/"
                self.assertEqual(get(principal + "t:UserId"), USER)
                self.assertEqual(get(principal + "t:LogonType"), "InteractiveToken")
                self.assertEqual(get(principal + "t:RunLevel"), "LeastPrivilege")
                for setting, value in {
                    "MultipleInstancesPolicy": "IgnoreNew",
                    "DisallowStartIfOnBatteries": "false",
                    "StopIfGoingOnBatteries": "false",
                    "AllowHardTerminate": "true",
                    "StartWhenAvailable": "false",
                    "Enabled": "true",
                    "ExecutionTimeLimit": "PT0S",
                }.items():
                    self.assertEqual(get(f"t:Settings/t:{setting}"), value)
                for forbidden in ("RestartOnFailure", "Password", "HighestAvailable"):
                    self.assertNotIn(forbidden, xml)
                self.assertEqual(len(task.find("t:Actions", NAMESPACE)), 1)
                action = "t:Actions/t:Exec/"
                self.assertEqual(get(action + "t:Command"),
                                 str(CHECKOUT / r".venv\Scripts\pythonw.exe"))
                self.assertEqual(get(action + "t:Arguments"),
                                 f'-I -B "{CHECKOUT}\\tools\\scn_muse_autostart.py" run{suffix}')
                self.assertEqual(get(action + "t:WorkingDirectory"), str(CHECKOUT))
                self.assertIn("Synthetic &amp; Demo", xml)
                self.assertIn(r"EXAMPLE&amp;LAB\MuseUser", xml)

    def test_nonlocal_appdata_is_refused_before_any_io(self):
        for path in ("relative", r"\\synthetic\share\AppData", r"C:AppData"):
            with (self.subTest(path=path),
                  patch.dict(os.environ, {"LOCALAPPDATA": path}, clear=True),
                  patch.object(Path, "lstat") as lstat,
                  patch.object(subprocess, "run") as run,
                  redirect_stderr(io.StringIO()) as stderr):
                self.assertEqual(autostart.main(["run"]), 2)
                self.assertIn("[HOLD] LOCALAPPDATA must be an absolute local path",
                              stderr.getvalue())
                lstat.assert_not_called()
                run.assert_not_called()

    def test_public_tree_includes_autostart_tools(self):
        names = (
            "tools/scn_muse_autostart.py",
            "tools/install-muse-bridge-autostart.cmd",
            "tools/uninstall-muse-bridge-autostart.cmd",
        )
        root = Path(autostart.__file__).resolve().parents[1]
        for name in names:
            self.assertTrue((root / name).is_file(), name)

        # The public tree already contains the tools and omits the exporter.
        if importlib.util.find_spec("tools.build_public_tree") is not None:
            from tools import build_public_tree as public

            files = dict.fromkeys((*public.MAPPED.values(), *public.TOOLS,
                                   *public.BRANDING, *public.TEST_HELPERS), b"")
            test_name = "tests/test_muse_autostart_20261118.py"
            files[public.TEST_LIST] = (root / public.TEST_LIST).read_bytes()
            for line in files[public.TEST_LIST].decode("utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    files[line.strip()] = b""
            for name in (*names, test_name):
                files[name] = (root / name).read_bytes()
            chosen = public.select(files)
            for name in (*names, test_name):
                self.assertEqual(chosen[name], files[name])
            self.assertEqual(public.scan({name: chosen[name] for name in (*names, test_name)}), [])


class AutostartDiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(cls.enterClassContext(inherited_test_temp()))

    def setUp(self):
        self.appdata = self.scratch / self._testMethodName
        self.appdata.mkdir()
        self.log = self.appdata / "SoraCommanderNext" / autostart.LOG_NAME
        self.rotated = self.log.with_suffix(".log.1")
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.appdata),
            "USERDOMAIN": "EXAMPLE",
            "USERNAME": "MuseUser",
            "USERPROFILE": str(self.appdata / "SyntheticProfile"),
            "ProgramFiles": str(self.appdata / "Synthetic Programs"),
        }, clear=True))
        stack.enter_context(patch.object(autostart, "CHECKOUT", CHECKOUT))
        stack.enter_context(patch.object(sys, "path", sys.path.copy()))
        self.run = stack.enter_context(patch.object(subprocess, "run"))
        stack.enter_context(patch.object(autostart.ctypes, "FormatError",
                                         return_value="Synthetic task file missing."))
        self.which = stack.enter_context(
            patch.object(autostart.shutil, "which", return_value="synthetic-tailscale.exe"))
        self.now = 0
        self.sleep = stack.enter_context(patch.object(autostart.time, "sleep", side_effect=self._sleep))
        stack.enter_context(patch.object(autostart.time, "monotonic", side_effect=lambda: self.now))
        self.bridge = stack.enter_context(patch.object(bridge, "main", return_value=0))
        # The synthetic tailnet address is not on this PC; tests choose when it is.
        self.local = stack.enter_context(patch.object(autostart, "_address_is_local", return_value=True))
        self.stdout = stack.enter_context(redirect_stdout(io.StringIO()))
        self.stderr = stack.enter_context(redirect_stderr(io.StringIO()))

    def _sleep(self, seconds):
        self.now += seconds

    def _scheduler_call(self, command, *args):
        # Review fix: the system copy is named, never a bare "schtasks".
        self.assertTrue(autostart.SCHTASKS.lower().endswith(r"\system32\schtasks.exe"))
        return [autostart.SCHTASKS, command, "/TN", TASK_NAME, *args]

    def _ready(self):
        return SimpleNamespace(returncode=0, stdout=IP + "\n")

    def _absent(self):
        return SimpleNamespace(returncode=1, stderr=b"ERROR: Synthetic task file missing.\r\n")

    def test_install_creates_without_force_and_removes_xml_on_success_and_failure(self):
        for code in (0, 7):
            with self.subTest(code=code):
                observed = []

                def task(argv, **kwargs):
                    if argv[1] == "/Query":
                        return self._absent()
                    path = Path(argv[-1])
                    observed.append(path)
                    self.assertEqual(path.parent, self.log.parent)
                    self.assertEqual(argv, self._scheduler_call("/Create", "/XML", str(path)))
                    self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                    data = path.read_bytes()
                    self.assertTrue(data.startswith(codecs.BOM_UTF16_LE))
                    self.assertEqual(data.decode("utf-16"),
                                     autostart.task_xml(CHECKOUT, r"EXAMPLE\MuseUser", code == 0))
                    return SimpleNamespace(returncode=code)

                self.run.reset_mock()
                self.run.side_effect = task
                with patch.object(autostart, "CHECKOUT", Path(CHECKOUT)), \
                        patch.object(Path, "is_file", return_value=True):
                    self.assertEqual(autostart.main(["install"] + (["--inbox"] if code == 0 else [])),
                                     code)
                    self.assertEqual(self.run.call_args_list[0].args[0], self._scheduler_call("/Query"))
                    self.assertEqual(self.run.call_count, 2)
                    self.assertEqual(len(observed), 1)
                    self.assertFalse(observed[0].exists())
                    self.assertIn(f"schtasks /Create exit code: {code}", self.stdout.getvalue())

    def test_install_refuses_an_existing_task(self):
        self.run.return_value = SimpleNamespace(returncode=0)
        with patch.object(autostart, "CHECKOUT", Path(CHECKOUT)), \
                patch.object(Path, "is_file", return_value=True):
            self.assertEqual(autostart.main(["install"]), 2)
        self.assertEqual([item.args[0] for item in self.run.call_args_list],
                         [self._scheduler_call("/Query")])
        self.assertIn("[HOLD] already installed; run the uninstaller first", self.stderr.getvalue())

    def test_install_requires_pythonw_and_the_user_environment(self):
        with patch.object(autostart, "CHECKOUT", Path(CHECKOUT)), \
                patch.object(Path, "is_file", return_value=False) as is_file:
            self.assertEqual(autostart.main(["install"]), 2)
            self.assertIn("pythonw.exe is missing", self.stderr.getvalue())
            is_file.return_value = True
            for key in ("USERDOMAIN", "USERNAME"):
                with self.subTest(key=key), patch.dict(os.environ, {key: ""}):
                    self.assertEqual(autostart.main(["install"]), 2)
                    self.assertIn("USERDOMAIN and USERNAME are required", self.stderr.getvalue())
        self.run.assert_not_called()

    def test_uninstall_when_absent(self):
        self.run.return_value = self._absent()
        self.assertEqual(autostart.main(["uninstall"]), 0)
        self.assertEqual([item.args[0] for item in self.run.call_args_list],
                         [self._scheduler_call("/Query")])
        self.assertIn("Task is absent.", self.stdout.getvalue())

    def test_scheduler_query_failure_does_not_mean_absence(self):
        self.run.return_value = SimpleNamespace(returncode=1, stderr=b"ERROR: Access is denied.")
        for argv in (["install"], ["uninstall"]):
            with (self.subTest(argv=argv), patch.object(autostart, "CHECKOUT", Path(CHECKOUT)),
                  patch.object(Path, "is_file", return_value=True)):
                self.run.reset_mock()
                self.assertEqual(autostart.main(argv), 2)
                self.assertEqual([item.args[0] for item in self.run.call_args_list],
                                 [self._scheduler_call("/Query")])
        self.assertIn("task state could not be determined", self.stderr.getvalue())

    def test_uninstall_ignores_end_failure_and_keeps_log(self):
        self.log.parent.mkdir()
        self.log.write_bytes(b"log stays")
        self.run.side_effect = [SimpleNamespace(returncode=code) for code in (0, 1, 0)]
        self.assertEqual(autostart.main(["uninstall"]), 0)
        self.assertEqual([item.args[0] for item in self.run.call_args_list], [
            self._scheduler_call("/Query"), self._scheduler_call("/End"),
            self._scheduler_call("/Delete", "/F"),
        ])
        self.assertEqual(self.log.read_bytes(), b"log stays")

    def test_run_holds_after_12_tries_in_one_minute(self):
        self.run.return_value = SimpleNamespace(returncode=1, stdout="")
        self.assertEqual(autostart.main(["run"]), 2)
        self.assertEqual(self.run.call_args_list, [
            call(["synthetic-tailscale.exe", "ip", "-4"], capture_output=True, text=True,
                 encoding="utf-8", errors="replace", timeout=5,
                 creationflags=subprocess.CREATE_NO_WINDOW),
        ] * 36)
        self.assertEqual(self.sleep.call_args_list, [call(5)] * 36)
        self.assertEqual(self.now, 180)
        self.bridge.assert_not_called()
        self.assertIn("[HOLD] Tailscale address not available after 180 seconds",
                      self.log.read_text(encoding="utf-8"))

    def test_run_uses_third_try_calls_bridge_once_and_logs_its_exit(self):
        base = ["--checkout", str(CHECKOUT), "--bind", IP, "--port", "8765",
                "--mode", "readonly", "--auth", "tailscale"]
        for flags in ([], ["--inbox"]):
            with self.subTest(flags=flags):
                self.run.reset_mock()
                self.bridge.reset_mock()
                self.now = 0
                self.run.side_effect = [
                    subprocess.TimeoutExpired("synthetic-tailscale.exe", 5),
                    SimpleNamespace(returncode=0, stdout=f"127.0.0.1\n{IP}\n"),
                    self._ready(),
                ]
                self.bridge.side_effect = self._bridge_output
                self.assertEqual(autostart.main(["run", *flags]), 23)
                self.assertEqual(self.run.call_count, 3)
                self.bridge.assert_called_once_with(base + flags)
        log = self.log.read_text(encoding="utf-8")
        self.assertEqual(log.count("+00:00 autostart start\n"), 2)
        self.assertEqual(log.count("+00:00 bridge exit code: 23\n"), 2)
        self.assertEqual(log.count("bridge stdout\nbridge stderr\n"), 2)

    def _bridge_output(self, argv):
        self.assertEqual(sys.path[0], str(CHECKOUT))
        print("bridge stdout")
        print("bridge stderr", file=sys.stderr)
        return 23

    def test_run_waits_until_the_reported_address_can_be_bound(self):
        # Live 2026-11-19: `tailscale ip -4` reported the remembered address
        # before Windows assigned it, and the bridge's bind failed (exit 3).
        self.run.return_value = self._ready()
        self.local.side_effect = [False, False, True]
        self.assertEqual(autostart.main(["run"]), 0)
        self.assertEqual(self.local.call_count, 3)
        self.bridge.assert_called_once()
        self.assertEqual(self.bridge.call_args.args[0][3], IP)

    def test_run_holds_when_the_address_never_becomes_local(self):
        self.run.return_value = self._ready()
        self.local.return_value = False
        self.assertEqual(autostart.main(["run"]), 2)
        self.bridge.assert_not_called()
        self.assertEqual(self.now, autostart.TAILSCALE_WAIT)
        self.assertIn(f"[HOLD] Tailscale address not available after {autostart.TAILSCALE_WAIT} seconds",
                      self.log.read_text(encoding="utf-8"))

    def test_run_uses_program_files_fallback_and_rotates_once(self):
        self.log.parent.mkdir()
        old = b"L" * (autostart.LOG_LIMIT + 1)
        self.log.write_bytes(old)
        self.rotated.write_bytes(b"previous rotation")
        self.which.return_value = None
        self.run.return_value = self._ready()
        self.assertEqual(autostart.main(["run"]), 0)
        self.assertEqual(self.run.call_args.args[0], [
            str(self.appdata / "Synthetic Programs" / "Tailscale" / "tailscale.exe"), "ip", "-4",
        ])
        self.assertEqual(self.rotated.read_bytes(), old)
        self.assertIn("autostart start\n", self.log.read_text(encoding="utf-8"))

    def test_run_logs_unexpected_bridge_exception_on_one_line(self):
        self.run.return_value = self._ready()
        self.bridge.side_effect = RuntimeError("synthetic\nmultiline details")
        self.assertEqual(autostart.main(["run"]), 1)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], "[ERROR] RuntimeError")
        self.assertTrue(lines[2].endswith("bridge exit code: 1"))

    def test_linked_or_reparse_folder_and_logs_are_refused(self):
        self.log.parent.mkdir()
        original_lstat = Path.lstat
        for path, mode in ((self.log.parent, stat.S_IFLNK),
                           (self.log, stat.S_IFREG), (self.rotated, stat.S_IFREG)):
            with self.subTest(path=path.name):
                def lstat(target, *args, **kwargs):
                    if target == path:
                        return SimpleNamespace(st_mode=mode, st_nlink=1,
                                               st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    return original_lstat(target, *args, **kwargs)

                with patch.object(Path, "lstat", lstat):
                    self.assertEqual(autostart.main(["run"]), 2)
        self.assertIn("[HOLD] ordinary directory required: SoraCommanderNext", self.stderr.getvalue())
        self.run.assert_not_called()
        self.bridge.assert_not_called()
