"""DEV_ONLY read-only recovery status: no restart, no approval, no journal v1 migration."""
import asyncio
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sora_commander_next.commands import Command
from sora_commander_next.host import Host
from sora_commander_next.jobs import JobManager
from sora_commander_next.mcp_adapter import _validate_arguments, build_server
from sora_commander_next.recovery_overview import describe_recovery_sessions
from sora_commander_next.session_journal import write_snapshot
from sora_commander_next.offline_result_archive import write_result_checkpoint

ROOT = Path(__file__).resolve().parents[1]


def entry(sid, status, exit_code=None, output_incomplete=False):
    return {
        "session_id": sid, "status": status, "pid": 4242,
        "exit_code": exit_code, "captured_bytes": 0,
        "output_incomplete": output_incomplete,
    }


class DevInteractiveRecoveryGuidanceTests(unittest.TestCase):
    """Memory-only Host/JobManager integration; no child, file or Owner GUI."""

    def setUp(self):
        self.host = Host()
        self.dev = JobManager()
        self.host._dev_interactive_jobs = self.dev
        self.addCleanup(self.host.close)
        self.spawn = self.enterContext(patch(
            "sora_commander_next.jobs.subprocess.Popen",
            side_effect=AssertionError("unexpected dispatch")))
        self.write = self.enterContext(patch(
            "sora_commander_next.session_journal.write_snapshot",
            side_effect=AssertionError("unexpected persistence")))
        self.live = self.enterContext(patch.object(
            self.dev, "list_sessions", return_value={"sessions": [], "count": 0}))

    def observe(self, status, code=None, incomplete=False):
        self.live.return_value = {
            "sessions": [entry("a" * 48, status, code, incomplete)], "count": 1}
        return self.host.recovery_overview()["sessions"][0]

    def tearDown(self):
        self.spawn.assert_not_called()
        self.write.assert_not_called()

    def test_status_specific_steps_survive_dev_aggregation(self):
        for status, code, incomplete, step in (
            ("RUNNING", None, False, "OBSERVE_EXISTING_SESSION"),
            ("COMPLETED", 0, False, "VERIFY_EFFECTS_THEN_CONTINUE"),
            ("COMPLETED", 0, True, "INSPECT_RESULT_AND_EFFECTS"),
            ("FAILED", 1, False, "INSPECT_RESULT_AND_EFFECTS"),
            ("OUTCOME_UNKNOWN", None, True, "INSPECT_EFFECTS_MANUALLY"),
        ):
            with self.subTest(status=status, incomplete=incomplete):
                row = self.observe(status, code, incomplete)
                self.assertEqual(row["next_step"], step)
                self.assertEqual(row["status"], status)
                self.assertEqual(row["exit_code"], code)
                self.assertEqual(row["output_incomplete"], incomplete)
                self.assertEqual(row["persistence"], "NONE_DEV_INTERACTIVE")
                self.assertFalse(row["recoverable"])
                self.assertFalse(row["auto_retry"])

    def test_running_to_completed_keeps_identity_without_dispatch(self):
        self.dev._request_sessions["synthetic-request"] = "a" * 48
        running = self.observe("RUNNING")
        completed = self.observe("COMPLETED", 0)
        self.assertEqual(running["session_id"], completed["session_id"])
        self.assertEqual(completed["request_id"], "synthetic-request")
        self.assertIn("may continue", completed["guidance"])
        self.assertIn("NO automatic resend", completed["guidance"])
        self.assertNotIn("may continue", running["guidance"])

    def test_unknown_or_incomplete_requires_manual_inspection(self):
        for status, code, incomplete in (
            ("OUTCOME_UNKNOWN", None, True), ("COMPLETED", 0, True),
            ("FAILED", 1, False), ("RUNNING", None, False),
        ):
            row = self.observe(status, code, incomplete)
            self.assertEqual(row["guidance"],
                             "Inspect manually; NO automatic resend.")

    def test_restart_does_not_rehydrate_or_claim_not_started(self):
        self.dev._request_sessions["synthetic-request"] = "a" * 48
        self.observe("COMPLETED", 0)
        fresh = Host()
        fresh._dev_interactive_jobs = JobManager()
        self.addCleanup(fresh.close)
        result = fresh.recovery_overview()
        self.assertEqual(result["count"], 0)
        self.assertIn("does not prove", result["note"])
        self.assertEqual(fresh.jobs.request_status_readonly("synthetic-request")
                         ["status"], "UNKNOWN")
        self.assertEqual(result["dev_interactive"]["after_host_restart"],
                         "IDS_UNAVAILABLE_NO_REPLAY")
        self.assertNotIn("NOT_STARTED", str(result))

    def test_private_archive_stays_hidden_while_dev_guidance_available(self):
        self.host.jobs.output_archive_path = "synthetic-opt-in-no-file"
        self.host.jobs._previous_outputs = {"results": [{
            "request_id": "PRIVATE_SYNTHETIC", "session_id": "b" * 48,
            "chunks": [{"data_base64": "PRIVATE_SYNTHETIC"}],
        }], "count": 1}
        self.observe("COMPLETED", 0)
        result = self.host.recovery_overview()
        self.assertEqual(result["archive_correlation"],
                         "OWNER_AUTHORIZATION_REQUIRED")
        self.assertNotIn("PRIVATE_SYNTHETIC", str(result))
        self.assertEqual(result["sessions"][0]["next_step"],
                         "VERIFY_EFFECTS_THEN_CONTINUE")

    def test_malformed_snapshot_fails_closed_without_exception_details(self):
        self.live.return_value = {"sessions": [{"status": "SYNTHETIC_PRIVATE"}]}
        self.assertEqual(self.host.recovery_overview(), {
            "status": "ERROR", "error": "RecoveryOverviewUnavailable"})

    def test_historical_completion_does_not_change_ram_request_unknown(self):
        self.host.jobs._previous_sessions = {
            "sessions": [entry("b" * 48, "COMPLETED", 0)], "count": 1}
        row = self.host.recovery_overview()["sessions"][0]
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["origin"], "PREVIOUS_PROCESS")
        self.assertEqual(self.host.jobs.request_status_readonly("synthetic-request")
                         ["status"], "UNKNOWN")
        self.assertNotIn("guidance", row)  # Change is confined to DEV rows.


class RecoveryOverviewTests(unittest.TestCase):
    def test_no_history_does_not_mean_no_prior_side_effects(self):
        jobs = JobManager()
        try:
            result = jobs.recovery_overview()
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["journal"], "DISABLED")
            self.assertEqual(result["count"], 0)
            self.assertFalse(result["auto_retry"])
            self.assertIn("does not prove", result["note"])
        finally:
            jobs.close()

    def test_previous_unknown_and_terminal_offer_different_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            write_snapshot(path, [
                entry("a" * 48, "RUNNING"),
                entry("b" * 48, "COMPLETED", 0),
                entry("c" * 48, "FAILED", 1),
                entry("d" * 48, "COMPLETED", 0, True),
            ])
            jobs = JobManager(journal_path=path)
            try:
                host = Host(jobs=jobs)
                result = host.recovery_overview()
                self.assertEqual(result["count"], 4)
                self.assertEqual(result["journal"], "ENABLED")
                self.assertEqual([x["next_step"] for x in result["sessions"]], [
                    "INSPECT_EFFECTS_MANUALLY", "VERIFY_EFFECTS_THEN_CONTINUE",
                    "INSPECT_RESULT_AND_EFFECTS", "INSPECT_RESULT_AND_EFFECTS",
                ])
                self.assertEqual(result["sessions"][0]["status"],
                                 "INTERRUPTED_UNKNOWN")
                self.assertTrue(all(x["origin"] == "PREVIOUS_PROCESS"
                                    for x in result["sessions"]))
                self.assertTrue(all(x["auto_retry"] is False
                                    and not x["command_identified"]
                                    and "request_id" not in x
                                    for x in result["sessions"]))
                self.assertEqual(host.list_sessions()["count"], 0)
                result["sessions"][0]["status"] = "COMPLETED"
                self.assertEqual(host.recovery_overview()["sessions"][0]["status"],
                                 "INTERRUPTED_UNKNOWN")
            finally:
                jobs.close()

    def test_running_and_completed_same_session_without_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            jobs = JobManager(journal_path=path)
            try:
                cmd = Command(sys.executable,
                              ("-c", "import time;time.sleep(.25);print('OK')"),
                              str(ROOT), timeout=3, request_id="a" * 32)
                started = jobs.start(cmd)
                sid = started["session_id"]
                observation = jobs.recovery_overview()
                self.assertEqual(observation["sessions"][0]["session_id"], sid)
                self.assertEqual(observation["sessions"][0]["request_id"],
                                 cmd.request_id)
                self.assertEqual(observation["sessions"][0]["status"], "RUNNING")
                self.assertEqual(observation["sessions"][0]["next_step"],
                                 "OBSERVE_EXISTING_SESSION")
                self.assertFalse(observation["sessions"][0]["auto_retry"])
                self.assertEqual(jobs.wait(sid)["status"], "COMPLETED")
                finished = jobs.recovery_overview()["sessions"][0]
                self.assertEqual(finished["status"], "COMPLETED")
                self.assertEqual(finished["exit_code"], 0)
                self.assertEqual(finished["next_step"],
                                 "VERIFY_EFFECTS_THEN_CONTINUE")
                self.assertFalse(finished["command_identified"])
                self.assertEqual(jobs.request_status_readonly(cmd.request_id)
                                 ["session_id"], sid)
            finally:
                jobs.close()
            # Another invocation can inspect the START/END data but cannot
            # recover the old request ID from journal v1.
            reopened = JobManager(journal_path=path)
            try:
                old = reopened.recovery_overview()["sessions"][0]
                self.assertEqual(old["status"], "COMPLETED")
                self.assertEqual(old["session_id"], sid)
                self.assertNotIn("request_id", old)
                self.assertEqual(reopened.list_sessions()["count"], 0)
            finally:
                reopened.close()

    def test_sdk_exposes_only_a_readonly_tool(self):
        host = Host()
        try:
            server = build_server(host)
            names = {tool.name for tool in server._tool_manager.list_tools()}
            self.assertIn("recovery_overview", names)
            _validate_arguments("recovery_overview", {})
            for extra in ({"command": "unsafe"}, {"request_id": "a" * 32},
                          {"auto_retry": True}):
                with self.assertRaises(ValueError):
                    _validate_arguments("recovery_overview", extra)
            tool_result = asyncio.run(server.call_tool("recovery_overview", {}))
            self.assertIn("READ_ONLY_MANUAL_RECOVERY", str(tool_result))
        finally:
            host.jobs.close()

    def test_sdk_reconnect_observes_interrupted_without_any_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            write_snapshot(path, [entry("f" * 48, "RUNNING")])
            jobs = JobManager(journal_path=path)
            try:
                host = Host(jobs=jobs)
                sdk = build_server(host)
                with patch("sora_commander_next.jobs.subprocess.Popen",
                           side_effect=AssertionError("unexpected replay")) as spawn:
                    _, recovery = asyncio.run(sdk.call_tool("recovery_overview", {}))
                    self.assertEqual(recovery["count"], 1)
                    self.assertEqual(recovery["sessions"][0]["status"],
                                     "INTERRUPTED_UNKNOWN")
                    self.assertEqual(recovery["sessions"][0]["next_step"],
                                     "INSPECT_EFFECTS_MANUALLY")
                    self.assertFalse(recovery["auto_retry"])
                    spawn.assert_not_called()
                self.assertEqual(host.list_sessions()["count"], 0)
            finally:
                jobs.close()

    def test_existing_archive_rejoins_old_request_without_new_log_or_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            archive = Path(tmp) / "owner-output-archive.json"
            sid, rid = "a" * 48, "original-command-request"
            write_snapshot(journal, [entry(sid, "COMPLETED", 0)])
            write_result_checkpoint(archive, {
                "request_id": rid, "session_id": sid,
                "status": "COMPLETED", "exit_code": 0,
                "captured_bytes": 0, "output_incomplete": False,
                "archived_truncated": False, "saved_at": int(time.time()),
                "chunks": [],
            })
            jobs = JobManager(journal_path=journal, output_archive_path=archive)
            try:
                host = Host(jobs=jobs)
                # No Owner => old journal still usable, archive IDs private.
                hidden = host.recovery_overview()
                self.assertEqual(hidden["archive_correlation"],
                                 "OWNER_AUTHORIZATION_REQUIRED")
                self.assertEqual(hidden["count"], 1)
                self.assertNotIn("request_id", hidden["sessions"][0])
                self.assertEqual(hidden["sessions"][0]["next_step"],
                                 "VERIFY_EFFECTS_THEN_CONTINUE")
                # An in-process JobManager cannot authenticate its caller;
                # the public Host is the boundary.
                self.assertNotIn("request_id",
                                 jobs.recovery_overview()["sessions"][0])
                owner_verify = Mock(return_value=True)
                host._owner_review_dispatcher = SimpleNamespace(
                    _controller=SimpleNamespace(_verify=owner_verify))
                with patch("sora_commander_next.jobs.subprocess.Popen",
                           side_effect=AssertionError("unexpected restart")) as spawn:
                    sdk = build_server(host)
                    _, linked = asyncio.run(
                        sdk.call_tool("recovery_overview", {}))
                    row = linked["sessions"][0]
                    self.assertEqual(row["request_id"], rid)
                    self.assertEqual(row["session_id"], sid)
                    self.assertEqual(row["correlation"],
                                     "EXISTING_ARCHIVE_REQUEST_ID")
                    self.assertEqual(linked["identification_limit"],
                                     "ARCHIVE_REQUEST_ID_CORRELATION_ONLY")
                    self.assertEqual(linked["archive_correlation"], "AVAILABLE")
                    self.assertFalse(row["command_identified"])
                    self.assertFalse(linked["auto_retry"])
                    self.assertEqual(owner_verify.call_count, 1)
                    spawn.assert_not_called()
                # A locked Owner may still inspect journal progress but
                # must not receive the archived request/session correlation.
                owner_verify.side_effect = PermissionError("locked")
                locked = host.recovery_overview()
                self.assertEqual(locked["archive_correlation"],
                                 "OWNER_AUTHORIZATION_REQUIRED")
                self.assertNotIn("request_id", locked["sessions"][0])
                self.assertNotIn(rid, str(locked))
            finally:
                jobs.close()

    def test_archive_conflict_and_orphan_never_upgrade_to_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            archive = Path(tmp) / "owner-output-archive.json"
            write_snapshot(journal, [
                entry("b" * 48, "COMPLETED", 0),
                entry("c" * 48, "RUNNING"),
            ])
            for sid, rid, state, code in (
                    ("b" * 48, "bad-outcome", "FAILED", 1),
                    ("c" * 48, "old-running", "COMPLETED", 0),
                    ("d" * 48, "archive-only", "COMPLETED", 0)):
                write_result_checkpoint(archive, {
                    "request_id": rid, "session_id": sid,
                    "status": state, "exit_code": code,
                    "captured_bytes": 0, "output_incomplete": False,
                    "archived_truncated": False, "saved_at": int(time.time()),
                    "chunks": [],
                })
            jobs = JobManager(journal_path=journal, output_archive_path=archive)
            try:
                result = jobs.recovery_overview(include_archive_correlation=True)
                self.assertEqual(result["count"], 3)
                rows = {x["session_id"]: x for x in result["sessions"]}
                conflict = rows["b" * 48]
                self.assertTrue(conflict["evidence_conflict"])
                self.assertEqual(conflict["next_step"], "INSPECT_EFFECTS_MANUALLY")
                unknown = rows["c" * 48]
                self.assertEqual(unknown["status"], "INTERRUPTED_UNKNOWN")
                self.assertEqual(unknown["archive_status"], "COMPLETED")
                self.assertEqual(unknown["next_step"], "INSPECT_EFFECTS_MANUALLY")
                orphan = rows["d" * 48]
                self.assertEqual(orphan["origin"], "ARCHIVE_ONLY")
                self.assertEqual(orphan["status"], "OUTCOME_UNKNOWN")
                self.assertEqual(orphan["archive_status"], "COMPLETED")
                self.assertFalse(orphan["auto_retry"])
                for row in rows.values():
                    self.assertFalse(row["command_identified"])
            finally:
                jobs.close()

    def test_aggregation_rejects_oversized_inputs(self):
        with self.assertRaises(ValueError):
            describe_recovery_sessions(
                {"sessions": [entry("a"*48, "RUNNING")]*257},
                {"sessions": []}, journal_enabled=True, archive_enabled=False)
