"""Offline real-child lifecycle tests and synthetic approval/adapter fault tests."""

import base64
from concurrent.futures import ThreadPoolExecutor
import importlib
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.owner_ui import OwnerController, OwnerView
from sora_commander_next.policy import Decision, Grant, Pattern, Policy


def payload(request):
    return {name: getattr(request, name) for name in request.__dataclass_fields__}


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.policy = Policy((str(ROOT),), (Pattern(sys.executable, ("--version",)),), lambda: self.now)
        self.jobs = JobManager()
        self.addCleanup(self.jobs.close)
        self.host = Host(self.policy, "owner", LocalApproval(lambda: self.now), jobs=self.jobs)
        self.controller = OwnerController(self.host)
        self.grants = 0

    def request(self, code="print('hello')", **changes):
        return Command(sys.executable, ("-I", "-S", "-c", code), str(ROOT), **changes)

    def grant(self, request):
        self.grants += 1
        grant = Grant(str(self.grants), "owner", str(ROOT),
                      (Pattern(request.executable, request.argv),), self.now + 100)
        self.policy.add_grant_local(grant)
        return grant

    def start(self, code="print('hello')", **changes):
        request = self.request(code, **changes)
        self.grant(request)
        result = self.host.run_command(background=True, **payload(request))
        self.assertEqual(result["status"], "RUNNING", result)
        return result["session_id"]

    def finish(self, session):
        result = self.jobs.wait(session)
        self.assertNotEqual(result["status"], "OUTCOME_UNKNOWN", result)
        self.assertIsNotNone(result["exit_code"])
        return result

    def pending(self, request=None):
        result = self.host.run_command(background=True, **payload(request or self.request()))
        self.assertEqual(result["status"], "REVIEW")
        return result["pending_id"]

    def test_long_command_budget_requires_background_mode(self):
        request = self.request("print('long-budget')", timeout=3600)
        self.grant(request)
        self.assertEqual(self.host.run_command(**payload(request))["error"], "LongCommandRequiresBackground")
        started = self.host.run_command(background=True, **payload(request))
        self.assertEqual(started["status"], "RUNNING")
        finished = self.finish(started["session_id"])
        self.assertEqual(finished["status"], "COMPLETED")
        self.assertEqual(finished["stdout"].strip(), "long-budget")
        with self.assertRaises(ValueError):
            self.request(timeout=3601)
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_real_background_completion_and_closed_stdin(self):
        session = self.start("import sys; assert sys.stdin.read()==''; print('out'); print('err',file=sys.stderr)")
        result = self.finish(session)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "out")
        self.assertEqual(result["stderr"].strip(), "err")
        self.assertNotEqual(str(result["pid"]), session)
        self.assertEqual(len(session), 48)

    def test_running_health_100_times_and_next_request(self):
        session = self.start("import time; print('running',flush=True); time.sleep(.5)")
        self.assertEqual(self.host.read_process_output(session)["status"], "RUNNING")
        for _ in range(100):
            self.assertEqual(self.host.ping()["status"], "OK")
            self.assertEqual(self.host.status()["sessions"], "IN_MEMORY")
        self.assertEqual(self.finish(session)["status"], "COMPLETED")
        self.assertEqual(self.host.run_command(**payload(Command(sys.executable, ("--version",), str(ROOT))))["status"], "COMPLETED")

    def test_incremental_byte_cursor_and_multibyte_reconstruction(self):
        session = self.start("import os,time; os.write(1,bytes([227,129,130])); time.sleep(.3); os.write(2,b'err'); os.write(1,b'end')")
        deadline = time.monotonic() + 2
        while True:
            first = self.host.read_process_output(session)
            if first["captured_bytes"] >= 3:
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)
        self.assertEqual(first["status"], "RUNNING")
        self.assertEqual(first["stdout"], "あ")
        final = self.finish(session)
        tail = self.host.read_process_output(session, 2)
        self.assertEqual(tail["returned_bytes"], final["captured_bytes"] - 2)
        raw = b"".join(base64.b64decode(c["data_base64"]) for c in tail["chunks"])
        self.assertEqual(raw[:1], b'\x82')
        later = self.host.read_process_output(session, first["next_offset"])
        self.assertEqual(later["stdout"], "end")
        self.assertEqual(later["stderr"], "err")
        self.assertEqual(self.host.read_process_output(session, final["next_offset"])["returned_bytes"], 0)
        self.assertEqual(self.host.read_process_output(session), final)

    def test_background_overflow_is_bounded_and_terminal(self):
        result = self.finish(self.start("import os; os.write(1,b'a'*200000); os.write(2,b'b'*200000)", output_limit=128))
        self.assertEqual(result["status"], "OUTPUT_LIMIT")
        self.assertTrue(result["truncated"])
        self.assertTrue(result["overflow"])
        self.assertEqual(result["captured_bytes"], 128)
        self.assertEqual(result["returned_bytes"], 128)
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_background_timeout(self):
        result = self.finish(self.start("import time; time.sleep(5)", timeout=.1))
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_cancel_owned_job_and_repeat_stop_without_replay(self):
        session = self.start("import time; time.sleep(5)")
        self.assertEqual(self.host.stop_process(session)["status"], "CANCELLING")
        self.host.stop_process(session)
        self.assertEqual(self.finish(session)["status"], "CANCELLED")
        result = self.host.stop_process(session)
        self.assertTrue(result["already_finished"])
        self.assertEqual(result["status"], "CANCELLED")

    def test_stop_does_not_cancel_other_owned_child(self):
        first = self.start("import time; time.sleep(5)")
        second = self.start("import time; time.sleep(.3); print('second')")
        self.host.stop_process(first)
        self.assertEqual(self.finish(first)["status"], "CANCELLED")
        self.assertEqual(self.finish(second)["stdout"].strip(), "second")

    def test_bad_unknown_and_previous_host_ids_cannot_target_pid(self):
        session = self.start("print('one')")
        result = self.finish(session)
        for bad in (None, 1, True, [], {}, "", "bad", str(result["pid"]), "f" * 48):
            with self.subTest(bad=bad):
                self.assertEqual(self.host.read_process_output(bad)["status"], "ERROR")
                self.assertEqual(self.host.stop_process(bad)["status"], "ERROR")
        fresh = Host(self.policy)
        self.addCleanup(fresh.jobs.close)
        self.assertEqual(fresh.read_process_output(session)["error"], "UnknownSession")
        self.assertEqual(fresh.stop_process(session)["error"], "UnknownSession")

    def test_invalid_offsets_fail_without_host_failure(self):
        session = self.start()
        self.finish(session)
        for offset in (True, None, -1, 1.0, "0", 100000, [], float("nan")):
            self.assertEqual(self.host.read_process_output(session, offset)["status"], "ERROR")
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_concurrency_rejected_before_popen(self):
        self.jobs.max_concurrent = 1
        session = self.start("import time; time.sleep(.3)")
        request = self.request()
        self.grant(request)
        with patch("sora_commander_next.jobs.subprocess.Popen") as spawn:
            result = self.host.run_command(background=True, **payload(request))
        self.assertEqual(result, {"status": "BUSY", "error": "ConcurrencyLimit", "decision": "SCOPED"})
        spawn.assert_not_called()
        self.finish(session)

    def test_session_capacity_and_expiry_without_evicting_live_jobs(self):
        self.jobs.capacity = 1
        self.jobs.retention, self.jobs.clock = 5, lambda: self.now
        session = self.start("import time; time.sleep(.2)")
        self.now += 10
        self.assertEqual(self.host.read_process_output(session)["status"], "RUNNING")
        self.finish(session)
        request = self.request()
        self.grant(request)
        self.assertEqual(self.host.run_command(background=True, **payload(request))["error"], "SessionCapacity")
        self.now += 5
        self.assertEqual(self.host.read_process_output(session)["error"], "UnknownSession")
        self.assertEqual(self.host.stop_process(session)["error"], "UnknownSession")
        self.finish(self.start())

    def test_invalid_manager_limits(self):
        for kwargs in ({"capacity": 0}, {"max_concurrent": True}, {"max_concurrent": 17},
                       {"retention": float("inf")}, {"retention": True}, {"retention": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                JobManager(**kwargs)

    def test_review_and_deny_never_spawn_worker(self):
        with patch("sora_commander_next.jobs.subprocess.Popen") as spawn:
            pending = self.pending()
            denied = Command(str(ROOT / "runas.exe"), (), str(ROOT))
            self.assertEqual(self.host.run_command(background=True, **payload(denied))["status"], "DENY")
            self.assertEqual(len(self.controller.pending_local()), 1)
            self.assertEqual(self.controller.approve_local("invented-deny")["status"], "ERROR")
        spawn.assert_not_called()
        self.assertEqual(self.controller.reject_local(pending)["status"], "REJECTED")

    def test_review_approval_runs_once_in_background(self):
        request = self.request("import time; time.sleep(.3); print('approved')", timeout=1, output_limit=128)
        pending = self.pending(request)
        approved = self.controller.approve_local(pending)
        self.assertEqual(approved["status"], "RUNNING")
        self.assertEqual(approved["decision"], "REVIEW")
        self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
        result = self.finish(approved["session_id"])
        self.assertEqual(result["request_id"], request.request_id)
        self.assertEqual(result["stdout"].strip(), "approved")

    def test_racing_approval_only_one_consumes(self):
        pending = self.pending()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.controller.approve_local, [pending] * 4))
        self.assertEqual(sum(r["status"] == "RUNNING" for r in results), 1)
        session = next(r["session_id"] for r in results if "session_id" in r)
        self.finish(session)

    def test_approval_reject_revoke_and_expiry(self):
        pending = self.pending()
        self.assertEqual(self.controller.reject_local(pending)["status"], "REJECTED")
        self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
        pending = self.pending()
        self.now += 60
        with patch("sora_commander_next.jobs.subprocess.Popen") as spawn:
            self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
        spawn.assert_not_called()
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_policy_generation_add_and_revoke_invalidate_reviews(self):
        for revoke in (False, True):
            grant = self.grant(self.request("print('other')"))
            pending = self.pending()
            if revoke:
                self.policy.revoke_local(grant.grant_id, "owner")
            else:
                self.grant(self.request("print('another')"))
            with patch("sora_commander_next.jobs.subprocess.Popen") as spawn:
                self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
            spawn.assert_not_called()

    def test_owner_and_replaced_policy_context_invalidate_review(self):
        pending = self.pending()
        self.host.owner = "other"
        self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
        self.host.owner = "owner"
        pending = self.pending()
        self.host.policy = Policy((str(ROOT),))
        self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")

    def test_reclassification_to_deny_or_unknown_cannot_be_approved(self):
        for decision in (Decision.DENY, Decision.AUTO, None):
            pending = self.pending()
            with patch.object(self.policy, "classify", return_value=decision), patch("sora_commander_next.jobs.subprocess.Popen") as spawn:
                self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
            spawn.assert_not_called()

    def test_expiry_during_reclassification_cannot_spawn(self):
        pending = self.pending()
        def classify(*args):
            self.now += 60
            return Decision.REVIEW
        with patch.object(self.policy, "classify", side_effect=classify), patch("sora_commander_next.jobs.subprocess.Popen") as spawn:
            self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
        spawn.assert_not_called()

    def test_approval_no_substitution_argument_and_digest_tampering(self):
        pending = self.pending()
        with self.assertRaises(TypeError):
            self.controller.approve_local(pending, self.request("print('substitute')"))
        record = self.host.approval._pending[pending]
        object.__setattr__(record, "request", self.request("print('tampered')"))
        with patch("sora_commander_next.jobs.subprocess.Popen") as spawn:
            self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
        spawn.assert_not_called()

    def test_spawn_failure_consumes_approval_without_retry_or_secret_message(self):
        pending = self.pending()
        with patch("sora_commander_next.jobs.subprocess.Popen", side_effect=OSError("secret")) as spawn:
            result = self.controller.approve_local(pending)
            self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")
        self.assertEqual(spawn.call_count, 1)
        self.assertNotIn("secret", str(result))
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_capture_failure_kills_child_and_isolates_host(self):
        request = self.request("import time; time.sleep(5)")
        self.grant(request)
        with patch("sora_commander_next.jobs._read_available", side_effect=OSError("secret")):
            result = self.host.run_command(background=True, **payload(request))
            final = self.finish(result["session_id"])
        self.assertEqual(final["status"], "ERROR")
        self.assertNotIn("secret", str(final))
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_incomplete_pipe_has_no_blocked_reader_threads(self):
        # Synthetic held-pipe condition; no actual unsupported descendants launched.
        request = Command(sys.executable, ("--version",), str(ROOT))
        with patch("sora_commander_next.jobs._read_available", return_value=None):
            result = self.host.run_command(background=True, **payload(request))
            final = self.finish(result["session_id"])
        self.assertEqual(final["status"], "OUTPUT_INCOMPLETE")
        self.assertTrue(final["output_incomplete"])
        self.assertFalse(self.jobs._jobs[result["session_id"]].thread.is_alive())

    def test_thread_start_failure_reaps_spawned_child(self):
        request = Command(sys.executable, ("--version",), str(ROOT))
        children = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        with patch("sora_commander_next.jobs.subprocess.Popen", side_effect=spawn), patch("sora_commander_next.jobs.Thread.start", side_effect=RuntimeError("secret")):
            result = self.host.run_command(background=True, **payload(request))
        self.assertEqual(result, {
            "request_id": request.request_id, "status": "OUTCOME_UNKNOWN",
            "error": "LaunchOutcomeUnknown", "launch_phase": "resumed",
        })
        self.assertNotIn("secret", str(result))
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stderr.closed)
        self.assertFalse(self.jobs._jobs)
        self.assertNotIn(request.request_id, self.jobs._request_sessions)
        # Fail before any OS launch if the replay guard regresses.
        with patch("sora_commander_next.jobs.subprocess.Popen",
                   side_effect=AssertionError("duplicate execution forbidden")) as retry:
            duplicate = self.host.run_command(background=True, **payload(request))
        retry.assert_not_called()
        self.assertEqual(duplicate, {
            "request_id": request.request_id, "status": "DENY_DUPLICATE_UNKNOWN",
            "error": "LaunchOutcomeUnknown", "decision": "AUTO",
        })
        self.assertEqual(self.host.ping()["status"], "OK")

    def test_malformed_background_and_public_status_do_not_leak_commands(self):
        for value in (1, "true", None, []):
            self.assertEqual(self.host.run_command(background=value, **payload(self.request()))["status"], "ERROR")
        self.pending(self.request("print('SECRET_ARGUMENT')"))
        self.assertNotIn("SECRET_ARGUMENT", str(self.host.status()))
        self.assertFalse(hasattr(self.host, "approve_local"))

    def test_tkinter_import_has_no_window_or_automatic_approval(self):
        fake = ModuleType("tkinter")
        fake.Tk = Mock(side_effect=AssertionError("window must not open"))
        with patch.dict(sys.modules, {"tkinter": fake}):
            import sora_commander_next.owner_ui as module
            importlib.reload(module)
        fake.Tk.assert_not_called()
        self.assertFalse(self.jobs._jobs)

    def test_ui_button_invokes_only_selected_saved_request(self):
        pending = self.pending()
        self.assertFalse(self.jobs._jobs)
        view = SimpleNamespace(controller=self.controller,
                               records=self.controller.pending_local(),
                               items=Mock(curselection=Mock(return_value=(0,))), result=Mock())
        OwnerView._act(view, False)
        view.result.set.assert_called_once_with("REJECTED")
        self.assertEqual(self.controller.approve_local(pending)["status"], "ERROR")

    def test_fake_adapter_reconnect_registers_no_mutating_approval_or_replay(self):
        registrations = []
        class FakeFastMCP:
            def __init__(self, name):
                self.methods = {}
                registrations.append(self.methods)
            def tool(self):
                def register(func):
                    self.methods[func.__name__] = func
                    return func
                return register
        fake = ModuleType("mcp.server.fastmcp")
        fake.FastMCP = FakeFastMCP
        modules = {"mcp": ModuleType("mcp"), "mcp.server": ModuleType("mcp.server"), "mcp.server.fastmcp": fake}
        with patch.dict(sys.modules, modules):
            build_server(self.host)
            request = self.request("import time; time.sleep(.2); print('once')")
            self.grant(request)
            with patch("sora_commander_next.jobs.subprocess.Popen", wraps=subprocess.Popen) as spawn:
                started = registrations[0]["run_command"](request.executable, list(request.argv), request.cwd, background=True)
                build_server(self.host)  # reconnect only reconstructs the adapter
                result = self.finish(started["session_id"])
                again = registrations[1]["read_process_output"](started["session_id"])
                self.assertEqual(again, result)
                self.assertEqual(spawn.call_count, 1)
        for registered in registrations:
            self.assertEqual(set(registered), {
                "run_command", "run_command_wait", "await_command_result",
                "read_process_output", "await_process_output", "stop_process",
                "stop_process_wait", "ping", "status", "list_folder",
                "read_image_file", "read_text_file", "review_status", "preview_text_edit",
                "list_sessions", "previous_sessions", "recovery_overview", "request_status", "previous_output",
                # Ten bounded reads, two non-writing previews, local catalog only.
                "list_folder_page", "read_text_page", "find_files", "search_code",
                "path_info", "read_text_lines", "read_many_text_files",
                "compare_text_files", "read_text_chunk", "sha256_file", "git_snapshot", "git_diff", "git_log", "git_show", "git_remote_check", "list_tasks", "run_task", "ogf_cue",
                "preview_source_create", "preview_source_patch", "tool_catalog",
                "create_directory", "create_file", "move_file", "request_source_patch_dev",
                "list_processes", "get_config", "send_input",
            })
            self.assertFalse(set(registered) & {
                "approve", "reject", "approve_local", "reject_local",
                "request_source_create", "request_source_patch", "source_review_status",
                "cancel_source_review", "request_text_edit", "request_text_edit_wait",
                "edit_review_status", "await_edit_result", "request_text_create",
                "request_text_create_wait",
            })
        fresh = Host(self.policy)
        self.addCleanup(fresh.jobs.close)
        self.assertEqual(fresh.read_process_output(started["session_id"])["error"], "UnknownSession")
        self.assertFalse(fresh.jobs._jobs)

    def test_teardown_cancels_all_and_closes_manager(self):
        session = self.start("import time; time.sleep(5)")
        self.jobs.close()
        self.assertEqual(self.host.read_process_output(session)["status"], "CANCELLED")
        self.assertFalse(self.jobs._jobs[session].thread.is_alive())
        self.assertEqual(self.host.run_command(background=True, **payload(Command(sys.executable, ("--version",), str(ROOT))))["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()
