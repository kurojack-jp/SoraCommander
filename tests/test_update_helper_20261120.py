"""Synthetic update checks with no filesystem fixtures or child processes."""

from contextlib import nullcontext, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sora_commander_next import owner_journal_lease
from tools import scn_update


class WindowsQueryTests(unittest.TestCase):
    def test_com_task_query_connects_to_the_named_root_task(self):
        client = Mock()
        service = client.Dispatch.return_value
        service.GetFolder.return_value.GetTask.return_value.State = 4
        with patch.dict(scn_update.sys.modules, {"win32com.client": client}):
            self.assertEqual(scn_update._task_state(scn_update.BROKER_TASK), 4)
        client.Dispatch.assert_called_once_with("Schedule.Service")
        service.Connect.assert_called_once_with()
        service.GetFolder.assert_called_once_with("\\")
        service.GetFolder.return_value.GetTask.assert_called_once_with(scn_update.BROKER_TASK)

    def test_wmi_query_reads_the_four_process_properties(self):
        client = Mock()
        fields = dict(Name="python.exe", ProcessId=17, ExecutablePath=None, CommandLine=None)
        row = SimpleNamespace(Properties_=lambda key: SimpleNamespace(Value=fields[key]))
        query = client.GetObject.return_value.ExecQuery
        query.return_value = [row]
        with patch.dict(scn_update.sys.modules, {"win32com.client": client}):
            self.assertEqual(scn_update._process_rows(), [fields])
        client.GetObject.assert_called_once_with(r"winmgmts:\\.\root\cimv2")
        query.assert_called_once_with("SELECT Name, ProcessId, ExecutablePath, CommandLine FROM Win32_Process")


class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "USERPROFILE": "synthetic-profile", "LOCALAPPDATA": "synthetic-appdata",
        }, clear=True))
        self.root = Path("checkout")
        self.output = io.StringIO()
        self.commands = []
        self.old = "a" * 40
        self.new = "b" * 40
        self.replies = {
            ("rev-parse", "--show-prefix"): "",
            ("rev-parse", "--is-inside-work-tree"): "true",
            ("branch", "--show-current"): "main",
            ("rev-parse", "--symbolic-full-name", "@{upstream}"): "refs/remotes/origin/main",
            ("remote", "get-url", "--all", "origin"): scn_update.OFFICIAL + ".git",
            ("config", "--get-all", "remote.origin.fetch"):
                "+refs/heads/*:refs/remotes/origin/*",
            ("config", "--name-only", "--get-regexp", scn_update.EXTERNAL_DRIVER): (1, ""),
            ("ls-files", "-v"): "H tracked.py",
            ("status", "--porcelain=v1", "-uall", "--ignore-submodules=none"): "",
            ("fetch", "--tags", "--no-auto-maintenance", "origin"): "",
            ("describe", "--tags", "--abbrev=0", "HEAD"): "v1.2.0",
            ("tag", "--merged", "refs/remotes/origin/main",
             "--format=%(objecttype) %(refname:strip=2)"):
                "tag v1.2.0\ntag v1.11.0\ntag v1.9.0\ncommit v99.0.0\ntag latest",
            ("rev-parse", "HEAD"): self.old,
            ("rev-parse", "refs/remotes/origin/main"): self.new,
            ("merge-base", "--is-ancestor", self.old, self.new): "",
            ("tag", "--merged", "refs/remotes/origin/main", "--no-merged", "HEAD"):
                "v1.9.0\nv1.11.0\nv99.0.0",
            ("merge", "--ff-only", "--no-overwrite-ignore", "origin/main"): "",
        }
        self.processes = [{
            "Name": "explorer.exe", "ProcessId": 17,
            "ExecutablePath": None, "CommandLine": None,
        }]
        self.setup_result = 0
        self.tasks = dict.fromkeys(scn_update.LOGON_TASKS, "Ready")
        self.process_query = self.enterContext(patch.object(
            scn_update, "_process_rows", side_effect=lambda: self.processes))
        self.numeric_state = self.enterContext(patch.object(scn_update, "_task_state"))
        self.lease = self.enterContext(patch.object(
            owner_journal_lease, "owner_journal_lease", side_effect=nullcontext))
        self.enterContext(patch.object(scn_update.subprocess, "run", side_effect=self.fake_run))

    def fake_run(self, argv, **kwargs):
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        if argv[0] == "git":
            args = tuple(argv[1 + len(scn_update.GIT_OPTIONS):])
            self.commands.append(args)
            reply = self.replies[args]
            status, output = reply if isinstance(reply, tuple) else (0, reply)
            if args[0] == "fetch":
                self.assertIn("credential.helper=", argv)
                self.assertIn("http.followRedirects=false", argv)
                self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(kwargs["env"]["GIT_ASKPASS"], "")
                self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        elif argv[0].endswith("schtasks.exe"):
            name = argv[3]
            self.assertEqual(argv[1:], ["/Query", "/TN", name, "/FO", "CSV", "/NH"])
            self.commands.append(("task", name))
            if self.tasks[name] is None:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: Synthetic missing.")
            status, output = 0, f'"\\{name}","N/A","{self.tasks[name]}"'
        elif argv[0] == str(self.root / ".venv" / "Scripts" / "python.exe"):
            self.assertEqual(argv[1:4], ["-I", "-B", str(self.root / "tools" / "scn_update.py")])
            self.assertEqual(kwargs["timeout"], 30)
            if argv[4] == "--query-processes":
                self.commands.append(("processes",))
            with patch.object(scn_update, "ROOT", self.root), redirect_stdout(io.StringIO()) as pipe:
                status = scn_update._probe_main(argv[4:])
            output = pipe.getvalue()
        else:
            self.assertEqual(argv, [scn_update.sys.executable, "-I", "-B",
                                    "-c", scn_update.PACKAGE_CHECK])
            self.commands.append(("setup",))
            status, output = self.setup_result, ""
        return subprocess.CompletedProcess(argv, status, output, "")

    def invoke(self, *args):
        with redirect_stdout(self.output):
            return scn_update.main(args, root=self.root)

    def mutations(self):
        return [command[0] for command in self.commands
                if command[0] in ("fetch", "merge", "setup")]

    def test_dirty_tree_stops_at_status(self):
        status = ("status", "--porcelain=v1", "-uall", "--ignore-submodules=none")
        self.replies[status] = " M tracked.py\n?? untracked.txt"
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.commands[-1], status)
        self.assertIn("tracked.py", self.output.getvalue())
        self.assertIn("untracked.txt", self.output.getvalue())

    def test_wrong_branch_stops_before_remote(self):
        self.replies[("branch", "--show-current")] = "dev"
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.commands[-1], ("branch", "--show-current"))

    def test_wrong_remote_stops_before_status(self):
        self.replies[("remote", "get-url", "--all", "origin")] = "https://example.invalid/repo"
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.commands[-1], ("remote", "get-url", "--all", "origin"))

    def test_running_process_holds_without_exposing_arguments_or_stopping_it(self):
        self.processes.append({
            "Name": "pythonw.exe", "ProcessId": 25,
            "ExecutablePath": r"checkout\.venv\Scripts\pythonw.exe",
            "CommandLine": r"pythonw checkout\tools\scn_muse_autostart.py private-argument",
        })
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.commands[-1], ("processes",))
        self.assertIn("pythonw.exe (PID 25)", self.output.getvalue())
        for command in scn_update.STOP_TASKS:
            self.assertIn(command, self.output.getvalue())
        self.assertNotIn("private-argument", self.output.getvalue())

    def test_malformed_process_listing_holds(self):
        self.processes = {"Name": "python.exe"}
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.commands[-1], ("processes",))

    def test_process_query_failure_or_empty_snapshot_holds_before_fetch(self):
        for failure in (PermissionError("synthetic failure"), []):
            with self.subTest(failure=type(failure).__name__):
                self.process_query.side_effect = None
                if isinstance(failure, Exception):
                    self.process_query.side_effect = failure
                else:
                    self.process_query.return_value = failure
                self.assertEqual(self.invoke(), 2)
                self.assertEqual(self.mutations(), [])
                self.assertIn("Could not", self.output.getvalue())

    def test_process_query_ignores_only_its_own_processes(self):
        self.processes = [dict(self.processes[0], Name="tunnel-client.exe", ProcessId=pid)
                          for pid in (13, 17, 21, 25)]
        with (patch.object(os, "getpid", return_value=17),
              patch.object(os, "getppid", return_value=21),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(scn_update._probe_main(["--query-processes", "13"]), 0)
        self.assertEqual(output.getvalue().strip(), '["tunnel-client.exe (PID 25)"]')

    def test_localized_numeric_task_states_and_query_error(self):
        self.tasks[scn_update.MUSE_TASK] = "localized"
        for state in (1, 3, 0, 2, 4, None, True, "3", PermissionError("synthetic failure")):
            with self.subTest(state=state):
                self.commands.clear()
                self.numeric_state.side_effect = state if isinstance(state, Exception) else None
                self.numeric_state.return_value = state
                idle = type(state) is int and state in (1, 3)
                self.assertEqual(self.invoke("--check"), 0 if idle else 2)
                self.assertEqual(self.mutations(), ["fetch"] if idle else [])
                self.numeric_state.assert_called_with(scn_update.MUSE_TASK)

    def test_only_confirmed_missing_task_is_idle(self):
        self.tasks[scn_update.BROKER_TASK] = None
        for message, expected in (("Synthetic missing.", 0), ("Access denied.", 2)):
            with self.subTest(message=message), patch.object(
                    scn_update.ctypes, "FormatError", return_value=message, create=True):
                self.commands.clear()
                self.assertEqual(self.invoke("--check"), expected)
                self.assertEqual(self.mutations(), ["fetch"] if expected == 0 else [])
        self.numeric_state.assert_not_called()

    def test_helper_has_no_shell_executable(self):
        source = Path(scn_update.__file__).read_text(encoding="utf-8").casefold()
        for name in ("powershell.exe", "pwsh.exe"):
            self.assertNotIn(name, source)

    def test_relative_entry_points_hold_without_merge(self):
        for entry in (r"tools\scn_muse_bridge.py", r"tools\scn_broker_autostart.py",
                      r"tools\scn_muse_autostart.py", "-m sora_commander_next.owner_broker_main",
                      "-m sora_commander_next.owner_broker_client_main"):
            with self.subTest(entry=entry):
                self.processes[-1] = {
                    "Name": "python.exe", "ProcessId": 25,
                    "ExecutablePath": r"elsewhere\python.exe", "CommandLine": f"python {entry}",
                }
                self.assertEqual(self.invoke(), 2)
                self.assertEqual(self.mutations(), [])

    def test_either_running_task_holds_without_merge(self):
        for name in scn_update.LOGON_TASKS:
            with self.subTest(task=name):
                self.tasks[name] = "Running"
                self.assertEqual(self.invoke(), 2)
                self.assertEqual(self.mutations(), [])
                self.assertIn(name, self.output.getvalue())
                self.tasks[name] = "Ready"
        self.lease.assert_not_called()

    def test_held_owner_journal_holds_without_merge(self):
        self.lease.side_effect = owner_journal_lease.OwnerJournalLeaseError("synthetic contention")
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.mutations(), [])
        self.assertIn("Owner journal is held", self.output.getvalue())

    def test_already_current(self):
        self.replies[("rev-parse", "refs/remotes/origin/main")] = self.old
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), ["fetch", "setup"])
        self.assertIn("Already current", self.output.getvalue())

    def test_already_current_keeps_dependency_hold_on_rerun(self):
        self.setup_result = 2
        self.assertEqual(self.invoke(), 2)
        self.replies[("rev-parse", "refs/remotes/origin/main")] = self.old
        self.commands.clear()
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.mutations(), ["fetch", "setup"])
        self.commands.clear()
        self.assertEqual(self.invoke("--check"), 0)
        self.assertEqual(self.mutations(), ["fetch"])

    def test_preview_lists_annotated_tags_in_version_order(self):
        self.assertEqual(self.invoke("--check"), 0)
        self.assertEqual(self.mutations(), ["fetch"])
        self.assertIn("v1.9.0, v1.11.0", self.output.getvalue())
        self.assertIn("/releases/tag/v1.11.0", self.output.getvalue())
        self.assertNotIn("v99.0.0", self.output.getvalue())

    def test_local_commits_hold_without_merge(self):
        self.replies[("merge-base", "--is-ancestor", self.old, self.new)] = (1, "")
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.mutations(), ["fetch"])
        self.assertIn("local commits on main; update by hand", self.output.getvalue())

    def test_failed_merge_holds_without_setup(self):
        self.replies[("merge", "--ff-only", "--no-overwrite-ignore", "origin/main")] = (1, "")
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.mutations(), ["fetch", "merge"])

    def test_update_runs_fetch_merge_and_setup_in_order(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), ["fetch", "merge", "setup"])
        self.assertIn("Refresh tools in ChatGPT and Muse", self.output.getvalue())

    def test_missing_packages_hold_after_merge(self):
        self.setup_result = 2
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.mutations(), ["fetch", "merge", "setup"])
        self.assertIn("Packages could not be verified offline", self.output.getvalue())

    def test_path_with_space_exits_six_before_any_command(self):
        self.root = Path("checkout with space")
        self.assertEqual(self.invoke(), 6)
        self.assertEqual(self.commands, [])

    def test_unsafe_path_exits_six_before_any_command(self):
        self.root = Path("checkout%")
        self.assertEqual(self.invoke(), 6)
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
