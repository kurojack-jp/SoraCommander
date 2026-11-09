"""Cross-tool completion tests: wait for existing work, never self-approve or retry.

Real Windows Job Object and disposable file writers are exercised. The
Owner GUI decision is synthetic in this test; production is never activated.
All real file changes are restricted to uniquely named throwaway fixtures.
"""
import asyncio
from hashlib import sha256
from threading import Event
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.edit_review_pilot import EditReviewPilot
from sora_commander_next.mcp_adapter import (
    _validate_arguments, _await_edit_data_only, build_server,
)
from sora_commander_next.policy import Policy
from tests._inherited_test_temp import inherited_test_temp
from tests._mock_read_boundary import mock_read_boundary


def call(server, name, args):
    return asyncio.run(server.call_tool(name, args))[1]


class UnifiedOperationWaitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        self.host = Host(policy=Policy(roots=(str(self.root),)))
        self.addCleanup(self.host.jobs.close)

    def mock_pilot(self, *, create=False):
        pilot = Mock()
        pilot.enable_create = create
        self.host._edit_review_pilot = pilot
        return pilot, build_server(self.host)

    def test_wait_existing_process_returns_output_no_duplicate_start(self):
        server = build_server(self.host)
        request = Command(
            sys.executable, ("-I", "-S", "-c",
                             "import time; time.sleep(.18); print('later-output')"),
            str(self.root), timeout=3)
        started = self.host.jobs.start(request)
        session_id = started["session_id"]
        with patch.object(self.host.jobs, "start") as no_resubmit:
            first = call(server, "await_process_output", {
                "session_id": session_id, "wait_seconds": 0})
            self.assertEqual(first["status"], "RUNNING", first)
            completed = call(server, "await_process_output", {
                "session_id": session_id, "wait_seconds": 4})
            self.assertEqual(completed["status"], "COMPLETED", completed)
            self.assertIn("later-output", completed["stdout"])
            self.assertEqual(completed["session_id"], session_id)
            no_resubmit.assert_not_called()

    def test_stop_process_wait_reports_real_cancelled_job(self):
        server = build_server(self.host)
        request = Command(
            sys.executable, ("-I", "-S", "-c", "import time; time.sleep(4)"),
            str(self.root), timeout=8)
        started = self.host.jobs.start(request)
        with patch.object(self.host.jobs, "start") as no_resubmit:
            stopped = call(server, "stop_process_wait", {
                "session_id": started["session_id"], "wait_seconds": 4})
        self.assertEqual(stopped["status"], "CANCELLED", stopped)
        self.assertEqual(stopped["stop_request_status"], "CANCELLING")
        self.assertEqual(stopped["session_id"], started["session_id"])
        no_resubmit.assert_not_called()
        self.assertEqual(call(server, "await_process_output", {
            "session_id": started["session_id"], "wait_seconds": 0})["status"],
            "CANCELLED")

    def test_stop_bad_id_and_invalid_wait_never_stops(self):
        for invalid in (True, -1, 91, float("nan"), "10", None):
            with self.subTest(invalid=invalid), patch.object(
                    self.host.jobs, "stop") as stop:
                result = self.host.stop_process_wait(
                    "f" * 48, wait_seconds=invalid)
                self.assertEqual(result["error"], "InvalidWaitSeconds")
                stop.assert_not_called()
        self.assertEqual(
            self.host.stop_process_wait("bad", wait_seconds=1)["status"],
            "ERROR")
        self.assertEqual(
            self.host.await_process_output("bad", wait_seconds=1)["status"],
            "ERROR")

    def test_edit_wait_one_submission_one_result_no_chat(self):
        pilot, server = self.mock_pilot()
        p = str(self.root / "owner-edit-pilot-fixture.txt")
        pending = "a" * 32
        pilot.request.return_value = {
            "status": "REVIEW", "pending_id": pending,
            "new_sha256": "d" * 64}
        pilot.status.side_effect = [
            {"status": "PENDING"},
            {"status": "REPLACED_LOCAL_DEV", "sha256": "d" * 64},
        ]
        result = call(server, "request_text_edit_wait", {
            "path": p, "content": "updated", "expected_sha256": "b" * 64,
            "wait_seconds": 2})
        self.assertEqual(result["status"], "REPLACED_LOCAL_DEV", result)
        self.assertEqual(result["pending_id"], pending)
        pilot.request.assert_called_once_with(p, "updated", "b" * 64)
        self.assertEqual(pilot.status.call_count, 2)

    def test_real_disposable_edit_pilot_roundtrip_one_mcp_call(self):
        """Actual pilot queue/thread/SHA check; mock only human click + disk edit."""
        target = self.root / "owner-edit-pilot-live-test.txt"
        target.write_text("before", encoding="utf-8")
        original = sha256(target.read_bytes()).hexdigest()
        # Option B: production _check denies the canonical temp/ fixture ancestor.
        self.enterContext(mock_read_boundary(self.root, (target.name,), preview=True))
        proposed = "after"
        expected = sha256(proposed.encode("utf-8")).hexdigest()
        self.host._edit_review_pilot = EditReviewPilot(self.root)
        server = build_server(self.host)
        def local_approved_replace(path, roots, content, old_sha):
            self.assertEqual(path, str(target))
            self.assertEqual(old_sha, original)
            self.assertEqual(sha256(target.read_bytes()).hexdigest(), original)
            target.write_text(content, encoding="utf-8")
            return {"status": "REPLACED_LOCAL_DEV", "sha256": expected}
        with patch("sora_commander_next.edit_review_pilot.review_and_replace_local",
                   side_effect=local_approved_replace) as reviewed:
            result = call(server, "request_text_edit_wait", {
                "path": str(target), "content": proposed,
                "expected_sha256": original, "wait_seconds": 3})
        self.assertEqual(result["status"], "REPLACED_LOCAL_DEV", result)
        self.assertEqual(result["sha256"], expected)
        self.assertEqual(sha256(target.read_bytes()).hexdigest(), expected)
        reviewed.assert_called_once()

    def test_real_disposable_edit_pilot_native_atomic_replace(self):
        """Real Win32 ACL and actual CAS writer; only GUI click is synthetic."""
        from uuid import uuid4
        from sora_commander_next import file_edits
        scope = ROOT / "tests"
        target = scope / (
            "owner-edit-pilot-auto-atomic-integration-" + uuid4().hex + ".txt")
        initial = b"Owner auto-wait CAS test BEFORE\\n"
        changed = b"Owner auto-wait CAS test AFTER\\n"
        self.assertFalse(target.exists())
        with target.open("xb") as out:
            out.write(initial)
        self.host._edit_review_pilot = EditReviewPilot(scope)
        server = build_server(self.host)
        try:
            # The native Owner click is simulated. Actual Owner guard,
            # inherited-ACL inspection and atomic replacement are NOT mocked.
            with patch.object(file_edits, "_show_edit_review",
                              return_value=True) as reviewed:
                result = call(server, "request_text_edit_wait", {
                    "path": str(target), "content": changed.decode("utf-8"),
                    "expected_sha256": sha256(initial).hexdigest(),
                    "wait_seconds": 15})
            self.assertEqual(result["status"], "REPLACED_LOCAL_DEV", result)
            self.assertEqual(result["sha256"], sha256(changed).hexdigest())
            self.assertEqual(target.read_bytes(), changed)
            reviewed.assert_called_once()
            self.assertEqual(list(scope.glob(".sora-next-*.tmp")), [])
        finally:
            if (target.is_file() and not target.is_symlink()
                    and not target.is_junction()
                    and target.read_bytes() in (initial, changed)):
                target.unlink()

    def test_real_pilot_timeout_and_resume_without_duplicate_edit(self):
        target = self.root / "owner-edit-pilot-deferred.txt"
        target.write_text("first", encoding="utf-8")
        original = sha256(target.read_bytes()).hexdigest()
        self.enterContext(mock_read_boundary(self.root, (target.name,), preview=True))
        proposed = "second"
        expected = sha256(proposed.encode("utf-8")).hexdigest()
        self.host._edit_review_pilot = EditReviewPilot(self.root)
        server = build_server(self.host)
        gate = Event()
        def delayed_approval(path, roots, content, old_sha):
            if not gate.wait(3):
                return {"status": "DENIED"}
            self.assertEqual(sha256(target.read_bytes()).hexdigest(), original)
            target.write_text(content, encoding="utf-8")
            return {"status": "REPLACED_LOCAL_DEV", "sha256": expected}
        try:
            with patch("sora_commander_next.edit_review_pilot.review_and_replace_local",
                       side_effect=delayed_approval) as reviewed:
                initial = call(server, "request_text_edit_wait", {
                    "path": str(target), "content": proposed,
                    "expected_sha256": original, "wait_seconds": 0})
                self.assertEqual(initial["status"], "PENDING", initial)
                gate.set()
                result = call(server, "await_edit_result", {
                    "pending_id": initial["pending_id"], "wait_seconds": 3})
            self.assertEqual(result["status"], "REPLACED_LOCAL_DEV", result)
            self.assertEqual(target.read_text(encoding="utf-8"), proposed)
            reviewed.assert_called_once()
        finally:
            gate.set()

    def test_edit_timeout_returns_pending_then_read_only_resume(self):
        pilot, server = self.mock_pilot()
        pending = "b" * 32
        pilot.request.return_value = {"status": "REVIEW",
                                      "pending_id": pending}
        pilot.status.side_effect = [
            {"status": "PENDING"},
            {"status": "REPLACED_LOCAL_DEV", "sha256": "c" * 64},
        ]
        args = {
            "path": str(self.root / "owner-edit-pilot-fixture.txt"),
            "content": "after", "expected_sha256": "d" * 64,
            "wait_seconds": 0}
        first = call(server, "request_text_edit_wait", args)
        self.assertEqual(first, {"status": "PENDING", "pending_id": pending})
        resumed = call(server, "await_edit_result", {
            "pending_id": pending, "wait_seconds": 1})
        self.assertEqual(resumed["status"], "REPLACED_LOCAL_DEV")
        pilot.request.assert_called_once()
        self.assertEqual(pilot.status.call_count, 2)

    def test_edit_denial_and_stale_error_have_no_retry(self):
        pilot, server = self.mock_pilot()
        pilot.request.return_value = {
            "status": "REVIEW", "pending_id": "c" * 32}
        pilot.status.return_value = {"status": "DENIED"}
        denied = call(server, "request_text_edit_wait", {
            "path": str(self.root / "owner-edit-pilot-fixture.txt"),
            "content": "after", "expected_sha256": "0" * 64,
            "wait_seconds": 1})
        self.assertEqual(denied["status"], "DENIED")
        self.assertEqual(pilot.request.call_count, 1)
        pilot.request.side_effect = None
        pilot.request.return_value = {
            "status": "ERROR", "error": "STALE_SHA256"}
        stale = call(server, "request_text_edit_wait", {
            "path": str(self.root / "owner-edit-pilot-fixture.txt"),
            "content": "after", "expected_sha256": "0" * 64})
        self.assertEqual(stale["error"], "STALE_SHA256")
        self.assertEqual(pilot.status.call_count, 1)

    def test_edit_no_change_is_sync_and_does_not_wait(self):
        pilot, server = self.mock_pilot()
        pilot.request.return_value = {"status": "UNCHANGED"}
        out = call(server, "request_text_edit_wait", {
            "path": str(self.root / "owner-edit-pilot-fixture.txt"),
            "content": "", "expected_sha256": "0" * 64})
        self.assertEqual(out["status"], "UNCHANGED")
        pilot.status.assert_not_called()

    def test_invalid_edit_wait_rejected_before_request(self):
        pilot, server = self.mock_pilot()
        for bad in (True, -1, 91, "10", float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _validate_arguments("request_text_edit_wait", {
                    "path": "C:\\fixture.txt", "content": "x",
                    "expected_sha256": "0" * 64,
                    "wait_seconds": bad})
                # Must raise before any edit request.
        pilot.request.assert_not_called()

    def test_create_wait_is_create_opt_in_only(self):
        pilot, server = self.mock_pilot(create=False)
        names = {t.name for t in server._tool_manager.list_tools()}
        self.assertNotIn("request_text_create", names)
        self.assertNotIn("request_text_create_wait", names)
        self.assertIn("request_text_edit_wait", names)
        self.assertIn("await_edit_result", names)
        self.assertNotIn("approve", names)

    def test_create_wait_one_submission_and_no_implicit_retry(self):
        pilot, server = self.mock_pilot(create=True)
        p = str(self.root / "owner-edit-pilot-new.txt")
        pilot.request_create.return_value = {
            "status": "REVIEW", "pending_id": "d" * 32}
        pilot.status.side_effect = [
            {"status": "PENDING"},
            {"status": "CREATED_LOCAL_DEV", "sha256": "e" * 64},
        ]
        first = call(server, "request_text_create_wait", {
            "path": p, "content": "test", "wait_seconds": 0})
        self.assertEqual(first["status"], "PENDING")
        later = call(server, "await_edit_result", {
            "pending_id": first["pending_id"], "wait_seconds": 1})
        self.assertEqual(later["status"], "CREATED_LOCAL_DEV")
        pilot.request_create.assert_called_once_with(p, "test")
        self.assertEqual(pilot.status.call_count, 2)

    def test_real_disposable_create_pilot_exclusive_disk_write(self):
        """Actual create CAS, exclusive file, post-SHA; synthetic Owner click."""
        from uuid import uuid4
        from sora_commander_next.file_edits import (
            _CREATE_ROOT, create_text_exclusive)
        self.assertEqual(_CREATE_ROOT, ROOT / "tests" / "real_mcp")
        target = _CREATE_ROOT / (
            "owner-edit-pilot-auto-create-integration-" + uuid4().hex + ".txt")
        proposed = "Sora auto-wait exclusive create test\\n"
        expected = sha256(proposed.encode("utf-8")).hexdigest()
        self.assertFalse(target.exists())
        self.host._edit_review_pilot = EditReviewPilot(
            _CREATE_ROOT, enable_create=True)
        server = build_server(self.host)

        def synthetic_gui_approved(path, roots, content, new_sha):
            # Explicit unit-test stand-in for GUI; real restricted writer.
            self.assertEqual(path, str(target))
            self.assertEqual(new_sha, expected)
            return create_text_exclusive(path, roots, content, new_sha)

        try:
            with patch(
                "sora_commander_next.edit_review_pilot.review_and_create_local",
                side_effect=synthetic_gui_approved
            ) as decision:
                result = call(server, "request_text_create_wait", {
                    "path": str(target), "content": proposed, "wait_seconds": 5})
                self.assertEqual(result["status"], "CREATED_LOCAL_DEV", result)
                self.assertEqual(result["sha256"], expected)
                self.assertEqual(sha256(target.read_bytes()).hexdigest(), expected)
                duplicate = call(server, "request_text_create_wait", {
                    "path": str(target), "content": proposed, "wait_seconds": 1})
                self.assertEqual(duplicate.get("error"), "CREATE_REJECTED")
                decision.assert_called_once()
                self.assertEqual(target.read_text(encoding="utf-8"), proposed)
        finally:
            # Only delete this exact unique file when its bytes are unchanged.
            if (target.is_file() and not target.is_symlink()
                    and not target.is_junction()
                    and target.read_bytes() == proposed.encode("utf-8")):
                target.unlink()

    def test_create_reject_does_not_issue_creation(self):
        pilot, server = self.mock_pilot(create=True)
        pilot.request_create.return_value = {
            "status": "ERROR", "error": "CREATE_REJECTED"}
        result = call(server, "request_text_create_wait", {
            "path": str(self.root / "owner-edit-pilot-new.txt"),
            "content": "test"})
        self.assertEqual(result["error"], "CREATE_REJECTED")
        pilot.status.assert_not_called()

    def test_edit_unknown_is_never_reissued(self):
        pilot, server = self.mock_pilot()
        pilot.status.return_value = {"status": "UNKNOWN"}
        out = call(server, "await_edit_result", {
            "pending_id": "e" * 32, "wait_seconds": 1})
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertEqual(out["pending_id"], "e" * 32)
        pilot.request.assert_not_called()

    def test_base_tool_matrix_no_write_without_owner_opt_in(self):
        names = {t.name for t in build_server(self.host)._tool_manager.list_tools()}
        self.assertEqual(names, {
            "run_command", "run_command_wait", "await_command_result",
            "read_process_output", "await_process_output", "stop_process",
            "stop_process_wait", "ping", "status", "list_folder",
            "read_text_file", "review_status", "preview_text_edit",
            "list_sessions", "previous_sessions", "recovery_overview", "request_status", "previous_output",
            # Ten bounded reads, two non-writing previews, local catalog only.
            "list_folder_page", "read_text_page", "find_files", "search_code",
            "path_info", "read_text_lines", "read_many_text_files",
            "compare_text_files", "read_text_chunk", "sha256_file",
            "preview_source_create", "preview_source_patch", "tool_catalog",
                "create_directory", "create_file", "move_file", "request_source_patch_dev",
                "list_processes", "get_config", "send_input",
        })
        self.assertFalse(names & {
            "approve", "reject", "approve_local", "reject_local",
            "request_source_create", "request_source_patch", "source_review_status",
            "cancel_source_review", "request_text_edit", "request_text_edit_wait",
            "edit_review_status", "await_edit_result", "request_text_create",
            "request_text_create_wait",
        })
        for tool in ("run_command_wait", "await_command_result",
                     "await_process_output", "stop_process_wait"):
            self.assertIn(tool, names)
        for tool in ("request_text_edit", "request_text_edit_wait",
                     "await_edit_result", "request_text_create_wait"):
            self.assertNotIn(tool, names)
        self.assertNotIn("approve", names)
        self.assertNotIn("reject", names)


if __name__ == "__main__":
    unittest.main()
