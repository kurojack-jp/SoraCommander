"""SCN16 T2: fake Windows admission; harmless isolated child compatibility.

No real Job Object, GUI, credentials, temporary directories or environment logs.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from sora_commander_next import jobs
from sora_commander_next.commands import Command
from tests.test_owner_job_launch_outcome_20261027 import (
    FakeChild, FakeThread, FakeWindowsJob,
)

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE = {
    "CONTROL_PLANE_API_KEY": "SCN16_SYNTHETIC_ONLY",
    "AWS_SECRET_ACCESS_KEY": "SCN16_SYNTHETIC_ONLY",
    "OPENAI_API_KEY": "SCN16_SYNTHETIC_ONLY",
    "GIT_SSH_COMMAND": "SCN16_SYNTHETIC_ONLY",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "synthetic.key",
    "GIT_CONFIG_VALUE_0": "SCN16_SYNTHETIC_ONLY",
    "PYTHONPATH": "SCN16_SYNTHETIC_ONLY",
    "PYTHONHOME": "SCN16_SYNTHETIC_ONLY",
    "SSH_AUTH_SOCK": "SCN16_SYNTHETIC_ONLY",
}


@unittest.skipUnless(os.name == "nt", "Windows launch contract")
class JobEnvironmentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manager = jobs.JobManager()
        self.child = FakeChild()
        self.addCleanup(self.child.close_pipes)
        self.addCleanup(self.manager.close)
        self.request = Command(sys.executable, ("-I", "-S", "-c", "pass"),
                               str(ROOT), timeout=5, output_limit=4096)
        self.tree = FakeWindowsJob()
        self.tree_factory = self.enterContext(patch.object(
            jobs, "WindowsJob", return_value=self.tree))
        self.spawn = self.enterContext(patch.object(
            jobs.subprocess, "Popen", return_value=self.child))
        self.enterContext(patch.object(jobs, "Thread", FakeThread))

    def test_popen_gets_fresh_filtered_environment_and_suspended_job(self):
        parent = {"SystemRoot": r"C:\Windows", "Path": r"C:\synthetic-bin",
                  "TEMP": r"C:\synthetic-temp", "HOME": r"C:\synthetic-home",
                  **SENSITIVE}
        order = []
        self.spawn.side_effect = lambda *a, **k: (order.append("spawn") or self.child)
        def resume(child):
            self.assertIs(child, self.child)
            self.assertEqual(len(self.manager._jobs), 1)
            self.assertFalse(next(iter(self.manager._jobs.values())).thread.started)
            order.append("resume")
        self.tree.attach_and_resume = resume
        with patch.dict(jobs.os.environ, parent, clear=True):
            before = dict(jobs.os.environ)
            result = self.manager.start(self.request)
            self.assertTrue(dict(jobs.os.environ) == before, "parent changed")
        self.assertEqual(result["status"], "RUNNING")
        self.tree_factory.assert_called_once_with()
        args, kwargs = self.spawn.call_args
        self.assertEqual(args, ([self.request.executable, *self.request.argv],))
        self.assertEqual(kwargs["cwd"], str(ROOT))
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["creationflags"], 0x00000004)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        # Windows os.environ normalizes keys to uppercase on insertion.
        self.assertEqual(kwargs["env"], {k: v for k, v in before.items()
                                        if k not in SENSITIVE})
        self.assertEqual(order, ["spawn", "resume"])
        self.assertTrue(next(iter(self.manager._jobs.values())).thread.started)
        # Same request reuses the original session without a second OS launch.
        self.assertTrue(self.manager.start(self.request)["reused_session"])
        self.spawn.assert_called_once()

    def test_windows_job_failure_never_spawns(self):
        self.tree_factory.side_effect = RuntimeError("synthetic admission failure")
        with self.assertRaises(jobs.LaunchNotStarted):
            self.manager.start(self.request)
        self.spawn.assert_not_called()

    def test_protected_manager_rejects_interactive_before_job_or_spawn(self):
        self.manager.protected_stage = True
        request = Command(sys.executable, ("-I", "-S", "-q", "-i"),
                          str(ROOT), timeout=5, interactive=True)
        with self.assertRaises(PermissionError):
            self.manager.start(request)
        self.tree_factory.assert_not_called()
        self.spawn.assert_not_called()


class EnvironmentCompatibilityTests(unittest.TestCase):
    def test_invalid_environment_values_are_not_forwarded(self):
        self.assertEqual(jobs._child_environment(
            {"PATH": None, "TEMP": 7, 5: "invalid", "SystemRoot": "synthetic"}),
            {"SystemRoot": "synthetic"})
        with self.assertRaises(TypeError):
            jobs._child_environment([])

    def filtered_environment(self):
        # Read only allowlisted runtime values; inject dummy secret variables
        # into a private mapping, never into the actual parent environment.
        parent = {**jobs._child_environment(), **SENSITIVE}
        filtered = jobs._child_environment(parent)
        self.assertTrue(all(k not in filtered for k in SENSITIVE),
                        "synthetic sensitive variable survived")
        return filtered

    def test_real_isolated_python_checks_absence_without_printing_values(self):
        code = ("import os; keys=" + repr(tuple(SENSITIVE)) +
                "; raise SystemExit(0 if all(k not in os.environ for k in keys) else 8)")
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code],
                                cwd=ROOT, env=self.filtered_environment(),
                                capture_output=True, timeout=12)
        self.assertTrue(result.returncode == 0, "isolated Python child failed")
        self.assertTrue(result.stdout == result.stderr == b"", "unexpected child output")

    def test_real_git_version_and_readonly_repository_detection(self):
        git = shutil.which("git")
        self.assertIsNotNone(git, "Git required for this compatibility check")
        env = self.filtered_environment()
        for argv, expected in ((["--version"], b"git version "),
                               (["rev-parse", "--is-inside-work-tree"], b"true")):
            with self.subTest(command=argv[0]):
                result = subprocess.run([git, *argv], cwd=ROOT, env=env,
                                        capture_output=True, timeout=12)
                self.assertTrue(result.returncode == 0, "Git compatibility check failed")
                self.assertTrue(result.stdout.startswith(expected), "unexpected Git output")
                self.assertTrue(result.stderr == b"", "unexpected Git diagnostic")
