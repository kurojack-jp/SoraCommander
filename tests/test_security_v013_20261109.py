"""Focused v0.1.3 synthetic tests. No real windows, commands or protected data."""
import ast
from contextlib import ExitStack
import os
from pathlib import Path
import sys
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests._inherited_test_temp import inherited_test_temp
from tests.test_owner_broker_dev import call
from sora_commander_next import owner_approval_broker as active
from sora_commander_next import owner_localized_broker_dev_20261104 as localized
from sora_commander_next import owner_file_review_gate_dev_20261105 as file_gate
from sora_commander_next import approval_v2_owner_local as v2
from sora_commander_next.broker_host import BrokerHost
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.owner_broker_dev import OwnerBrokerBackend, DevBrokerHost
from sora_commander_next.owner_command_caution import command_caution
from sora_commander_next.owner_dialog_integrity import (
    DIALOG_INTEGRITY, DIALOG_NAMES, DialogIntegrity, sibling_names)
from sora_commander_next.policy import Policy
from sora_commander_next.stdio_main import owner_journal_path
from sora_commander_next.source_review import SourceReviewWorkflow

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "sora_commander_next"


class InstallWriteTests(unittest.TestCase):
    def setUp(self):
        self.host = Host(Policy((str(ROOT),)))
        self.addCleanup(self.host.close)
        self.backend = OwnerBrokerBackend(self.host)

    def targets(self):
        for target in (PACKAGE / "owner_review_renderer.py", ROOT / "tools" / "new.py",
                       ROOT / "pyproject.toml"):
            yield str(target)
            yield str(target).swapcase()

    def assert_rejected(self, result):
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["outcome"], "NOT_STARTED")
        self.assertIs(result["retry_allowed"], False)

    def test_each_write_tool_and_both_move_paths_refuse_before_mutation(self):
        server = build_server(self.host)
        client = DevBrokerHost(Mock(), read_roots=(str(ROOT),))
        self.addCleanup(client.close)
        for target in self.targets():
            other = str(ROOT / "tests" / "safe.txt")
            cases = [("create_directory", {"path": target}),
                     ("create_file", {"path": target, "content": "new"}),
                     ("move_file", {"source": target, "destination": other}),
                     ("move_file", {"source": other, "destination": target}),
                     ("request_source_patch_dev", {"path": target, "old_text": "a",
                         "new_text": "b", "expected_sha256": "a" * 64})]
            for name, args in cases:
                with self.subTest(name=name, target=target), patch(
                        "sora_commander_next.dev_rdc_compat.mutate") as mutate:
                    self.assert_rejected(getattr(self.backend, name)(**args))
                    self.assert_rejected(call(server, name, args))
                    self.assert_rejected(client._call(name, **args))
                    mutate.assert_not_called()

    def test_source_patch_refuses_before_transport(self):
        host = BrokerHost(Mock(), read_roots=(str(ROOT),))
        self.addCleanup(host.close)
        for target in self.targets():
            self.assert_rejected(host.request_source_patch(
                target, "a", "b", "a" * 64, "b" * 64, "1" * 32))
        host._transport.request.assert_not_called()

    def test_source_workflow_refuses_before_snapshot_or_owner(self):
        workflow = object.__new__(SourceReviewWorkflow)
        workflow._lock = RLock()
        workflow._records = {}
        workflow._halted = False
        workflow._policy = Mock()
        workflow._store = Mock()
        workflow._owner = Mock()
        for target in self.targets():
            self.assert_rejected(workflow.submit("request_source_patch", {
                "path": target, "old_text": "a", "new_text": "b",
                "expected_sha256": "a" * 64, "expected_new_sha256": "b" * 64,
                "request_id": "1" * 32}))
        workflow._store.snapshot.assert_not_called()
        workflow._owner.verify.assert_not_called()

    def test_other_allowed_root_works_and_checkout_cwd_stays_allowed(self):
        self.host.policy.require_full_path(str(ROOT))
        with inherited_test_temp() as folder:
            host = Host(Policy((str(folder),)))
            self.addCleanup(host.close)
            target = str(Path(folder) / "ordinary.txt")
            host.policy.require_write_path(target)
            with patch("sora_commander_next.dev_rdc_compat.mutate", return_value={"status": "OK"}) as mutate:
                self.assertEqual(OwnerBrokerBackend(host).create_file(path=target, content="synthetic"),
                                 {"status": "OK"})
                mutate.assert_called_once()


class DialogPinTests(unittest.TestCase):
    def test_all_dialog_constants_and_sibling_loads_are_pinned(self):
        self.assertIsNotNone(DIALOG_INTEGRITY.hashes)
        pins = set(DIALOG_INTEGRITY.hashes)
        self.assertLessEqual(DIALOG_NAMES, pins)
        for source in PACKAGE.glob("*.py"):
            tree = ast.parse(source.read_bytes())
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id in {"DIALOG_SCRIPT", "_DIALOG"}
                        for t in node.targets):
                    self.assertLessEqual(sibling_names(ast.unparse(node)), pins)
                if isinstance(node, ast.Call) and (
                        isinstance(node.func, ast.Name) and node.func.id == "_load_sibling"
                        or isinstance(node.func, ast.Attribute) and node.func.attr == "_load_sibling"):
                    self.assertIn(node.args[0].value, pins)
        for name in pins:
            self.assertLessEqual(sibling_names((PACKAGE / name).read_bytes()), pins)

    def test_changed_missing_and_unreadable_files_fail_closed(self):
        with inherited_test_temp() as folder:
            folder = Path(folder)
            for name in DIALOG_NAMES:
                (folder / name).write_text("# synthetic dialog\n", encoding="utf-8")
            pin = DialogIntegrity(folder)
            pin.verify()
            name = next(iter(DIALOG_NAMES))
            (folder / name).write_text("# changed\n", encoding="utf-8")
            with self.assertRaises(PermissionError):
                pin.verify()
            with patch.object(Path, "read_bytes", side_effect=OSError("synthetic read error")):
                with self.assertRaises(PermissionError):
                    pin.verify()
                self.assertIsNone(DialogIntegrity(folder).hashes)

    def test_hash_mismatch_blocks_every_launcher_without_spawn(self):
        request = Command(sys.executable, ("--version",), str(ROOT))
        controller = Mock()
        controller.host.policy.lock = RLock()
        controller.host.approval.clock.return_value = 0
        controller.pending_local.return_value = [SimpleNamespace(
            pending_id="pending", expires_at=40, request=request)]
        bad_pin = DialogIntegrity(PACKAGE)
        self.assertIsNotNone(bad_pin.hashes)
        with ExitStack() as stack:
            verification = stack.enter_context(patch.object(bad_pin, "verify", wraps=bad_pin.verify))
            stack.enter_context(patch.object(Path, "read_bytes", return_value=b"# changed"))
            launch = stack.enter_context(patch("subprocess.Popen"))
            for module in (active, localized, file_gate, v2):
                stack.enter_context(patch.object(module, "DIALOG_INTEGRITY", bad_pin))
            stack.enter_context(patch.object(active, "OwnerSessionEventLatch"))
            stack.enter_context(patch.object(localized, "OwnerSessionEventLatch"))
            stack.enter_context(patch.object(file_gate, "OwnerSessionEventLatch"))
            stack.enter_context(patch("sora_commander_next.windows_session_events.OwnerSessionEventLatch"))
            for cls in (active.OwnerApprovalBroker, localized.LocalizedOwnerApprovalBrokerDev):
                broker = object.__new__(cls)
                broker._controller = controller
                if cls is localized.LocalizedOwnerApprovalBrokerDev:
                    broker._locale_source = lambda: "ja"
                result = broker.review_once("pending")
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["launch_outcome"], "NOT_STARTED")
            proposal = {"operation": "create_directory", "path": str(ROOT / "tests" / "unused")}
            self.assertEqual(file_gate.review_once(Mock(), proposal), "ERROR")
            pilot = object.__new__(v2.LocalOwnerV2Pilot)
            pilot._guard = Mock(sid="synthetic-owner")
            self.assertEqual(pilot.review_once_local(request).status, "ERROR")
            self.assertEqual(verification.call_count, 4)
            launch.assert_not_called()


class BatchAndCautionTests(unittest.TestCase):
    def test_all_batch_metacharacters_rejected_before_review(self):
        host = Host(Policy((str(ROOT),)))
        self.addCleanup(host.close)
        dispatcher = host._owner_review_dispatcher = Mock()
        with patch("subprocess.Popen") as launch:
            for suffix in (".cmd", ".BAT"):
                for char in '&|<>^%!"\r\n\0':
                    with self.subTest(suffix=suffix, char=repr(char)):
                        result = host.run_command(executable=str(ROOT / ("npm" + suffix)),
                            argv=["run", "test" + char + "literal"], cwd=str(ROOT))
                        self.assertEqual(result["error"], "BatchArgumentMetacharacterRejected")
                        self.assertEqual(result["status"], "ERROR")
            dispatcher.offer_local.assert_not_called()
            launch.assert_not_called()

    def test_exe_arguments_unchanged_and_plain_batch_allowed(self):
        for char in '&|<>^%!"\r\n':
            args = ("test" + char + "literal",)
            self.assertEqual(Command(str(ROOT / "app.exe"), args, str(ROOT)).argv, args)
        for suffix in (".cmd", ".BAT"):
            self.assertEqual(Command(str(ROOT / ("npm" + suffix)), ("run", "test"), str(ROOT)).argv,
                             ("run", "test"))

    def test_batch_conhost_shell_and_python_push_cautions(self):
        for name, args, level in (
                ("npm.cmd", ["run", "test"], "ATTENTION"),
                ("script.BAT", [], "ATTENTION"),
                ("conhost.exe", ["cmd.exe", "/c", "echo hi"], "ATTENTION"),
                ("conhost.exe", ["powershell.exe", "-c", "git push"], "STRONG"),
                ("python.exe", ["-c", "import os; os.system('git push')"], "STRONG"),
                ("pythonw.exe", ["-c", "print('PUSH')"], "STRONG"),
                ("python.exe", ["script.py", "push"], "ATTENTION")):
            with self.subTest(name=name, args=args):
                self.assertEqual(command_caution(executable=name, argv=args).level, level)


class PowerShellPathTests(unittest.TestCase):
    def test_absolute_system_powershell_and_missing_systemroot(self):
        import winreg
        with inherited_test_temp() as folder, patch.object(winreg, "OpenKey"), patch.object(
                winreg, "QueryValueEx", return_value=("synthetic-modules", 1)), patch(
                "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="OWNER_ACL_OK")) as runner:
            with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
                owner_journal_path(storage_override=folder)
                self.assertEqual(runner.call_args.args[0][0],
                    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
            runner.reset_mock()
            with patch.dict(os.environ):
                os.environ.pop("SystemRoot", None)
                with self.assertRaisesRegex(RuntimeError, "SystemRoot unavailable"):
                    owner_journal_path(storage_override=folder)
                runner.assert_not_called()
