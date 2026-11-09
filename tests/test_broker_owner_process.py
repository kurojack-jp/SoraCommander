"""Owner process composition: real Windows lease, local reads, inert startup.

Only fixed independent Python fixtures run. No listener, transport, Owner UI,
ACL mutation, journal, ledger, installed runtime or remote command is used.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if os.name == "nt":
    from sora_commander_next import broker_owner_process as core
    from sora_commander_next import broker_owner_lease as lease_module
    from sora_commander_next import broker_runtime as runtime_module
    from sora_commander_next import jobs as jobs_module
    from sora_commander_next import windows_owner as owner
    from sora_commander_next.commands import Command


RID, SID = "a" * 32, "b" * 48
WIRE = json.dumps({"version": 2, "op": "read_result",
                   "request_id": RID, "offset": 0}).encode("ascii")
_CHILD = '''import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from sora_commander_next.broker_owner_process import BrokerOwnerProcess, JobManager
from sora_commander_next.broker_owner_lease import BrokerOwnerLeaseError
with patch("sora_commander_next.broker_owner_process.JobManager", wraps=JobManager) as factory:
    try:
        with BrokerOwnerProcess(sys.executable, sys.argv[2]):
            assert factory.call_count == 1
            print("ACQUIRED")
    except BrokerOwnerLeaseError as exc:
        assert str(exc) in ("Owner mutex name already exists", "Owner mutex creation denied")
        factory.assert_not_called()
        print("DENIED_AT_KERNEL")
'''


@unittest.skipUnless(os.name == "nt", "actual Windows required")
class BrokerOwnerProcessTests(unittest.TestCase):
    def candidate(self, **kwargs):
        candidate = core.BrokerOwnerProcess(str(Path(sys.executable).absolute()),
                                           str(ROOT), **kwargs)
        self.addCleanup(candidate.close)
        return candidate

    def enter(self):
        candidate = self.candidate()
        candidate.__enter__()
        return candidate

    def bind_status(self, candidate):
        # Synthetic retained correlation only: no approval/admission/launch.
        candidate._runtime.sessions[RID] = SID
        return {"request_id": RID, "session_id": SID, "status": "RUNNING",
                "argv": ["private"], "pid": 123, "cwd": "private",
                "stdout": "private", "exit_code": None}

    def assert_reads_denied(self, candidate):
        for call in (lambda: candidate.query_status_local(RID),
                     lambda: candidate.read_result_page_wire_local(WIRE)):
            with self.assertRaises(PermissionError):
                call()

    def test_import_and_construct_do_not_create_authority_or_jobs(self):
        with patch.object(lease_module, "BrokerOwnerLease") as lease, \
                patch.object(jobs_module, "JobManager") as manager, \
                patch.object(runtime_module, "BrokerRuntime") as runtime, \
                patch.object(subprocess, "Popen") as spawn:
            spec = importlib.util.spec_from_file_location(
                "sora_commander_next._owner_process_import_test", core.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            candidate = module.BrokerOwnerProcess(sys.executable, str(ROOT))
            candidate.close()
            for factory in (lease, manager, runtime, spawn):
                factory.assert_not_called()

    def test_entry_owns_one_real_manager_exact_guard_and_sid_without_launch(self):
        candidate = self.candidate(capacity=2)
        self.assert_reads_denied(candidate)
        with patch.object(core, "JobManager", wraps=jobs_module.JobManager) as factory, \
                patch.object(jobs_module.JobManager, "start") as start, \
                patch.object(subprocess, "Popen") as spawn:
            with candidate:
                factory.assert_called_once_with(max_concurrent=2)
                self.assertIs(type(candidate._manager), jobs_module.JobManager)
                self.assertIs(candidate._runtime.manager, candidate._manager)
                self.assertIs(candidate._runtime.guard, candidate._lease._guard)
                self.assertIs(type(candidate._guard), owner.OwnerSessionGuard)
                self.assertEqual(candidate._runtime.admission.owner_binding,
                                 candidate._lease._identity.sid)
                self.assertEqual(candidate._manager._jobs, {})
                self.assertEqual(candidate._runtime.sessions, {})
                self.assertEqual(candidate._runtime.admission.active_ids_local(), ())
                self.assertIsNone(candidate._manager.journal_path)
                self.assertIsNone(candidate._manager.output_archive_path)
            start.assert_not_called()
            spawn.assert_not_called()

    def test_real_same_process_denied_without_second_manager_then_reacquire(self):
        with self.candidate():
            second = self.candidate()
            with patch.object(core, "JobManager") as factory:
                with self.assertRaises(lease_module.BrokerOwnerLeaseError):
                    second.__enter__()
                factory.assert_not_called()
        with self.candidate():
            pass

    def test_real_independent_child_denied_at_kernel_then_acquires(self):
        def run_child():
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", _CHILD,
                 str(ROOT / "src"), str(ROOT)], cwd=str(ROOT),
                capture_output=True, text=True, timeout=15, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        with self.candidate():
            self.assertEqual(run_child(), "DENIED_AT_KERNEL")
        self.assertEqual(run_child(), "ACQUIRED")
        with self.candidate():
            pass

    def test_invalid_explicit_config_rejected_before_lease(self):
        bad_paths = (None, 1, b"C:\\a", "", "relative.exe", "C:a.exe",
                     "\\rooted", "\\\\server\\share\\file.exe",
                     "\\\\?\\C:\\file.exe", "C:\\x:stream", "C:\\..\\x",
                     "C:\\x.\\a", "C:\\x \\a", "C:\\a\0.exe", "C:\\NUL",
                     "C:\\*.exe", "C:\\a\\\\b", "https://example.com/a")
        with patch.object(core, "BrokerOwnerLease") as lease:
            for value in bad_paths:
                with self.subTest(value=repr(value)), self.assertRaises((ValueError, OSError)):
                    core.BrokerOwnerProcess(value, str(ROOT))
                with self.subTest(cwd=repr(value)), self.assertRaises((ValueError, OSError)):
                    core.BrokerOwnerProcess(sys.executable, value)
            lease.assert_not_called()

    def test_wrong_target_type_and_missing_paths_denied(self):
        for exe, cwd in ((str(ROOT), str(ROOT)), (sys.executable, sys.executable),
                         (str(ROOT / "missing-owner-broker.exe"), str(ROOT))):
            with self.subTest(exe=exe, cwd=cwd), self.assertRaises((ValueError, OSError)):
                core.BrokerOwnerProcess(exe, cwd)

    def test_network_drive_and_reparse_alias_denied(self):
        with patch.object(core, "_GetDriveTypeW", return_value=4):
            with self.assertRaises(ValueError):
                core.BrokerOwnerProcess(sys.executable, str(ROOT))
        info = SimpleNamespace(st_file_attributes=core.stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with patch.object(Path, "lstat", return_value=info):
            with self.assertRaises(ValueError):
                core.BrokerOwnerProcess(sys.executable, str(ROOT))

    def test_capacity_and_clock_are_bounded(self):
        for capacity in (True, 0, 17, 1.0, None):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                self.candidate(capacity=capacity)
        with self.assertRaises(ValueError):
            self.candidate(clock=0)
        clock = Mock(return_value=123)
        with self.candidate(capacity=16, clock=clock) as candidate:
            self.assertIs(candidate._runtime.admission.clock, clock)
        clock.assert_not_called()

    def test_no_public_execution_approval_or_runtime_endpoint(self):
        candidate = self.candidate()
        self.assertEqual({name for name in dir(candidate) if not name.startswith("_")},
                         {"close", "query_status_local", "read_result_page_wire_local"})

    def test_constructor_path_is_revalidated_before_acquisition(self):
        candidate = self.candidate()
        with patch.object(core, "_local_path", side_effect=ValueError("changed")), \
                patch.object(core, "BrokerOwnerLease") as lease, \
                patch.object(core, "JobManager") as manager:
            with self.assertRaises(ValueError):
                candidate.__enter__()
            lease.assert_not_called()
            manager.assert_not_called()
        self.assert_reads_denied(candidate)

    def test_entry_and_cleanup_order_including_pinned_verifications(self):
        events = []
        actual_lease = lease_module.BrokerOwnerLease()
        actual_manager = jobs_module.JobManager()
        original_verify = actual_lease._verify
        original_close = actual_manager.close

        def verify():
            events.append("verify")
            original_verify()

        def make_manager(**kwargs):
            self.assertIsNotNone(actual_lease._handle)
            events.append("manager.new")
            return actual_manager

        def make_runtime(**kwargs):
            events.append("runtime.new")
            self.assertIs(kwargs["owner_guard"], actual_lease._guard)
            self.assertEqual(kwargs["owner_binding"], actual_lease._identity.sid)
            return runtime_module.BrokerRuntime(**kwargs)

        def manager_close():
            self.assertIsNotNone(actual_lease._handle)
            events.append("manager.close")
            original_close()

        original_lease_enter = actual_lease.__enter__
        def enter_lease():
            events.append("lease.enter")
            return original_lease_enter()

        with patch.object(core, "BrokerOwnerLease", return_value=actual_lease), \
                patch.object(actual_lease, "__enter__", side_effect=enter_lease), \
                patch.object(actual_lease, "_verify", side_effect=verify), \
                patch.object(core, "JobManager", side_effect=make_manager), \
                patch.object(core, "BrokerRuntime", side_effect=make_runtime), \
                patch.object(actual_manager, "close", side_effect=manager_close), \
                patch.object(actual_lease, "close", wraps=actual_lease.close) as release:
            with self.candidate():
                events.append("active")
            release.assert_called_once_with()
            self.assertIsNone(actual_lease._handle)
        self.assertLess(events.index("lease.enter"), events.index("manager.new"))
        self.assertLess(events.index("manager.new"), events.index("runtime.new"))
        self.assertEqual(events[events.index("manager.new") + 1], "verify")
        self.assertEqual(events[events.index("runtime.new") + 1], "verify")
        self.assertEqual(events[-2:], ["active", "manager.close"])

    def test_manager_constructor_failure_releases_lease(self):
        candidate = self.candidate()
        with patch.object(core, "JobManager", side_effect=RuntimeError("manager failure")):
            with self.assertRaisesRegex(RuntimeError, "manager failure"):
                candidate.__enter__()
        self.assertTrue(candidate._closed)
        with self.candidate():
            pass

    def test_runtime_constructor_failure_closes_manager_before_lease(self):
        actual_manager = jobs_module.JobManager()
        candidate = self.candidate()
        def close_manager():
            self.assertIsNotNone(candidate._lease._handle)
            jobs_module.JobManager.close(actual_manager)
        with patch.object(core, "JobManager", return_value=actual_manager), \
                patch.object(actual_manager, "close", side_effect=close_manager) as close, \
                patch.object(core, "BrokerRuntime", side_effect=RuntimeError("runtime failure")):
            with self.assertRaisesRegex(RuntimeError, "runtime failure"):
                candidate.__enter__()
            close.assert_called_once_with()
        self.assertTrue(candidate._closed)
        with self.candidate():
            pass

    def test_lock_after_acquisition_prevents_manager_construction(self):
        candidate = self.candidate()
        original_enter = lease_module.BrokerOwnerLease.__enter__
        def enter_then_lock(lease):
            result = original_enter(lease)
            lease._verify = Mock(side_effect=PermissionError("locked"))
            return result
        with patch.object(lease_module.BrokerOwnerLease, "__enter__", enter_then_lock), \
                patch.object(core, "JobManager") as manager:
            with self.assertRaises(PermissionError):
                candidate.__enter__()
            manager.assert_not_called()
        self.assertTrue(candidate._closed)

    def test_lock_after_runtime_construction_cleans_all_resources(self):
        candidate = self.candidate()
        manager = jobs_module.JobManager()
        def runtime_then_lock(**kwargs):
            result = runtime_module.BrokerRuntime(**kwargs)
            candidate._lease._verify = Mock(side_effect=PermissionError("locked"))
            return result
        with patch.object(core, "JobManager", return_value=manager), \
                patch.object(manager, "close", wraps=manager.close) as close, \
                patch.object(core, "BrokerRuntime", side_effect=runtime_then_lock):
            with self.assertRaises(PermissionError):
                candidate.__enter__()
            close.assert_called_once_with()
        self.assertTrue(candidate._closed)

    def test_context_body_exception_propagates_and_releases(self):
        candidate = self.candidate()
        with self.assertRaisesRegex(RuntimeError, "body"):
            with candidate:
                raise RuntimeError("body")
        self.assertTrue(candidate._closed)
        self.assert_reads_denied(candidate)

    def test_single_use_idempotent_close_and_close_before_entry(self):
        candidate = self.enter()
        manager = candidate._manager
        runtime = candidate._runtime
        with patch.object(manager, "close", wraps=manager.close) as close, \
                patch.object(runtime, "close", wraps=runtime.close) as runtime_close:
            candidate.close()
            candidate.close()
            close.assert_called_once_with()
            runtime_close.assert_called_once_with()
        with self.assertRaises(PermissionError):
            candidate.__enter__()
        self.assert_reads_denied(candidate)
        before = self.candidate()
        before.close()
        with self.assertRaises(PermissionError):
            before.__enter__()

    def test_wrong_thread_reads_and_close_denied_owner_thread_can_cleanup(self):
        candidate = self.enter()
        errors = []
        def worker():
            for call in (candidate.close, lambda: candidate.query_status_local(RID),
                         lambda: candidate.read_result_page_wire_local(WIRE)):
                try:
                    call()
                except PermissionError:
                    errors.append("denied")
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["denied"] * 3)
        self.assertIsNotNone(candidate._lease._handle)
        candidate.close()

    def test_read_only_status_is_exact_metadata_and_never_reads_output(self):
        with self.candidate() as candidate:
            snapshot = self.bind_status(candidate)
            with patch.object(candidate._manager, "request_status_readonly", return_value=snapshot) as read, \
                    patch.object(candidate._manager, "read", side_effect=AssertionError("output read")), \
                    patch.object(candidate._manager, "start", side_effect=AssertionError("launch")):
                self.assertEqual(candidate.query_status_local(RID),
                                 {"request_id": RID, "status": "RUNNING"})
                read.assert_called_once_with(RID)

    def test_status_unknown_and_invalid_ids_never_consult_manager(self):
        with self.candidate() as candidate:
            with patch.object(candidate._manager, "request_status_readonly") as read:
                self.assertEqual(candidate.query_status_local(RID),
                                 {"request_id": RID, "status": "UNKNOWN"})
                for value in (None, True, [], {}, "", "a" * 31, "A" * 32, "x" * 32):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        candidate.query_status_local(value)
                read.assert_not_called()

    def test_status_malformed_mismatched_and_unknown_values_fail_closed(self):
        with self.candidate() as candidate:
            good = self.bind_status(candidate)
            for snapshot in (None, [], {}, {**good, "request_id": "c" * 32},
                             {**good, "session_id": "c" * 48},
                             {**good, "status": "private-path"},
                             {**good, "status": True}, {**good, "session_id": 1}):
                with self.subTest(snapshot=snapshot), patch.object(
                        candidate._manager, "request_status_readonly", return_value=snapshot):
                    self.assertEqual(candidate.query_status_local(RID),
                                     {"request_id": RID, "status": "OUTCOME_UNKNOWN"})

    def test_status_changed_session_during_read_and_manager_error_fail_closed(self):
        with self.candidate() as candidate:
            snapshot = self.bind_status(candidate)
            def changed(request_id):
                candidate._runtime.sessions[RID] = "c" * 48
                return snapshot
            with patch.object(candidate._manager, "request_status_readonly", side_effect=changed):
                self.assertEqual(candidate.query_status_local(RID)["status"], "OUTCOME_UNKNOWN")
            with patch.object(candidate._manager, "request_status_readonly", side_effect=OSError("private")):
                self.assertEqual(candidate.query_status_local(RID)["status"], "OUTCOME_UNKNOWN")

    def test_owner_lock_before_reads_never_consults_manager(self):
        with self.candidate() as candidate:
            with patch.object(owner, "session_is_unlocked", return_value=False), \
                    patch.object(candidate._manager, "request_status_readonly") as status, \
                    patch.object(candidate._manager, "read_live_page") as page:
                self.assert_reads_denied(candidate)
                status.assert_not_called()
                page.assert_not_called()

    def test_owner_lock_during_status_never_discloses_metadata(self):
        with self.candidate() as candidate:
            snapshot = self.bind_status(candidate)
            with patch.object(owner, "session_is_unlocked", return_value=True) as unlocked:
                def lock(request_id):
                    unlocked.return_value = False
                    return snapshot
                with patch.object(candidate._manager, "request_status_readonly", side_effect=lock):
                    with self.assertRaises(PermissionError):
                        candidate.query_status_local(RID)

    def test_changed_pinned_identity_or_guard_denies_reads(self):
        with self.candidate() as candidate:
            original = candidate._guard
            candidate._guard = owner.OwnerSessionGuard()
            self.assert_reads_denied(candidate)
            candidate._guard = original
            identity = candidate._identity
            candidate._identity = None
            self.assert_reads_denied(candidate)
            candidate._identity = identity

    def test_wire_read_delegates_to_exact_runtime_and_rechecks_owner(self):
        with self.candidate() as candidate:
            with patch.object(candidate._runtime, "read_result_page_wire_local", return_value=b"bounded") as read:
                self.assertEqual(candidate.read_result_page_wire_local(WIRE), b"bounded")
                read.assert_called_once_with(WIRE)
            with patch.object(owner, "session_is_unlocked", return_value=True) as unlocked:
                def lock(raw):
                    unlocked.return_value = False
                    return b"must not escape"
                with patch.object(candidate._runtime, "read_result_page_wire_local", side_effect=lock):
                    with self.assertRaises(PermissionError):
                        candidate.read_result_page_wire_local(WIRE)

    def test_real_runtime_wire_unknown_and_malformed_requests_never_launch(self):
        with self.candidate() as candidate:
            with patch.object(candidate._manager, "start") as start, \
                    patch.object(candidate._manager, "read_live_page") as page:
                response = json.loads(candidate.read_result_page_wire_local(WIRE))
                self.assertEqual(response["request_id"], RID)
                self.assertEqual(response["status"], "UNKNOWN")
                for raw in (None, "{}", b"", b"{}", b"x" * 65537,
                            WIRE.replace(b'"offset": 0', b'"offset": true')):
                    with self.subTest(raw_type=type(raw)), self.assertRaises(ValueError):
                        candidate.read_result_page_wire_local(raw)
                start.assert_not_called()
                page.assert_not_called()

    def test_owner_lock_during_real_wire_snapshot_never_discloses_bytes(self):
        with self.candidate() as candidate:
            self.bind_status(candidate)
            with patch.object(owner, "session_is_unlocked", return_value=True) as unlocked:
                def lock(session, offset):
                    unlocked.return_value = False
                    return None
                with patch.object(candidate._manager, "read_live_page", side_effect=lock) as page:
                    with self.assertRaises(PermissionError):
                        candidate.read_result_page_wire_local(WIRE)
                    page.assert_called_once_with(SID, 0)

    def test_shutdown_during_reads_prevents_returning_data(self):
        for operation in ("status", "wire"):
            with self.subTest(operation=operation), self.candidate() as candidate:
                snapshot = self.bind_status(candidate)
                if operation == "status":
                    def close_during_read(request_id):
                        candidate.close()
                        return snapshot
                    with patch.object(candidate._manager, "request_status_readonly", side_effect=close_during_read):
                        with self.assertRaises(PermissionError):
                            candidate.query_status_local(RID)
                else:
                    def close_during_wire(raw):
                        candidate.close()
                        return b"must not escape"
                    with patch.object(candidate._runtime, "read_result_page_wire_local", side_effect=close_during_wire):
                        with self.assertRaises(PermissionError):
                            candidate.read_result_page_wire_local(WIRE)

    def test_manager_close_failure_retains_lease_and_disables_all_reads(self):
        candidate = self.enter()
        actual_lease = candidate._lease
        manager = candidate._manager
        with patch.object(manager, "close", side_effect=OSError("close failed")) as close, \
                patch.object(actual_lease, "close", wraps=actual_lease.close) as release:
            with self.assertRaises(OSError):
                candidate.close()
            self.assert_reads_denied(candidate)
            with self.assertRaises(PermissionError):
                candidate.close()
            close.assert_called_once_with()
            release.assert_not_called()
            self.assertIsNotNone(actual_lease._handle)
            with self.assertRaises(lease_module.BrokerOwnerLeaseError):
                self.candidate().__enter__()
        # Test-only recovery after an injected failure with zero real children.
        manager.close()
        candidate._manager_close_failed = False
        candidate.close()

    def test_unconfirmed_children_retain_lease_until_later_confirmation(self):
        candidate = self.enter()
        manager, actual_lease = candidate._manager, candidate._lease
        job = SimpleNamespace(thread=Mock(), done=Mock(), child=Mock(),
                              status="CANCELLED", tree=SimpleNamespace(_handle=None),
                              cancel=threading.Event())
        job.thread.is_alive.return_value = True
        job.done.is_set.return_value = True
        job.child.poll.return_value = 1
        manager._jobs[SID] = job
        with patch.object(manager, "close", wraps=manager.close) as close, \
                patch.object(actual_lease, "close", wraps=actual_lease.close) as release:
            with self.assertRaises(PermissionError):
                candidate.close()
            release.assert_not_called()
            self.assert_reads_denied(candidate)
            job.thread.is_alive.return_value = False
            candidate.close()
            close.assert_called_once_with()
            release.assert_called_once_with()

    def test_incomplete_shutdown_evidence_never_releases_lease(self):
        for defect in ("undone", "running_child", "unknown_outcome", "open_tree",
                       "missing_tree", "uncertain_spawn", "retired_job"):
            with self.subTest(defect=defect):
                candidate = self.enter()
                manager = candidate._manager
                job = SimpleNamespace(thread=Mock(), done=Mock(), child=Mock(),
                                      status="CANCELLED", tree=SimpleNamespace(_handle=None),
                                      cancel=threading.Event())
                job.thread.is_alive.return_value = False
                job.done.is_set.return_value = defect != "undone"
                job.child.poll.return_value = None if defect == "running_child" else 1
                if defect == "unknown_outcome":
                    job.status = "OUTCOME_UNKNOWN"
                if defect == "open_tree":
                    job.tree._handle = 123
                if defect == "missing_tree":
                    job.tree = None
                if defect == "uncertain_spawn":
                    # Current JobManager records an uncertain phase by request ID.
                    manager._uncertain_request_ids[RID] = "spawning"
                if defect == "retired_job":
                    manager._retired_request_ids.add(RID)
                    manager._retired_sessions[RID] = SID
                manager._jobs[SID] = job
                with self.assertRaises(PermissionError):
                    candidate.close()
                self.assertIsNotNone(candidate._lease._handle)
                self.assert_reads_denied(candidate)
                # Remove synthetic evidence only; no real child/handle exists.
                manager._jobs.clear()
                manager._uncertain_request_ids.clear()
                manager._retired_request_ids.clear()
                manager._retired_sessions.clear()
                candidate.close()

    def test_real_fixed_long_running_child_stops_before_lease_release(self):
        candidate = self.enter()
        manager, actual_lease = candidate._manager, candidate._lease
        # Manually exercise local lifecycle only. No Owner grant is synthesized
        # and no start/approval endpoint is added to the production context.
        result = manager.start(Command(
            sys.executable, ("-I", "-S", "-B", "-c", "import time; time.sleep(30)"),
            str(ROOT), timeout=40, output_limit=128, request_id=RID))
        self.assertEqual(result["status"], "RUNNING")
        job = manager._jobs[result["session_id"]]
        self.assertIsNone(job.child.poll())
        self.bind_status(candidate)
        candidate._runtime.sessions[RID] = result["session_id"]
        self.assertEqual(candidate.query_status_local(RID)["status"], "RUNNING")
        page = json.loads(candidate.read_result_page_wire_local(WIRE))
        self.assertEqual(page["request_id"], RID)
        self.assertEqual(page["session_id"], result["session_id"])
        self.assertEqual(page["status"], "RUNNING")
        original_release = actual_lease.close
        def release():
            self.assertFalse(job.thread.is_alive())
            self.assertTrue(job.done.is_set())
            self.assertIsNotNone(job.child.poll())
            self.assertIsNone(job.tree._handle)
            original_release()
        with patch.object(manager, "close", wraps=manager.close) as close, \
                patch.object(actual_lease, "close", side_effect=release) as lease_close:
            candidate.close()
            close.assert_called_once_with()
            lease_close.assert_called_once_with()
        self.assert_reads_denied(candidate)
        with self.candidate():
            pass

    def test_explicit_core_inventory(self):
        from sora_commander_next.release_inventory import MODULES, DEV_ONLY_MODULES
        self.assertIn("broker_owner_process.py", MODULES)
        self.assertNotIn("broker_owner_process.py", DEV_ONLY_MODULES)


if __name__ == "__main__":
    unittest.main()
