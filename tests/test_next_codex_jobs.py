"""Read-only recovery, synthetic records, real Windows handles; no Codex launch."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import shutil
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next import codex_jobs as m

JOB = "a" * 32
TIME = "2026-10-22T01:00:00.1234567Z"


def record(status="RUNNING"):
    return dict(schemaVersion=2, jobId=JOB, status=status,
                createdAtUtc=TIME, updatedAtUtc=TIME, startedAtUtc=TIME,
                endedAtUtc=None if status in {"STARTING", "RUNNING"} else TIME,
                wrapperProcessId=1001, wrapperProcessStartTimeUtc=TIME,
                codexStarted=True, codexProcessId=1002, codexProcessStartTimeUtc=TIME,
                exitCode=0 if status == "SUCCESS" else None,
                evidence=dict(turnStarted=True, turnCompleted=status == "SUCCESS",
                              turnFailed=status == "FAILED", errorEvent=False,
                              stdoutBytes=100, stdoutLines=2, malformedLines=0,
                              truncatedLines=0, oversizedLines=0),
                diagnostics=dict(cleanupErrorCount=0, stateWriteFailures=0,
                                 childMayStillBeRunning=False),
                task="SECRET_TASK", repo="SECRET_PATH", reasonCode="SECRET_REASON")


class ReconcileTests(unittest.TestCase):
    def reconcile(self, data, observations=("MISSING", "MISSING")):
        before = deepcopy(data)
        with patch.object(m, "_process_identity", side_effect=observations) as probe:
            result = m._reconcile(data, JOB)
        self.assertEqual(data, before)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(result["state_mutated"])
        self.assertFalse(result["supervision_verified"])
        self.assertEqual(probe.call_count, 2 if data["codexStarted"] else 1)
        return result

    def test_stale_running_is_unknown_immediately_without_age_heuristic(self):
        result = self.reconcile(record())
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["recorded_status"], "RUNNING")
        self.assertEqual(result["reason"], "WRAPPER_MISSING")

    def test_live_processes_do_not_prove_containment_or_success(self):
        result = self.reconcile(record(), ("MATCH", "MATCH"))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "SUPERVISION_NOT_PROVEN")

    def test_partial_exit_pid_reuse_and_unavailable_observations(self):
        cases = [("MATCH", "MISSING", "CODEX_MISSING"),
                 ("PID_REUSED", "MATCH", "PROCESS_IDENTITY_MISMATCH"),
                 ("UNAVAILABLE", "MISSING", "PROCESS_OBSERVATION_UNAVAILABLE"),
                 ("EXITED", "MATCH", "WRAPPER_MISSING")]
        for wrapper, codex, reason in cases:
            with self.subTest(wrapper=wrapper, codex=codex):
                result = self.reconcile(record(), (wrapper, codex))
                self.assertEqual((result["status"], result["reason"]), ("UNKNOWN", reason))

    def test_startup_crash_never_becomes_success(self):
        data = record("STARTING")
        data.update(codexStarted=False, codexProcessId=None,
                    codexProcessStartTimeUtc=None, startedAtUtc=None)
        self.assertEqual(self.reconcile(data, ("MISSING",))["status"], "UNKNOWN")

    def test_recorded_success_failure_and_unknown_remain_observations(self):
        for status in ("SUCCESS", "FAILED", "UNKNOWN"):
            with self.subTest(status=status):
                result = self.reconcile(record(status))
                self.assertEqual(result["status"], status)
                self.assertEqual(result["reason"], "RECORDED_TERMINAL")

    def test_terminal_live_or_unreadable_process_is_not_success(self):
        for observations in (("MATCH", "MISSING"), ("MISSING", "MATCH"), ("UNAVAILABLE", "MISSING")):
            self.assertEqual(self.reconcile(record("SUCCESS"), observations)["status"], "UNKNOWN")

    def test_terminal_reused_pid_is_not_control_authority(self):
        self.assertEqual(self.reconcile(record("SUCCESS"), ("PID_REUSED", "EXITED"))["status"], "SUCCESS")

    def test_uncertain_cleanup_and_persistence_fail_closed(self):
        for key in ("cleanupErrorCount", "stateWriteFailures", "childMayStillBeRunning"):
            data = record("SUCCESS")
            data["diagnostics"][key] = True if key == "childMayStillBeRunning" else 1
            self.assertEqual(self.reconcile(data)["status"], "UNKNOWN")

    def test_invalid_wire_types_and_lifecycle_rejected_before_pid_lookup(self):
        cases = [("schemaVersion", True), ("jobId", "b" * 32), ("status", []),
                 ("wrapperProcessId", True), ("codexProcessId", 0),
                 ("codexStarted", 1), ("wrapperProcessStartTimeUtc", None),
                 ("updatedAtUtc", "2026-10-21T01:00:00Z"),
                 ("startedAtUtc", "bad"), ("exitCode", 0),
                 ("endedAtUtc", TIME), ("evidence", None)]
        for key, value in cases:
            with self.subTest(key=key), patch.object(m, "_process_identity") as probe:
                data = record()
                data[key] = value
                with self.assertRaises(m.RecordError):
                    m._reconcile(data, JOB)
                probe.assert_not_called()

    def test_false_success_rejected(self):
        cases = [("exitCode", 1), ("exitCode", False), ("endedAtUtc", None),
                 ("turnCompleted", False), ("turnFailed", True), ("errorEvent", True),
                 ("malformedLines", 1), ("truncatedLines", 1), ("oversizedLines", 1)]
        for key, value in cases:
            with self.subTest(key=key), patch.object(m, "_process_identity") as probe:
                data = record("SUCCESS")
                (data["evidence"] if key in data["evidence"] else data)[key] = value
                with self.assertRaises(m.RecordError):
                    m._reconcile(data, JOB)
                probe.assert_not_called()

    def test_exact_filetime_precision_and_utc_validation(self):
        self.assertEqual(m._ticks(TIME) - m._ticks(TIME.replace("1234567", "1234566")), 1)
        self.assertEqual(m._ticks(TIME), m._ticks(TIME.replace("Z", "+00:00")))
        for value in (None, "2026-02-30T00:00:00Z", "2026-01-01T00:00:00+09:00", "2026-01-01"):
            with self.assertRaises(m.RecordError):
                m._ticks(value)

    def test_cli_errors_are_sanitized_and_non_success_exit(self):
        for error in (PermissionError("SECRET"), m.RecordError("SECRET"), OSError("SECRET")):
            with patch.object(m, "LocalCodexJobs", side_effect=error), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(m.main([JOB]), 3)
            self.assertEqual(json.loads(out.getvalue())["status"], "ERROR")
            self.assertNotIn("SECRET", out.getvalue())


@unittest.skipUnless(os.name == "nt", "Windows handles and Owner guard")
class LocalQueryTests(unittest.TestCase):
    def setUp(self):
        from sora_commander_next import windows_owner
        self.guard_patch = patch.object(windows_owner, "OwnerSessionGuard", autospec=True)
        self.guard = self.guard_patch.start().return_value
        self.addCleanup(self.guard_patch.stop)
        scratch = Path(__file__).resolve().parents[1] / "temp"
        scratch.mkdir(exist_ok=True)
        # Python 3.13's TemporaryDirectory requests a private Windows DACL
        # (mode 0700), excluding the restricted test token on this machine.
        # Ordinary inherited test directories need no ACL modification.
        self.temp_path = scratch / ("codex-query-" + uuid.uuid4().hex)
        self.temp_path.mkdir()
        self.addCleanup(self.cleanup_fixture)
        self.env_patch = patch.dict(os.environ, LOCALAPPDATA=str(self.temp_path))
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.root = self.temp_path / "CodexJobNotify" / "jobs"
        self.root.mkdir(parents=True)
        self.path = self.root / (JOB + ".json")
        self.raw = json.dumps(record()).encode("utf-8")
        self.path.write_bytes(self.raw)
        self.query = m.LocalCodexJobs()

    def cleanup_fixture(self):
        expected = Path(__file__).resolve().parents[1] / "temp"
        if self.temp_path.resolve().parent != expected.resolve():
            raise RuntimeError("unsafe fixture cleanup")
        shutil.rmtree(self.temp_path)

    def test_read_only_query_preserves_existing_record_and_id(self):
        with patch.object(m, "_process_identity", return_value="MISSING"):
            result = self.query.query(JOB)
        self.assertEqual(result["job_id"], JOB)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_owner_denial_prevents_file_access(self):
        self.guard.verify.side_effect = PermissionError("locked")
        with patch.object(m, "_read_record") as read, self.assertRaises(PermissionError):
            self.query.query(JOB)
        read.assert_not_called()

    def test_owner_rechecked_before_result_release(self):
        self.guard.verify.side_effect = [True, PermissionError("changed session")]
        with patch.object(m, "_process_identity", return_value="MISSING"), self.assertRaises(PermissionError):
            self.query.query(JOB)

    def test_invalid_ids_never_read(self):
        for value in ("../x", "A" * 32, "a" * 48, "", None, JOB + ".json"):
            with patch.object(m, "_read_record") as read, self.assertRaises(m.RecordError):
                self.query.query(value)
            read.assert_not_called()

    def test_missing_file_is_not_recreated(self):
        self.path.unlink()
        with self.assertRaises(OSError):
            self.query.query(JOB)
        self.assertFalse(self.path.exists())

    def test_bounded_malformed_duplicate_and_nonfinite_json(self):
        for raw in (b"x" * (m.MAX_RECORD_BYTES + 1), b"{", b"\xff",
                    b'{"jobId":1,"jobId":2}', b'{"n":NaN}', b"[" * 2000):
            with self.subTest(size=len(raw)):
                self.path.write_bytes(raw)
                with self.assertRaises(m.RecordError):
                    self.query.query(JOB)
                self.assertEqual(self.path.read_bytes(), raw)

    def test_hard_link_rejected(self):
        alias = self.root / "alias"
        os.link(self.path, alias)
        with self.assertRaises(m.RecordError):
            self.query.query(JOB)

    def test_read_does_not_allow_concurrent_replace(self):
        with m._snapshot(self.path):
            with self.assertRaises(OSError):
                self.path.rename(self.root / "replacement.json")
        self.assertTrue(self.path.exists())

    def test_parent_cannot_be_renamed_during_snapshot(self):
        with m._snapshot(self.path):
            with self.assertRaises(OSError):
                self.root.rename(self.root.with_name("swapped"))

    def test_junction_ancestor_rejected_without_following_to_record(self):
        import subprocess
        alias = self.temp_path / "alias"
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(self.root)],
                       capture_output=True, check=True, timeout=10)
        try:
            with self.assertRaises(m.RecordError):
                m._read_record(alias / self.path.name)
        finally:
            alias.rmdir()

    def test_reconnect_query_uses_existing_id_and_no_memory_state(self):
        with patch.object(m, "_process_identity", return_value="MISSING"):
            first = self.query.query(JOB)
            second = m.LocalCodexJobs().query(JOB)
        self.assertEqual(first, second)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_real_pid_creation_time_and_reuse_mismatch(self):
        # Independent .NET Get-Process observation of this Python process.
        import subprocess
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                 f"(Get-Process -Id {os.getpid()}).StartTime.ToUniversalTime().ToString('o')"],
                                capture_output=True, text=True, timeout=10, check=True)
        ticks = m._ticks(result.stdout.strip())
        self.assertEqual(m._process_identity(os.getpid(), ticks), "MATCH")
        self.assertEqual(m._process_identity(os.getpid(), ticks + 1), "PID_REUSED")


if __name__ == "__main__":
    unittest.main()
