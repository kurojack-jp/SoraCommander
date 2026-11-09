"""Synthetic Owner/record/process fixtures; never launch, approve or repair."""
import ast
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next import codex_jobs as records
from sora_commander_next.broker_host import BrokerHost, UnavailableTransport
from sora_commander_next.host import Host
from sora_commander_next.recovery_overview import describe_recovery_sessions
from sora_commander_next.release_inventory import MODULES
from test_next_codex_jobs import JOB, record

ROOT = Path(__file__).resolve().parents[1]


class CodexHostObservationTests(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.addCleanup(self.host.close)
        self.guard = Mock()
        self.guard_type = Mock(return_value=self.guard)
        self.enterContext(patch.dict(sys.modules, {
            "sora_commander_next.windows_owner": SimpleNamespace(
                OwnerSessionGuard=self.guard_type)}))
        self.base = ROOT / "temp" / "synthetic-owner-storage"
        self.enterContext(patch.dict(os.environ, LOCALAPPDATA=str(self.base)))
        self.read = self.enterContext(patch.object(records, "_read_record", return_value=record()))
        self.probe = self.enterContext(patch.object(records, "_process_identity", return_value="MISSING"))
        # Any execution/approval/state mutation is a test failure. The record
        # and process readers above are pure fixtures, including on Windows.
        self.blocked = []
        for target in ("subprocess.Popen", "sora_commander_next.jobs.JobManager.start",
                       "sora_commander_next.approval.LocalApproval.create",
                       "sora_commander_next.session_journal.write_snapshot",
                       "sora_commander_next.offline_result_archive.write_result_checkpoint"):
            self.blocked.append(self.enterContext(patch(target, side_effect=AssertionError(target))))

    def tearDown(self):
        for blocked in self.blocked:
            blocked.assert_not_called()

    def observe(self, job_id=JOB, host=None):
        result = (host or self.host).query_codex_job(job_id)
        self.assertFalse(result["supervision_verified"])
        self.assertFalse(result["task_acceptance_verified"])
        self.assertFalse(result["auto_retry"])
        self.assertFalse(result["state_mutated"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertLess(len(json.dumps(result)), 1024)
        return result

    def test_exact_id_and_fixed_internally_derived_storage(self):
        result = self.observe()
        self.assertEqual(result["job_id"], JOB)
        self.read.assert_called_once_with(self.base / "CodexJobNotify" / "jobs" / (JOB + ".json"))
        self.assertEqual(self.guard.verify.call_count, 3)

    def test_invalid_ids_fail_before_owner_or_record_access(self):
        for value in (None, True, 1, {}, [], "", "A" * 32, "a" * 31,
                      "a" * 33, JOB + "\n", "../" + JOB, "x" * 100000):
            self.assertEqual(self.observe(value)["error"], "InvalidJobId")
        self.guard_type.assert_not_called()
        self.read.assert_not_called()
        self.probe.assert_not_called()

    def test_missing_argument_or_action_arguments_never_read(self):
        for args, kwargs in (((), {}), ((JOB,), {"retry": True}),
                             ((JOB,), {"records_path": "SECRET"}),
                             ((JOB,), {"approve": True}), ((JOB,), {"stop": True})):
            with self.assertRaises(TypeError):
                self.host.query_codex_job(*args, **kwargs)
        self.read.assert_not_called()

    def test_missing_record_is_unknown_and_never_launch_permission(self):
        self.read.side_effect = FileNotFoundError("SECRET")
        result = self.observe()
        self.assertEqual((result["status"], result["error"]), ("UNKNOWN", "RecordNotFound"))

    def test_malformed_record_and_read_errors_are_sanitized(self):
        for value in (None, {}, {**record(), "status": []}, {**record(), "exitCode": 0}):
            self.read.return_value = value
            self.assertEqual(self.observe()["error"], "RecordUnavailableOrInvalid")
        self.probe.assert_not_called()
        for error in (OSError("SECRET"), RuntimeError("SECRET"), records.RecordError("SECRET")):
            self.read.side_effect = error
            self.assertEqual(self.observe()["status"], "UNAVAILABLE")

    def test_owner_denial_before_read_and_after_snapshot(self):
        self.guard.verify.side_effect = PermissionError("SECRET")
        self.assertEqual(self.observe()["error"], "OwnerOrReadAccessRequired")
        self.read.assert_not_called()
        self.guard.verify.side_effect = [None, None, PermissionError("SECRET")]
        self.assertEqual(self.observe()["error"], "OwnerOrReadAccessRequired")
        self.read.assert_called_once()

    def test_pinned_reader_rechecks_owner_without_rebinding(self):
        self.observe()
        reader = self.host._codex_job_reader
        self.guard.verify.side_effect = PermissionError("SECRET changed logon")
        self.assertEqual(self.observe()["status"], "UNAVAILABLE")
        self.assertIs(reader, self.host._codex_job_reader)
        self.guard_type.assert_called_once_with()
        self.read.assert_called_once()

    def test_storage_is_pinned_and_invalid_local_base_is_unavailable(self):
        self.observe()
        with patch.dict(os.environ, LOCALAPPDATA="relative-SECRET"):
            self.observe()
            fresh = Host()
            self.addCleanup(fresh.close)
            self.assertEqual(self.observe(host=fresh)["status"], "UNAVAILABLE")
        self.assertEqual([call.args[0] for call in self.read.call_args_list],
                         [self.base / "CodexJobNotify" / "jobs" / (JOB + ".json")] * 2)

    def test_owner_guard_construction_denial_is_sanitized(self):
        self.guard_type.side_effect = PermissionError("SECRET")
        self.assertEqual(self.observe()["status"], "UNAVAILABLE")
        self.read.assert_not_called()
        self.probe.assert_not_called()

    def test_pid_reuse_live_and_stale_running_remain_unknown(self):
        for pair in (("MATCH", "MATCH"), ("PID_REUSED", "MATCH"),
                     ("MISSING", "MISSING"), ("MATCH", "EXITED"),
                     ("UNAVAILABLE", "MISSING")):
            self.probe.side_effect = pair
            result = self.observe()
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["recorded_status"], "RUNNING")

    def test_cli_success_never_means_task_acceptance(self):
        self.read.return_value = record("SUCCESS")
        result = self.observe()
        self.assertEqual((result["status"], result["exit_code"]), ("SUCCESS", 0))
        self.assertEqual(result["next_step"], "VERIFY_ARTIFACTS_AND_TESTS")
        self.probe.return_value = "MATCH"
        self.assertEqual(self.observe()["status"], "UNKNOWN")

    def test_reconnect_new_host_reads_same_existing_id_without_replay(self):
        first = self.observe()
        fresh = Host()
        self.addCleanup(fresh.close)
        self.assertEqual(first, self.observe(host=fresh))
        self.assertEqual(fresh.jobs.list_sessions()["count"], 0)
        self.assertEqual(fresh.jobs.request_status_readonly(JOB)["status"], "UNKNOWN")
        self.assertEqual([call.args[0].name for call in self.read.call_args_list], [JOB + ".json"] * 2)

    def test_product_has_no_query_surface_and_transport_stays_unavailable(self):
        broker = BrokerHost(UnavailableTransport())
        self.addCleanup(broker.close)
        self.assertFalse(hasattr(broker, "query_codex_job"))
        self.assertEqual(Host.query_codex_job(broker, JOB)["status"], "UNAVAILABLE")
        self.assertEqual(broker.request_status(JOB)["status"], "UNAVAILABLE")
        self.assertFalse(self.host.status()["remote_ready"])
        self.assertEqual(self.host.status()["approval"], "LOCAL_DEMO_ONLY")
        self.read.assert_not_called()

    def test_mcp_registration_is_held_and_release_inventory_already_covers_dependencies(self):
        tree = ast.parse((ROOT / "src/sora_commander_next/mcp_adapter.py").read_text(encoding="utf-8"))
        self.assertNotIn("query_codex_job", {node.name for node in ast.walk(tree)
                                          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))})
        for name in ("codex_jobs.py", "host.py", "recovery_overview.py"):
            self.assertIn(name, MODULES)


class ManualRecoveryEvidenceTests(unittest.TestCase):
    def overview(self, old=(), live=(), archive=()):
        return describe_recovery_sessions(
            {"sessions": list(old)}, {"sessions": list(live)},
            journal_enabled=False, archive_enabled=bool(archive),
            previous_archive_rows=list(archive))

    def test_restart_empty_history_is_inconclusive(self):
        result = self.overview()
        self.assertEqual(result["count"], 0)
        self.assertFalse(result["auto_retry"])
        self.assertIn("IDS_UNAVAILABLE", result["after_host_restart"])
        self.assertIn("does not prove", result["note"])

    def test_archived_end_is_advisory_and_never_live_acceptance(self):
        sid = "b" * 48
        row = {"session_id": sid, "status": "COMPLETED", "exit_code": 0,
               "output_incomplete": False}
        archive = {"session_id": sid, "request_id": JOB, "status": "COMPLETED",
                   "exit_code": 0, "saved_at": 1}
        result = self.overview(old=[row], archive=[archive])
        observed = result["sessions"][0]
        self.assertEqual(observed["request_id"], JOB)
        self.assertFalse(observed["live_supervision_available"])
        self.assertFalse(observed["task_acceptance_verified"])
        self.assertFalse(observed["auto_retry"])
        orphan = self.overview(archive=[archive])["sessions"][0]
        self.assertEqual(orphan["status"], "OUTCOME_UNKNOWN")
        self.assertFalse(orphan["live_supervision_available"])
        self.assertFalse(orphan["task_acceptance_verified"])


if __name__ == "__main__":
    unittest.main()
