"""Synthetic DEV fixtures and explicit mocked local GUI; no live acceptance."""
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys
import shutil
from uuid import uuid4
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sora_commander_next import dev_rdc_compat as dev
from sora_commander_next import owner_file_review_gate_dev_20261105 as gate

REAL_REVIEW_ONCE = gate.review_once
from sora_commander_next import file_edits, file_reads
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.mcp_adapter import build_server, _validate_arguments


class DevRdcTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1]
        self.root = workspace / "tests" / ("rdc-step1-temp-" + uuid4().hex)
        self.root.mkdir()  # inherited permissions; no chmod/ACL changes
        self.root = self.root.resolve()
        def cleanup():
            if (self.root.parent != workspace / "tests"
                    or not self.root.name.startswith("rdc-step1-temp-")):
                raise RuntimeError("unexpected fixture cleanup target")
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        self.roots = (str(self.root),)
        self.source = self.root / "sample.py"
        self.source.write_bytes(b"x = 1\n")
        self.digest = sha256(self.source.read_bytes()).hexdigest()
        self.guard = Mock()
        self.owner = self.enterContext(patch.object(dev, "_owner", return_value=self.guard))
        self.gui = self.enterContext(patch.object(gate, "review_once", return_value="APPROVE"))
        self.old_window = self.enterContext(patch.object(file_edits, "_show_edit_review", return_value=True))
        # The existing cooperative writer's Windows ACL inspection is not a
        # live acceptance test here. No shell or real ACL query is executed.
        self.enterContext(patch.object(file_edits, "_require_inherited_windows_acl"))

    def call(self, operation, **arguments):
        return dev.mutate(operation, self.roots, **arguments)

    def apply(self, **extra):
        args = dict(path=str(self.source), old_text="x = 1", new_text="x = 2",
                    expected_sha256=self.digest)
        args.update(extra)
        return self.call("request_source_patch_dev", **args)

    def test_directory_and_no_overwrite_or_recursive_creation(self):
        target = self.root / "new"
        self.assertEqual(self.call("create_directory", path=str(target))["status"], "CREATED_LOCAL_DEV")
        self.assertTrue(target.is_dir())
        self.gui.assert_called_once()
        self.assertEqual(self.call("create_directory", path=str(target))["outcome"], "NOT_STARTED")
        self.assertEqual(self.call("create_directory", path=str(target / "absent" / "child"))["outcome"], "NOT_STARTED")

    def test_owner_denial_all_mutations_and_identity_failure(self):
        self.gui.return_value = "DENY"
        self.assertEqual(self.apply()["status"], "DENIED")
        self.assertEqual(self.call("create_directory", path=str(self.root / "new"))["status"], "DENIED")
        if os.name == "nt":
            self.assertEqual(self.call("move_file", source=str(self.source), destination=str(self.root / "other.py"))["status"], "DENIED")
        self.gui.return_value = "APPROVE"
        self.guard.verify.side_effect = PermissionError("sensitive identity")
        result = self.apply()
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["write_outcome"], "NOT_STARTED")
        self.assertNotIn("sensitive", str(result))
        self.assertEqual(self.source.read_bytes(), b"x = 1\n")

    def test_owner_constructor_failure_never_reviews(self):
        self.owner.side_effect = PermissionError("secret")
        self.assertEqual(self.apply()["outcome"], "NOT_STARTED")
        self.gui.assert_not_called()

    def test_literal_apply_reviews_complete_proposal_and_checks_sha(self):
        result = self.apply()
        self.assertEqual(result["status"], "REPLACED_LOCAL_DEV")
        self.assertEqual(self.source.read_bytes(), b"x = 2\n")
        proposal = self.gui.call_args.args[1]
        self.assertEqual(proposal["full_proposed_content"], "x = 2\n")
        self.assertEqual(proposal["old_sha256"], self.digest)
        self.assertEqual(proposal["new_sha256"], sha256(self.source.read_bytes()).hexdigest())
        self.assertIn("-x = 1", proposal["diff"])
        self.assertGreaterEqual(self.guard.verify.call_count, 3)

    def test_literal_apply_above_review_window_writes_and_verifies(self):
        # Writer and post-write verification share the 64 KiB cap with the
        # preview; a written file must never be reported as UNKNOWN by size.
        before = "".join(f"# filler line {n:05d}\n" for n in range(1200)) + "x = 1\n"
        self.source.write_bytes(before.encode("utf-8"))
        self.assertGreater(self.source.stat().st_size, 16384)
        result = self.apply(expected_sha256=sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(result["status"], "REPLACED_LOCAL_DEV", result)
        self.assertEqual(self.source.read_bytes(), before.replace("x = 1", "x = 2").encode("utf-8"))

    def test_stale_missing_and_duplicate_literal_never_reviews(self):
        self.assertEqual(self.apply(expected_sha256="0" * 64)["outcome"], "NOT_STARTED")
        self.assertEqual(self.apply(old_text="absent")["outcome"], "NOT_STARTED")
        self.source.write_bytes(b"x = 1\nx = 1\n")
        self.assertEqual(self.apply(expected_sha256=sha256(self.source.read_bytes()).hexdigest())["outcome"], "NOT_STARTED")
        self.gui.assert_not_called()

    def test_changes_during_gui_and_immediately_before_commit(self):
        def change_during_review(*args, **kwargs):
            self.source.write_bytes(b"external\n")
            return "APPROVE"
        self.gui.side_effect = change_during_review
        self.assertEqual(self.apply()["outcome"], "NOT_STARTED")
        self.assertEqual(self.source.read_bytes(), b"external\n")
        self.source.write_bytes(b"x = 1\n")
        self.gui.side_effect = None
        original = file_edits.replace_text
        def change_before_replace(*args, **kwargs):
            original_check = kwargs["_before_commit"]
            def late_check(*check_args):
                self.source.write_bytes(b"late change\n")
                original_check(*check_args)
            kwargs["_before_commit"] = late_check
            return original(*args, **kwargs)
        with patch.object(file_edits, "replace_text", side_effect=change_before_replace):
            result = self.apply()
        # The late change is caught by the pre-commit recheck, whose
        # WriteNotStartedError proves nothing was written (P3-4): the outcome
        # is NOT_STARTED, not UNKNOWN. Post-start failures stay UNKNOWN.
        self.assertEqual(result["outcome"], "NOT_STARTED")
        self.assertEqual(self.source.read_bytes(), b"late change\n")

    def test_unknown_write_or_mkdir_never_retries_and_sanitizes(self):
        with patch.object(file_edits, "replace_text", side_effect=OSError(str(self.source))) as write:
            result = self.apply()
        write.assert_called_once()
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertFalse(result["retry_allowed"])
        self.assertNotIn(str(self.root), str(result))
        with patch.object(Path, "mkdir", side_effect=OSError("private")) as mkdir:
            self.assertEqual(self.call("create_directory", path=str(self.root / "new"))["outcome"], "UNKNOWN")
        mkdir.assert_called_once()

    def test_expired_owner_decision_does_not_write(self):
        with patch.object(dev, "monotonic", side_effect=[0, 46]):
            self.assertEqual(self.apply()["status"], "ERROR")
        self.assertEqual(self.source.read_bytes(), b"x = 1\n")
        # A late answer stops at the first clock check, before the session is asked again.
        self.guard.verify.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows non-overwrite rename only")
    def test_move_exact_file_no_overwrite_no_self_or_directory_moves(self):
        destination = self.root / "moved.py"
        self.assertEqual(self.call("move_file", source=str(self.source), destination=str(self.source))["outcome"], "NOT_STARTED")
        directory = self.root / "folder"
        directory.mkdir()
        self.assertEqual(self.call("move_file", source=str(directory), destination=str(directory / "child"))["outcome"], "NOT_STARTED")
        destination.write_bytes(b"keep")
        self.assertEqual(self.call("move_file", source=str(self.source), destination=str(destination))["outcome"], "NOT_STARTED")
        self.assertEqual(destination.read_bytes(), b"keep")
        destination.unlink()
        self.assertEqual(self.call("move_file", source=str(self.source), destination=str(destination))["status"], "MOVED_LOCAL_DEV")
        self.assertFalse(self.source.exists())
        self.assertEqual(destination.read_bytes(), b"x = 1\n")

    @unittest.skipUnless(os.name == "nt", "Windows non-overwrite rename only")
    def test_destination_created_while_gui_open_never_overwritten(self):
        target = self.root / "taken.py"
        def occupy(*args, **kwargs):
            target.write_bytes(b"keep")
            return "APPROVE"
        self.gui.side_effect = occupy
        self.assertEqual(self.call("move_file", source=str(self.source), destination=str(target))["outcome"], "NOT_STARTED")
        self.assertEqual(target.read_bytes(), b"keep")

    def test_unauthorized_and_protected_and_ambiguous_paths(self):
        for path in (str(self.root.parent / "outside"), str(self.root / ".git"),
                     str(self.root / "secrets"), str(self.root / "CON.txt"),
                     str(self.root / "start-sora-commander.cmd"),
                     str(self.root / "some.bak"), str(self.root) + "/../escape",
                     str(self.root / "bad:name"), "relative", str(self.root / "trail.")):
            with self.subTest(path=path):
                result = self.call("create_directory", path=path)
                self.assertEqual(result["outcome"], "NOT_STARTED")
                self.assertNotIn(path, str(result))
        self.gui.assert_not_called()

    def test_hardlink_file_and_synthetic_symlink_junction_root_denied(self):
        alias = self.root / "alias.py"
        os.link(self.source, alias)
        self.assertEqual(self.apply()["outcome"], "NOT_STARTED")
        alias.unlink()
        original = Path.lstat
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
            def synthetic(path, *args, **kwargs):
                if path == self.root:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                return original(path, *args, **kwargs)
            with patch.object(Path, "lstat", synthetic):
                self.assertEqual(self.apply()["outcome"], "NOT_STARTED")
                self.assertEqual(self.call("create_directory", path=str(self.root / "new"))["outcome"], "NOT_STARTED")
        self.gui.assert_not_called()

    def test_recursive_listing_relative_paths_depth_and_scan_cap(self):
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "child.txt").write_text("ok")
        (folder / "secrets").mkdir()
        (folder / "secrets" / "invisible.txt").write_text("never read")
        (self.root / ".private").mkdir()
        plain = file_reads.list_folder(str(self.root), self.roots)
        self.assertTrue(all(set(entry) == {"name", "type"} for entry in plain["entries"]))
        nested = file_reads.list_folder(str(self.root), self.roots, depth=1)
        self.assertIn("folder/child.txt", [entry["path"] for entry in nested["entries"]])
        self.assertNotIn("secrets", str(nested))
        self.assertNotIn(".private", str(nested))
        self.assertTrue(file_reads.list_folder(str(self.root), self.roots, limit=1, depth=1)["truncated"])
        with patch.object(file_reads, "_MAX_NAV_VISITS", 1), self.assertRaises(file_reads.ReadBoundaryError):
            file_reads.list_folder(str(self.root), self.roots, depth=1)
        with self.assertRaises(file_reads.ReadBoundaryError):
            file_reads.list_folder(str(self.root), self.roots, depth=5)

    def test_recursive_listing_denied_folder_skipped(self):
        denied = self.root / "denied"
        denied.mkdir()
        original = Path.iterdir
        def entries(path):
            if path == denied:
                raise PermissionError("private path")
            return original(path)
        with patch.object(Path, "iterdir", entries):
            listing = file_reads.list_folder(str(self.root), self.roots, depth=2)
        self.assertIn("sample.py", str(listing))

    def test_recursive_listing_depth_boundary_and_reparse_or_hidden_entries(self):
        directory = self.root
        for level in range(6):
            directory = directory / ("level" + str(level))
            directory.mkdir()
        listing = file_reads.list_folder(str(self.root), self.roots, depth=4)
        self.assertIn("level0/level1/level2/level3/level4", str(listing))
        self.assertNotIn("level5", str(listing))
        original = Path.lstat
        for attributes in (0x2, 0x4, 0x400):
            def hidden(path, *args, **kwargs):
                if path == self.root / "level0":
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=attributes)
                return original(path, *args, **kwargs)
            with patch.object(Path, "lstat", hidden):
                self.assertNotIn("level0", str(file_reads.list_folder(str(self.root), self.roots, depth=2)))

    @unittest.skipUnless(os.name == "nt", "Windows non-overwrite rename only")
    def test_move_call_uncertain_no_retry_or_rollback(self):
        with patch.object(dev.os, "rename", side_effect=OSError("sensitive")) as rename:
            result = self.call("move_file", source=str(self.source), destination=str(self.root / "new.py"))
        self.assertEqual(result["outcome"], "UNKNOWN")
        rename.assert_called_once()
        self.assertFalse(result["retry_allowed"])

    def test_process_view_only_pid_name_bounded_sorted_and_unavailable_safe(self):
        rows = [SimpleNamespace(info={"pid": 9, "name": "nine.exe"}),
                SimpleNamespace(info={"pid": 2, "name": "two.exe"}),
                SimpleNamespace(info={"pid": 1, "name": "C:/private.exe"})]
        psutil = SimpleNamespace(process_iter=Mock(return_value=rows))
        with patch.dict("sys.modules", psutil=psutil):
            result = dev.list_processes(1)
            self.assertEqual(result, {"processes": [{"pid": 2, "name": "two.exe"}], "truncated": True})
            psutil.process_iter.assert_called_once_with(attrs=["pid", "name"])
            psutil.process_iter.side_effect = PermissionError("sensitive")
            self.assertEqual(dev.list_processes()["error"], "ProcessListingUnavailable")
        with patch.dict("sys.modules", psutil=None):
            with patch.object(dev, "_windows_process_rows", side_effect=OSError("private")):
                self.assertEqual(dev.list_processes()["status"], "ERROR")
        many = SimpleNamespace(process_iter=lambda **kw: (SimpleNamespace(info={"pid": i, "name": "app"}) for i in range(4097)))
        with patch.dict("sys.modules", psutil=many):
            self.assertEqual(dev.list_processes()["status"], "ERROR")

    @unittest.skipUnless(os.name == "nt", "Windows Toolhelp ABI")
    def test_windows_snapshot_abi_synthetic_no_spawn_and_close_on_error(self):
        import ctypes as ct
        api = SimpleNamespace(CreateToolhelp32Snapshot=Mock(return_value=123),
                              Process32FirstW=Mock(), Process32NextW=Mock(),
                              CloseHandle=Mock(return_value=True))
        def first(handle, pointer):
            entry = pointer._obj
            self.assertEqual(handle, 123)
            self.assertEqual(entry.dwSize, ct.sizeof(type(entry)))
            self.assertEqual(ct.sizeof(type(entry)), 568 if ct.sizeof(ct.c_void_p) == 8 else 556)
            entry.th32ProcessID, entry.szExeFile = 7, "synthetic.exe"
            return True
        def end(*args):
            ct.set_last_error(18)
            return False
        api.Process32FirstW.side_effect = first
        api.Process32NextW.side_effect = end
        with patch.object(ct, "WinDLL", return_value=api), patch.dict("sys.modules", psutil=None):
            self.assertEqual(dev.list_processes(), {"processes": [{"pid": 7, "name": "synthetic.exe"}], "truncated": False})
            api.CreateToolhelp32Snapshot.assert_called_once_with(2, 0)
            api.CloseHandle.assert_called_once_with(123)
            api.CloseHandle.reset_mock()
            def fail(*args):
                ct.set_last_error(5)
                return False
            api.Process32NextW.side_effect = fail
            self.assertEqual(dev.list_processes()["status"], "ERROR")
            api.CloseHandle.assert_called_once_with(123)

    def test_create_file_happy_path_and_no_overwrite(self):
        target = self.root / "created.txt"
        result = self.call("create_file", path=str(target), content="hello\n")
        self.assertEqual(result["status"], "CREATED_LOCAL_DEV")
        self.assertEqual(result["outcome"], "VERIFIED_LOCAL_DEV")
        self.assertEqual(target.read_bytes(), b"hello\n")
        proposal = self.gui.call_args.args[1]
        self.assertEqual(proposal["operation"], "create_file")
        self.assertEqual(proposal["full_proposed_content"], "hello\n")
        self.assertEqual(proposal["sha256"], sha256(b"hello\n").hexdigest())
        self.assertEqual(proposal["bytes"], 6)
        self.gui.assert_called_once()
        # An existing target is never overwritten and never re-reviewed.
        again = self.call("create_file", path=str(target), content="changed\n")
        self.assertEqual(again["outcome"], "NOT_STARTED")
        self.assertEqual(target.read_bytes(), b"hello\n")
        self.gui.assert_called_once()

    def test_create_file_denial_and_owner_failure_write_nothing(self):
        target = self.root / "denied.txt"
        self.gui.return_value = "DENY"
        self.assertEqual(self.call("create_file", path=str(target), content="x")["status"], "DENIED")
        self.assertFalse(target.exists())
        self.gui.return_value = "APPROVE"
        self.guard.verify.side_effect = PermissionError("sensitive identity")
        result = self.call("create_file", path=str(target), content="x")
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["write_outcome"], "NOT_STARTED")
        self.assertNotIn("sensitive", str(result))
        self.assertFalse(target.exists())

    def test_create_file_scope_size_and_parent_rules(self):
        for path in (str(self.root.parent / "outside.txt"),
                     str(self.root / "start-sora-commander.cmd"),
                     str(self.root / ".git" / "config"),
                     str(self.root / "absent-parent" / "child.txt")):
            with self.subTest(path=path):
                result = self.call("create_file", path=path, content="x")
                self.assertEqual(result["outcome"], "NOT_STARTED")
                self.assertNotIn(path, str(result))
        oversize = self.call("create_file", path=str(self.root / "big.txt"),
                             content="é" * 40000)  # 80000 UTF-8 bytes
        self.assertEqual(oversize["outcome"], "NOT_STARTED")
        self.assertFalse((self.root / "big.txt").exists())
        surrogate = self.call("create_file", path=str(self.root / "bad.txt"),
                              content="\ud800")
        self.assertEqual(surrogate["outcome"], "NOT_STARTED")
        self.gui.assert_not_called()

    def each_operation(self):
        return (("request_source_patch_dev", dict(path=str(self.source), old_text="x = 1", new_text="x = 2",
                                                  expected_sha256=self.digest)),
                ("create_file", dict(path=str(self.root / "new.txt"), content="x")),
                ("create_directory", dict(path=str(self.root / "folder"))),
                ("move_file", dict(source=str(self.source), destination=str(self.root / "moved.py"))))

    def state(self):
        return sorted((p.name, p.read_bytes() if p.is_file() else None) for p in self.root.iterdir())

    def test_all_four_operations_use_only_the_new_window(self):
        for operation, arguments in self.each_operation():
            with self.subTest(operation=operation):
                self.gui.reset_mock()
                self.call(operation, **arguments)
                self.gui.assert_called_once()
                self.assertIs(self.gui.call_args.args[0], self.guard)
                self.assertEqual(self.gui.call_args.args[1]["operation"], operation)
                self.assertEqual(self.gui.call_args.kwargs["seconds"], 45.0)
                self.assertEqual(set(self.gui.call_args.kwargs), {"seconds", "review_token"})
                self.assertIsNotNone(self.gui.call_args.kwargs["review_token"])
        self.old_window.assert_not_called()

    def test_a_failed_window_refuses_without_the_old_window_or_a_write(self):
        self.gui.return_value = "ERROR"
        before = self.state()
        for operation, arguments in self.each_operation():
            with self.subTest(operation=operation):
                result = self.call(operation, **arguments)
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["write_outcome"], "NOT_STARTED")
                self.assertFalse(result["auto_retry"])
        self.assertEqual(self.state(), before)
        self.old_window.assert_not_called()

    def test_reject_expiry_and_any_other_answer_write_nothing(self):
        before = self.state()
        for decision in ("DENY", "EXPIRED", "CLOSED", "MAYBE", None, True):
            self.gui.return_value = decision
            for operation, arguments in self.each_operation():
                with self.subTest(decision=decision, operation=operation):
                    result = self.call(operation, **arguments)
                    if decision == "DENY":
                        self.assertEqual(result, {"status": "DENIED", "outcome": "NOT_STARTED"})
                    else:
                        self.assertEqual(result["status"], "ERROR")
                        self.assertEqual(result["write_outcome"], "NOT_STARTED")
        self.assertEqual(self.state(), before)
        self.old_window.assert_not_called()

    def through_the_real_gate(self, operation, arguments, answer):
        # The real gate and display data, with a stand-in for the window process only.
        self.guard._identity.session_id = 1
        child = Mock(pid=4242, returncode=0)
        child.poll.return_value = 0

        def communicate(input=None, timeout=None):
            self.frames.append(json.loads(input))
            return answer(self.frames[-1]), b""
        child.communicate.side_effect = communicate
        self.gui.side_effect = REAL_REVIEW_ONCE
        with patch.object(gate.subprocess, "Popen", return_value=child), patch.object(gate, "WindowsJob"), \
                patch.object(gate, "OwnerSessionEventLatch"):
            return self.call(operation, **arguments)

    def test_every_real_proposal_reaches_the_window_and_then_writes(self):
        self.frames = []
        approve = lambda frame: json.dumps({"nonce": frame["nonce"], "decision": "APPROVE"}).encode()
        expected = {"request_source_patch_dev": "REPLACED_LOCAL_DEV", "create_file": "CREATED_LOCAL_DEV",
                    "create_directory": "CREATED_LOCAL_DEV", "move_file": "MOVED_LOCAL_DEV"}
        for operation, arguments in self.each_operation():
            if operation == "move_file":
                arguments = dict(source=str(self.root / "new.txt"), destination=str(self.root / "moved.txt"))
            with self.subTest(operation=operation):
                self.assertEqual(self.through_the_real_gate(operation, arguments, approve)["status"], expected[operation])
        self.assertEqual(self.source.read_bytes(), b"x = 2\n")
        self.assertEqual((self.root / "moved.txt").read_bytes(), b"x")
        self.assertTrue((self.root / "folder").is_dir())
        self.assertEqual(len(self.frames), 4)
        self.assertIn("+x = 2", self.frames[0]["body"])
        self.old_window.assert_not_called()

    def test_a_wrong_answer_through_the_real_gate_writes_nothing(self):
        self.frames = []
        before = self.state()
        wrong = lambda frame: json.dumps({"nonce": "0" * 64, "decision": "APPROVE"}).encode()
        for operation, arguments in self.each_operation():
            with self.subTest(operation=operation):
                result = self.through_the_real_gate(operation, arguments, wrong)
                self.assertEqual((result["status"], result["write_outcome"]), ("ERROR", "NOT_STARTED"))
        self.assertEqual(self.state(), before)
        self.old_window.assert_not_called()

    def test_c5_every_no_answer_and_lost_reply_calls_zero_writers(self):
        self.frames = []
        before = self.state()
        for reason in ('CLOSED', 'EXPIRED', 'DENY', 'lost', 'bad_nonce'):
            def answer(frame):
                if reason == 'lost':
                    return b''
                return json.dumps({'nonce': '0' * 64 if reason == 'bad_nonce' else frame['nonce'],
                                   'decision': 'EXPIRED' if reason == 'bad_nonce' else reason}).encode()
            for operation, arguments in self.each_operation():
                with self.subTest(reason=reason, operation=operation), \
                        patch.object(dev, '_write_new_text_file') as create, \
                        patch.object(dev.os, 'rename') as rename, \
                        patch.object(file_edits, 'replace_text') as replace, \
                        patch.object(Path, 'mkdir') as mkdir:
                    result = self.through_the_real_gate(operation, arguments, answer)
                    if reason in ('CLOSED', 'EXPIRED'):
                        self.assertEqual(result, {'status': 'TIMED_OUT', 'stage': 'FILE_GUI',
                            'reason': reason, 'approval_state': 'EXPIRED', 'write_outcome': 'NOT_STARTED',
                            'auto_retry': False, 'required_action': 'REPORT_AND_WAIT'})
                    elif reason == 'DENY':
                        self.assertEqual(result, {'status': 'DENIED', 'outcome': 'NOT_STARTED'})
                    else:
                        self.assertEqual(result['status'], 'ERROR')
                        self.assertEqual(result['write_outcome'], 'NOT_STARTED')
                    for writer in (create, rename, replace, mkdir):
                        self.assertEqual(writer.call_count, 0)
        self.assertEqual(self.state(), before)
        self.old_window.assert_not_called()

    def test_c5_writer_entry_then_deadline_failure_never_times_out(self):
        clock = [100.0]
        for operation, arguments in self.each_operation():
            seam = {'create_file': (dev, '_write_new_text_file'), 'move_file': (dev.os, 'rename'),
                    'request_source_patch_dev': (file_edits, 'replace_text'),
                    'create_directory': (Path, 'mkdir')}[operation]
            clock[0] = 100.0
            def failed(*a, **kw):
                clock[0] = 200.0
                if operation == 'create_file':
                    Path(arguments['path']).write_bytes(b'partial')
                raise OSError('synthetic failure after writer entry')
            with self.subTest(operation=operation), patch.object(dev, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(*seam, side_effect=failed) as writer:
                result = self.call(operation, **arguments)
                self.assertEqual(result['status'], 'OUTCOME_UNKNOWN')
                self.assertEqual(result['outcome'], 'UNKNOWN')
                writer.assert_called_once()
        self.old_window.assert_not_called()

    def test_c5_mixed_or_foreign_typed_proof_never_writes(self):
        from dataclasses import replace
        before = self.state()
        for field, bad in (('review_token', object()), ('reason', 'DENY'),
                           ('write_outcome', 'UNKNOWN'), ('operation', 'create_file'),
                           ('path', 'other'), ('old_sha256', '0' * 64), ('new_sha256', '0' * 64)):
            def forged(guard, proposal, *, seconds, review_token):
                valid = file_edits._VerifiedFileReviewExpiry(review_token, proposal['path'],
                    proposal['old_sha256'], sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest(),
                    proposal['operation'], reason='CLOSED')
                return replace(valid, **{field: bad})
            with self.subTest(field=field), patch.object(file_edits, 'replace_text') as writer:
                self.gui.side_effect = forged
                result = self.apply()
                self.assertEqual(result['status'], 'ERROR')
                self.assertEqual(result['write_outcome'], 'NOT_STARTED')
                writer.assert_not_called()
        self.assertEqual(self.state(), before)
        self.old_window.assert_not_called()

    def test_the_window_is_shown_the_trusted_preview(self):
        self.apply()
        proposal = self.gui.call_args.args[1]
        self.assertEqual(proposal["operation"], "request_source_patch_dev")
        self.assertEqual(proposal["old_sha256"], self.digest)
        self.assertEqual(proposal["path"], str(self.source))
        self.assertEqual(sha256(proposal["full_proposed_content"].encode()).hexdigest(), proposal["new_sha256"])
        self.assertIn("+x = 2", proposal["diff"])

    def test_a_session_change_after_approval_writes_nothing(self):
        self.guard.verify.side_effect = PermissionError("locked")
        self.assertEqual(self.apply()["write_outcome"], "NOT_STARTED")
        self.assertEqual(self.source.read_bytes(), b"x = 1\n")

    def test_legacy_review_keeps_the_bool_window(self):
        self.assertIsNotNone(dev._review(self.guard, {"command": "synthetic"}))
        self.old_window.assert_called_once()
        self.gui.assert_not_called()

    def test_create_file_write_failure_is_unknown_and_never_retried(self):
        target = self.root / "partial.txt"
        with patch.object(dev.os, "open", side_effect=OSError("private")) as open_:
            result = self.call("create_file", path=str(target), content="data")
        open_.assert_called_once()
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertFalse(result["retry_allowed"])
        self.assertNotIn(str(self.root), str(result))

    def test_release_without_dev_module_does_not_register_dev_endpoints(self):
        host = Host(jobs=Mock())
        with patch("importlib.util.find_spec", return_value=None):
            names = {tool.name for tool in build_server(host)._tool_manager.list_tools()}
        self.assertNotIn("request_source_patch_dev", names)
        self.assertNotIn("create_directory", names)
        self.assertNotIn("create_file", names)

    def test_sdk_config_mutations_no_job_spawn_and_wire_validation(self):
        jobs = Mock()
        host = Host(policy=Policy(roots=self.roots), jobs=jobs)
        sdk = build_server(host)
        async def call(name, args):
            _blocks, data = await sdk.call_tool(name, args)
            return data
        config = asyncio.run(call("get_config", {}))
        self.assertTrue(config["rootsConfigured"])
        self.assertEqual(config["rootCount"], 1)
        self.assertNotIn(str(self.root), str(config))
        self.gui.return_value = "DENY"
        self.assertEqual(asyncio.run(call("request_source_patch_dev", dict(path=str(self.source), old_text="x = 1", new_text="x = 2", expected_sha256=self.digest)))["status"], "DENIED")
        self.assertEqual(asyncio.run(call("create_directory", {"path": str(self.root / "sdk")}))["status"], "DENIED")
        self.assertEqual(asyncio.run(call("create_file", {"path": str(self.root / "sdk.txt"), "content": "x"}))["status"], "DENIED")
        jobs.assert_not_called()
        self.assertEqual(jobs.method_calls, [])
        cases = [("get_config", {"path": "private"}), ("list_processes", {"limit": True}),
                 ("list_processes", {"limit": 201}), ("list_folder", {"path": str(self.root), "depth": True}),
                 ("list_folder", {"path": str(self.root), "depth": 5}),
                 ("create_directory", {"path": True}), ("move_file", {"source": str(self.source)}),
                 ("create_file", {"path": str(self.root / "new.txt")}),
                 ("create_file", {"path": str(self.root / "new.txt"), "content": "x", "approve": True}),
                 ("create_file", {"path": str(self.root / "new.txt"), "content": "é" * 40000}),
                 ("create_directory", {"path": str(self.root / "new"), "approve": True}),
                 ("request_source_patch_dev", {"path": str(self.source), "old_text": "", "new_text": "a", "expected_sha256": self.digest}),
                 ("request_source_patch_dev", {"path": str(self.source), "old_text": "x", "new_text": "é" * 4096, "expected_sha256": self.digest})]
        from mcp.server.fastmcp.exceptions import ToolError
        for name, args in cases:
            with self.subTest(name=name, args=args), self.assertRaises((ValueError, file_reads.ReadBoundaryError)):
                _validate_arguments(name, args)
            with self.assertRaises(ToolError):
                asyncio.run(call(name, args))




# C4 shares the real C3 renderer/latch; only Tk and effects are synthetic.
class _C4Window:
    def __init__(self, test, events=(), return_at=None):
        self.test, self.events, self.return_at = test, events, return_at
        self.callbacks = {}
        self.tk = Mock()
        test.enterContext(patch.dict(sys.modules, {'tkinter': self.tk}))
        test.enterContext(patch.object(dev, 'monotonic', side_effect=lambda: test.now))
        test.enterContext(patch.object(file_edits, 'monotonic', side_effect=lambda: test.now))
        def actions(_tk, _card, reject, approve):
            self.callbacks.update(REJECT=reject, APPROVED=approve)
        def after(ms, callback):
            if 'EXPIRED' not in self.callbacks:
                self.callbacks['EXPIRED'] = callback
                self.timer_delay = ms
        root = self.tk.Tk.return_value
        root.after.side_effect = after
        root.protocol.side_effect = lambda _name, cb: self.callbacks.update(CLOSED=cb)
        def loop():
            for at, event in self.events:
                test.now = at
                self.callbacks[event]()
            if self.return_at is not None:
                test.now = self.return_at
        root.mainloop.side_effect = loop
        test.enterContext(patch.multiple('sora_commander_next.approval_visual_theme',
            window=Mock(), header=Mock(), notice=Mock(), viewer=Mock(), review_actions=actions))


def _c4_cases():
    from itertools import permutations
    cases = [('approve', [(110, 'APPROVED')], None, 'APPROVED'),
             ('reject', [(110, 'REJECT')], None, 'REJECT'),
             ('no_event', [], 146, 'ERROR'),
             ('late_return', [(144, 'APPROVED')], 146, 'ERROR')]
    for at in (144, 145, 146):
        for event in ('CLOSED', 'EXPIRED'):
            expected = 'ERROR' if event == 'EXPIRED' and at < 145 else event
            cases.append((f'{event}_{at}', [(at, event)], None, expected))
        for order in permutations(('APPROVED', 'CLOSED', 'EXPIRED')):
            expected = order[0]
            if expected == 'APPROVED' and at >= 145 or expected == 'EXPIRED' and at < 145:
                expected = 'ERROR'
            cases.append((f'race_{at}_' + '_'.join(order), [(at, e) for e in order], None, expected))
    cases += [('reject_first', [(110, 'REJECT'), (145, 'EXPIRED')], None, 'REJECT')]
    return cases


class C4InputReviewTests(unittest.TestCase):
    def setUp(self):
        from threading import Event, RLock
        self.now = 100.0
        from sora_commander_next.activity import ActivityStore
        self.manager = SimpleNamespace(_lock=RLock(), _stdin_attempts={}, _stdin_pending={},
                                       _activity_store=ActivityStore())
        self.job = SimpleNamespace(stdin_owner=Mock(), request=SimpleNamespace(request_id='c'*32),
                                   child=Mock(), stdin_uncertain=False, cancel=Event())
        self.enterContext(patch.object(dev, '_active_input_job', return_value=self.job))
        self.enterContext(patch('threading.Thread', side_effect=lambda **kw:
            SimpleNamespace(start=lambda: kw['target']())))
        self.write = self.enterContext(patch.object(dev.os, 'write', return_value=2))
        self.sid, self.iid = 'a'*48, 'b'*32

    def submit(self):
        pending = dev.send_input(self.manager, self.sid, '1\n', self.iid)
        self.assertEqual(pending['status'], 'PENDING')
        final = dev.input_status(self.manager, pending['pending_id'])
        self.assertEqual(final['pending_id'], pending['pending_id'])
        self.assertFalse(final['retry_allowed'])
        rows = self.manager._activity_store.snapshot()
        self.assertEqual(rows[0]['state'], final['status'])
        self.assertIsNone(rows[0]['job_state'])
        self.assertNotIn('launch_outcome', final)
        self.assertNotIn('session_id', final)
        self.assertEqual(dev.input_status(self.manager, pending['pending_id']), final)
        self.assertEqual(dev.send_input(self.manager, self.sid, '2\n', self.iid),
                         {'status': 'DENIED', 'error': 'InputIdConsumed', 'retry_allowed': False})
        return final

    def test_write_entered_then_failure_or_short_never_times_out(self):
        for fault in ('raise', 'short'):
            with self.subTest(fault=fault):
                self.manager._stdin_attempts.clear()
                self.manager._stdin_pending.clear()
                self.job.stdin_uncertain = False
                self.job.cancel.clear()
                self.now = 100
                _C4Window(self, [(110, 'APPROVED')])
                def write(*args):
                    self.now = 146
                    if fault == 'raise':
                        raise OSError('private error after entry')
                    return 1
                self.write.side_effect = write
                final = self.submit()
                self.assertEqual(final['status'], 'UNKNOWN')
                self.assertNotIn('write_outcome', final)
                self.assertTrue(self.job.stdin_uncertain)
                self.assertTrue(self.job.cancel.is_set())
        self.assertEqual(self.write.call_count, 2)

    def test_ui_failure_and_owner_loss_are_error_before_write(self):
        for owner_loss in (False, True):
            self.manager._stdin_attempts.clear()
            self.manager._stdin_pending.clear()
            if owner_loss:
                self.job.stdin_owner.verify.side_effect = PermissionError('private Owner detail')
            def observe(*args, **kw):
                if not owner_loss:
                    raise RuntimeError('private UI detail')
                return file_edits._DevFileReviewOutcome('CLOSED', 'LOCAL_EVENT',
                    kw['deadline'], 100, kw['_review_token'])
            with patch.object(dev, 'monotonic', return_value=100), \
                 patch.object(file_edits, 'monotonic', return_value=100), \
                 patch.object(file_edits, '_show_edit_review_dev', side_effect=observe):
                final = self.submit()
            self.assertEqual(final['status'], 'ERROR')
            self.assertEqual(final['write_outcome'], 'NOT_STARTED')
            self.assertNotIn('private', repr(final))
        self.write.assert_not_called()

    def test_deadline_crossed_in_final_job_check_never_writes(self):
        _C4Window(self, [(110, 'APPROVED')])
        calls = []
        def active(*args):
            calls.append(True)
            if len(calls) == 2:
                self.now = 145
            return self.job
        with patch.object(dev, '_active_input_job', side_effect=active):
            final = self.submit()
        self.assertEqual(final['status'], 'ERROR')
        self.assertEqual(final['write_outcome'], 'NOT_STARTED')
        self.write.assert_not_called()

    def test_broker_input_stays_denied_without_wire_or_window(self):
        from sora_commander_next.owner_broker_dev import DevBrokerHost
        from sora_commander_next.owner_broker_service import _SCHEMAS
        transport = Mock()
        host = DevBrokerHost(transport)
        sdk = build_server(host)
        async def call():
            _, result = await sdk.call_tool('send_input', dict(
                session_id=self.sid, input_text='1\n', input_id=self.iid))
            return result
        with patch.object(dev, 'send_input') as send, \
             patch.object(file_edits, '_show_edit_review_dev') as window:
            self.assertEqual(asyncio.run(call()), {'status': 'DENY', 'error': 'DevInteractiveOnly'})
            send.assert_not_called()
            window.assert_not_called()
        self.assertNotIn('send_input', _SCHEMAS)
        self.assertEqual(transport.mock_calls, [])
        self.write.assert_not_called()

    def test_legacy_bool_and_deadline_consumers_unchanged(self):
        cases = [([(110, 'APPROVED')], None, True), ([(110, 'REJECT')], None, False),
                 ([(110, 'CLOSED')], None, False), ([(145, 'EXPIRED')], None, False),
                 ([], 146, False)]
        for events, returned, approved in cases:
            for legacy in ('bool', 'deadline'):
                with self.subTest(events=events, legacy=legacy):
                    self.now = 100
                    _C4Window(self, events, returned)
                    if legacy == 'bool':
                        self.assertIs(file_edits._show_edit_review({'path': 'DEV', 'diff': ''},
                                                                  deadline=145), approved)
                    else:
                        result = dev._review(self.job.stdin_owner, {'command': 'synthetic'}, deadline=145)
                        self.assertEqual(result, 145 if approved else None)
        self.write.assert_not_called()

    def test_bilingual_interactive_notice_uses_no_answer_wording(self):
        from sora_commander_next.owner_review_i18n import load_catalog
        for locale in ('en', 'ja'):
            self.now = 100
            _C4Window(self, [(110, 'CLOSED')])
            with patch('sora_commander_next.approval_visual_theme.notice') as notice:
                file_edits._render_edit_review({'path': 'DEV', 'diff': ''}, deadline=145,
                    locale=locale, _notice_keys=('interactive_caution', 'interactive_information'))
            words = load_catalog(locale)
            self.assertEqual([c.args[2] for c in notice.call_args_list],
                             [words['interactive_caution'], words['interactive_information']])
            self.assertIn('Reject declines' if locale == 'en' else '却下ボタンは提案を却下', words['interactive_caution'])
            self.assertIn('does not start' if locale == 'en' else '開始しません', words['interactive_caution'])
        self.write.assert_not_called()

    def test_forged_mixed_or_missing_proof_is_error_without_write(self):
        from dataclasses import replace
        mutations = [dict(review_token=object()), dict(deadline=144), dict(reason='FORGED'),
                     dict(timer_at=None), dict(timer_at=144), dict(timer_at=147),
                     dict(timer_at=float('nan')), dict(timer_at=float('inf')), dict(timer_at=True),
                     dict(decision='APPROVED', review_token=object())]
        def observed(*args, **kw):
            self.now = 146
            valid = file_edits._DevFileReviewOutcome('EXPIRED', 'LOCAL_EVENT',
                kw['deadline'], 145, kw['_review_token'])
            return replace(valid, **mutation) if type(mutation) is dict else mutation
        for mutation in mutations + [True, None, {'status': 'TIMED_OUT'}]:
            with self.subTest(mutation=mutation):
                self.now = 100
                self.manager._stdin_attempts.clear()
                self.manager._stdin_pending.clear()
                with patch.object(dev, 'monotonic', side_effect=lambda: self.now), \
                     patch.object(file_edits, 'monotonic', side_effect=lambda: self.now), \
                     patch.object(file_edits, '_show_edit_review_dev', side_effect=observed):
                    # A result-shaped dictionary is not an observation either.
                    if mutation == {'status': 'TIMED_OUT'}:
                        with patch.object(file_edits, '_show_edit_review_dev', return_value=mutation):
                            final = self.submit()
                    else:
                        final = self.submit()
                self.assertEqual(final['status'], 'ERROR')
                self.assertEqual(final['write_outcome'], 'NOT_STARTED')
                self.assertFalse(self.job.stdin_uncertain)
                self.assertFalse(self.job.cancel.is_set())
        self.write.assert_not_called()


def _c4_input_case(events, return_at, expected):
    def test(self):
        window = _C4Window(self, events, return_at)
        final = self.submit()
        status = 'SENT' if expected == 'APPROVED' else 'DENIED' if expected == 'REJECT' else 'ERROR' if expected == 'ERROR' else 'TIMED_OUT'
        self.assertEqual(final['status'], status)
        if status == 'TIMED_OUT':
            self.assertEqual(final, dict(status='TIMED_OUT', pending_id=final['pending_id'],
                retry_allowed=False, stage='INPUT_GUI', reason=expected,
                approval_state='EXPIRED', write_outcome='NOT_STARTED', auto_retry=False,
                required_action='REPORT_AND_WAIT'))
        if status == 'ERROR':
            self.assertEqual(final['write_outcome'], 'NOT_STARTED')
        if status == 'SENT':
            self.write.assert_called_once()
        else:
            self.write.assert_not_called()
        self.assertFalse(self.job.stdin_uncertain)
        self.assertFalse(self.job.cancel.is_set())
        self.assertEqual(window.timer_delay, 45025)
        window.tk.Tk.assert_called_once()
    return test


for _name, _events, _return_at, _expected in _c4_cases():
    setattr(C4InputReviewTests, 'test_c4_input_' + _name,
            _c4_input_case(_events, _return_at, _expected))


if __name__ == "__main__":
    unittest.main()
