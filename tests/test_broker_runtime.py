import unittest
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from sora_commander_next.broker_contract import BoundRequest, BrokerRequestDenied
from sora_commander_next.broker_runtime import BrokerRuntime


class Guard:
    def __init__(self):
        self.allowed = True
        self.calls = 0
    def verify(self):
        self.calls += 1
        if not self.allowed:
            raise PermissionError("Owner locked")


class Manager:
    def __init__(self):
        self.starts = []
        self.closed = False
    def start(self, command):
        self.starts.append(command)
        return {"status": "RUNNING", "request_id": command.request_id,
                "session_id": "c"*48}
    def read(self, session, offset=0):
        return {"status": "RUNNING", "session_id": session,
                "request_id": "a"*32, "offset": offset,
                "next_offset": offset, "exit_code": None,
                "stdout": "", "stderr": ""}
    def request_status_readonly(self, request_id):
        return {"request_id": request_id, "session_id": "c"*48,
                "status": "RUNNING", "exit_code": None,
                "captured_bytes": 0, "output_incomplete": False}
    def wait_for(self, session, wait_seconds):
        return self.read(session)
    def stop(self, session):
        return {"session_id": session, "status": "CANCELLING"}
    def close(self):
        self.closed = True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.guard = Guard()
        self.manager = Manager()
        self.request = BoundRequest("a"*32, "C:/fixed.exe", ("-File", "fixed.ps1"),
                                    "D:/repo", "b"*64, "owner", 1000.0, 180.0, 8192)
        self.runtime = BrokerRuntime(expected_executable=self.request.executable,
                                     expected_cwd=self.request.cwd, owner_binding="owner",
                                     owner_guard=self.guard, clock=lambda: 100.0,
                                     manager=self.manager)

    def start(self):
        return self.runtime.start_approved(self.request,
                    approved_fingerprint=self.request.fingerprint(),
                    current_task_sha256=self.request.task_sha256)

    def test_start_and_query(self):
        self.assertEqual(self.start()["status"], "RUNNING")
        self.assertEqual(self.runtime.query(self.request.request_id)["status"], "RUNNING")
        self.assertEqual(len(self.manager.starts), 1)

    def test_duplicate_cannot_relaunch(self):
        self.start()
        with self.assertRaises(BrokerRequestDenied):
            self.start()
        self.assertEqual(len(self.manager.starts), 1)

    def test_ambiguous_start_ack_never_becomes_a_known_session(self):
        for invalid in (
            {"status": "RUNNING", "request_id": "f"*32,
             "session_id": "c"*48},
            {"status": "RUNNING", "request_id": self.request.request_id,
             "session_id": "bad"},
            {"status": "RUNNING", "request_id": self.request.request_id,
             "session_id": "c"*48, "reused_session": True},
        ):
            with self.subTest(invalid=invalid):
                guard = Guard()
                manager = Manager()
                manager.start = lambda command, result=invalid: result
                runtime = BrokerRuntime(
                    expected_executable=self.request.executable,
                    expected_cwd=self.request.cwd, owner_binding="owner",
                    owner_guard=guard, clock=lambda: 100.0, manager=manager)
                response = runtime.start_approved(
                    self.request,
                    approved_fingerprint=self.request.fingerprint(),
                    current_task_sha256=self.request.task_sha256)
                self.assertEqual(response["status"], "UNKNOWN")
                self.assertEqual(runtime.sessions, {})
                with self.assertRaises(BrokerRequestDenied):
                    runtime.start_approved(
                        self.request,
                        approved_fingerprint=self.request.fingerprint(),
                        current_task_sha256=self.request.task_sha256)

    def test_locked_owner_cannot_start(self):
        self.guard.allowed = False
        with self.assertRaises(PermissionError):
            self.start()
        self.assertEqual(self.manager.starts, [])

    def test_owner_locks_after_admission_before_os_spawn(self):
        # A task consented while the desktop was unlocked cannot be handed
        # to JobManager when its Owner logon changes after admission.
        original = self.runtime.admission.admit
        def revoke_after_admission(*args, **kwargs):
            result = original(*args, **kwargs)
            self.guard.allowed = False
            return result
        self.runtime.admission.admit = revoke_after_admission
        with self.assertRaises(PermissionError):
            self.start()
        self.assertEqual(self.manager.starts, [])
        self.assertEqual(self.runtime.sessions, {})
        self.guard.allowed = True
        with self.assertRaisesRegex(BrokerRequestDenied,
                                    "request already consumed"):
            self.start()
        self.assertEqual(self.manager.starts, [])

    def test_owner_locks_while_job_manager_starts_no_ack(self):
        # A real child may have started at this point. Do not fake an
        # unsuccessful execution or refund approval; retain correlation so
        # the Owner can query the state after returning to their desktop.
        def job_start(command):
            self.manager.starts.append(command)
            self.guard.allowed = False
            return {"status": "RUNNING", "request_id": command.request_id,
                    "session_id": "c" * 48}
        self.manager.start = job_start
        with self.assertRaises(PermissionError):
            self.start()
        self.assertEqual(len(self.manager.starts), 1)
        self.assertEqual(self.runtime.sessions, {self.request.request_id: "c"*48})
        self.guard.allowed = True
        self.assertEqual(self.runtime.query_status(
            self.request.request_id)["status"], "RUNNING")
        with self.assertRaisesRegex(BrokerRequestDenied,
                                    "request already consumed"):
            self.start()
        self.assertEqual(len(self.manager.starts), 1)

    def test_unknown_query(self):
        self.assertIsNone(self.runtime.query("d"*32))

    def test_legacy_query_never_leaks_another_job_result(self):
        self.start()
        for forged in (
            {"request_id": "f"*32, "session_id": "c"*48,
             "status": "COMPLETED", "stdout": "private-other-job"},
            {"request_id": self.request.request_id, "session_id": "d"*48,
             "status": "COMPLETED", "stdout": "private-other-job"},
            {"status": "COMPLETED", "stdout": "private-unbound"},
        ):
            with self.subTest(forged=forged):
                self.manager.read = lambda sid, forged=forged: forged
                self.assertEqual(self.runtime.query(self.request.request_id),
                                 {"request_id": self.request.request_id,
                                  "status": "OUTCOME_UNKNOWN"})
        self.assertEqual(len(self.manager.starts), 1)

    def test_cancel_owned_only(self):
        self.assertFalse(self.runtime.cancel("d"*32))
        self.start()
        self.assertTrue(self.runtime.cancel(self.request.request_id))

    def test_cancel_validates_request_before_stop(self):
        class Text(str):
            pass

        self.start()
        self.manager.stop = Mock(side_effect=AssertionError("unexpected stop"))
        for rid in (None, True, 1, 1.0, [], {}, b"a"*32, "", "a"*31,
                    "a"*33, "A"*32, "g"*32, "a"*31 + "\n", "c"*48,
                    Text(self.request.request_id), "d"*32):
            with self.subTest(rid=rid):
                before = self.guard.calls
                self.assertIs(self.runtime.cancel(rid), False)
                self.assertEqual(self.guard.calls, before + 2)
        self.manager.stop.assert_not_called()
        self.assertEqual(len(self.manager.starts), 1)

    def test_cancel_success_requires_exact_bound_response(self):
        self.start()
        session = self.runtime.sessions[self.request.request_id]
        for status in ("CANCELLING", "CANCELLED"):
            with self.subTest(status=status):
                self.manager.stop = Mock(return_value={
                    "session_id": session, "status": status})
                before = self.guard.calls
                self.assertIs(self.runtime.cancel(self.request.request_id), True)
                self.manager.stop.assert_called_once_with(session)
                self.assertEqual(self.guard.calls, before + 2)
        self.assertEqual(len(self.manager.starts), 1)

    def test_cancel_missing_extra_malformed_and_forged_responses_denied(self):
        class Mapping(dict):
            pass

        class Text(str):
            pass

        self.start()
        session = self.runtime.sessions[self.request.request_id]
        good = {"session_id": session, "status": "CANCELLING"}
        invalid = [None, True, "CANCELLED", [], list(good.items()),
                   Mapping(good), {}, {"status": "CANCELLING"},
                   {"session_id": session},
                   {**good, "request_id": self.request.request_id},
                   {**good, "stdout": "private"},
                   {**good, "status": "CANCELLED", "already_finished": True}]
        invalid += [{**good, "session_id": sid} for sid in (
            "d"*48, self.request.request_id, "", None, 123, [],
            session.upper(), Text(session))]
        invalid += [{**good, "status": status} for status in (
            "COMPLETED", "UNKNOWN", "RUNNING", "FAILED", "TIMEOUT",
            "OUTPUT_LIMIT", "OUTPUT_INCOMPLETE", "OUTCOME_UNKNOWN", "ERROR",
            "cancelled", "CANCELLED\n", None, True, 1, [], Text("CANCELLED"))]
        for result in invalid:
            with self.subTest(result=result):
                self.manager.stop = Mock(return_value=result)
                before = self.guard.calls
                self.assertIs(self.runtime.cancel(self.request.request_id), False)
                self.manager.stop.assert_called_once_with(session)
                self.assertEqual(self.guard.calls, before + 2)
        self.assertEqual(self.runtime.sessions,
                         {self.request.request_id: session})
        self.assertEqual(len(self.manager.starts), 1)

    def test_cancel_manager_exception_returns_false_without_retry(self):
        self.start()
        for error in (KeyError("missing"), RuntimeError("manager failure"),
                      TimeoutError("stop outcome unknown")):
            with self.subTest(error=error):
                self.manager.stop = Mock(side_effect=error)
                before = self.guard.calls
                self.assertIs(self.runtime.cancel(self.request.request_id), False)
                self.manager.stop.assert_called_once_with("c"*48)
                self.assertEqual(self.guard.calls, before + 2)
        with self.assertRaises(BrokerRequestDenied):
            self.start()
        self.assertEqual(len(self.manager.starts), 1)

    def test_cancel_owner_lock_before_call_denies_without_stop(self):
        self.start()
        self.guard.allowed = False
        self.manager.stop = Mock()
        for rid in (self.request.request_id, "d"*32, None, []):
            with self.subTest(rid=rid), self.assertRaises(PermissionError):
                self.runtime.cancel(rid)
        self.manager.stop.assert_not_called()
        self.assertEqual(len(self.manager.starts), 1)

    def test_cancel_owner_lock_during_stop_denies_even_on_manager_error(self):
        self.start()
        session = self.runtime.sessions[self.request.request_id]
        for outcome in ("success", "malformed", "error"):
            with self.subTest(outcome=outcome):
                self.guard.allowed = True

                def stop(sid):
                    self.guard.allowed = False
                    if outcome == "error":
                        raise RuntimeError("stop may already have taken effect")
                    if outcome == "malformed":
                        return None
                    return {"session_id": sid, "status": "CANCELLING"}

                self.manager.stop = Mock(side_effect=stop)
                before = self.guard.calls
                with self.assertRaises(PermissionError):
                    self.runtime.cancel(self.request.request_id)
                self.assertEqual(self.guard.calls, before + 2)
                self.manager.stop.assert_called_once_with(session)
                self.assertEqual(self.runtime.sessions,
                                 {self.request.request_id: session})
                self.guard.allowed = True
                with self.assertRaises(BrokerRequestDenied):
                    self.start()
                self.assertEqual(len(self.manager.starts), 1)

    def test_cancel_session_changed_during_stop_denies_ack(self):
        self.start()

        def stop(session):
            self.runtime.sessions[self.request.request_id] = "d"*48
            return {"session_id": session, "status": "CANCELLING"}

        self.manager.stop = Mock(side_effect=stop)
        self.assertIs(self.runtime.cancel(self.request.request_id), False)
        self.manager.stop.assert_called_once_with("c"*48)
        self.assertEqual(len(self.manager.starts), 1)

    @unittest.skipUnless(os.name == "nt", "real Windows JobManager required")
    def test_real_cancel_waits_for_cancelled_without_duplicate_start(self):
        """One bounded disposable child; synthetic Owner guard, no live UI."""
        from sora_commander_next.jobs import JobManager

        # This fixed child only prints/sleeps; it needs no temporary files.
        cwd = str(Path(__file__).resolve().parents[1])
        manager = JobManager(max_concurrent=1, capacity=2)
        self.addCleanup(manager.close)
        request = BoundRequest(
            "e"*32, sys.executable,
            ("-I", "-S", "-c",
             "import time;print('broker-cancel-child',flush=True);time.sleep(8)"),
            cwd, "b"*64, "owner", 1000.0, 6.0, 4096)
        runtime = BrokerRuntime(
            expected_executable=sys.executable, expected_cwd=cwd,
            owner_binding="owner", owner_guard=self.guard,
            clock=lambda: 100.0, manager=manager)
        with patch.object(manager, "start", wraps=manager.start) as start:
            self.assertEqual(runtime.start_approved(
                request, approved_fingerprint=request.fingerprint(),
                current_task_sha256=request.task_sha256)["status"], "RUNNING")
            session = runtime.sessions[request.request_id]
            deadline = time.monotonic() + 2.5
            result = manager.read(session)
            while ("broker-cancel-child" not in result["stdout"]
                   and result["status"] == "RUNNING"
                   and time.monotonic() < deadline):
                time.sleep(.015)
                result = manager.read(session)
            self.assertIn("broker-cancel-child", result["stdout"])
            self.assertEqual(result["status"], "RUNNING")
            self.assertIs(runtime.cancel(request.request_id), True)
            terminal = manager.wait_for(session, 3)
            self.assertEqual(terminal["status"], "CANCELLED")
            self.assertEqual(terminal["session_id"], session)
            self.assertEqual(terminal["request_id"], request.request_id)
            self.assertIs(runtime.cancel(request.request_id), False)
            with self.assertRaises(BrokerRequestDenied):
                runtime.start_approved(
                    request, approved_fingerprint=request.fingerprint(),
                    current_task_sha256=request.task_sha256)
            start.assert_called_once()
            self.assertEqual(manager.list_sessions()["count"], 1)

    def test_concurrent_duplicate_only_one_launch(self):
        def attempt(_):
            try:
                self.start()
                return "started"
            except BrokerRequestDenied:
                return "denied"
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(16)))
        self.assertEqual(results.count("started"), 1)
        self.assertEqual(results.count("denied"), 15)
        self.assertEqual(len(self.manager.starts), 1)
    def test_result_read_only_and_request_bound(self):
        self.start()
        result = self.runtime.query_result(self.request.request_id, offset=0)
        self.assertEqual(result["request_id"], self.request.request_id)
        self.assertEqual(result["session_id"], "c"*48)
        self.assertEqual(result["exit_code"], None)
        self.assertEqual(len(self.manager.starts), 1)
        again = self.runtime.query_result(self.request.request_id, offset=0)
        self.assertEqual(result, again)
        self.assertEqual(len(self.manager.starts), 1)

    def test_result_lookup_denied_on_owner_lock_or_invalid_offset(self):
        self.start()
        for offset in (-1, True, 65537, "0"):
            with self.subTest(offset=offset):
                with self.assertRaises(ValueError):
                    self.runtime.query_result(self.request.request_id, offset=offset)
        self.guard.allowed = False
        with self.assertRaises(PermissionError):
            self.runtime.query_result(self.request.request_id)

    def test_unknown_and_mismatched_results_are_not_success(self):
        self.assertEqual(
            self.runtime.query_result("d"*32)["status"], "UNKNOWN")
        self.start()
        original_read = self.manager.read
        self.manager.read = lambda session, offset=0: {
            "request_id": "e"*32, "session_id": session,
            "status": "COMPLETED", "exit_code": 0}
        self.assertEqual(
            self.runtime.query_result(self.request.request_id)["status"],
            "OUTCOME_UNKNOWN")
        self.manager.read = lambda session, offset=0: (_ for _ in ()).throw(KeyError())
        self.assertEqual(
            self.runtime.query_result(self.request.request_id)["status"],
            "OUTCOME_UNKNOWN")
        self.manager.read = original_read
        self.assertEqual(len(self.manager.starts), 1)

    def test_local_dispatcher_routes_known_unknown_and_cancel(self):
        self.start()
        dispatcher = self.runtime.make_local_dispatcher()

        def send(op, rid):
            return json.loads(dispatcher.handle(json.dumps(
                {"version": 1, "op": op, "request_id": rid}).encode("ascii")))

        self.assertEqual(send("query", self.request.request_id)["status"], "OK")
        self.assertEqual(send("query", "d"*32)["status"], "UNKNOWN")
        self.assertEqual(send("cancel", self.request.request_id)["status"], "OK")
        self.assertEqual(send("cancel", "d"*32)["status"], "UNKNOWN")
        self.assertEqual(len(self.manager.starts), 1)

    def test_local_dispatcher_owner_lock_blocks_existing_instance(self):
        self.start()
        dispatcher = self.runtime.make_local_dispatcher()
        self.guard.allowed = False
        raw = json.dumps({"version": 1, "op": "query",
                          "request_id": self.request.request_id}).encode()
        self.assertEqual(json.loads(dispatcher.handle(raw))["status"], "DENIED")
        self.assertEqual(len(self.manager.starts), 1)

    def test_local_dispatcher_status_survives_log_read_failure(self):
        self.start()
        self.manager.read = lambda session, offset=0: (_ for _ in ()).throw(KeyError())
        dispatcher = self.runtime.make_local_dispatcher()
        raw = json.dumps({"version": 1, "op": "query",
                          "request_id": self.request.request_id}).encode()
        # v1 only needs status metadata; a failed log read does not erase
        # an independently confirmed RUNNING state.
        self.assertEqual(json.loads(dispatcher.handle(raw))["status"], "OK")
        self.assertEqual(len(self.manager.starts), 1)

    def test_real_job_manager_broker_result_readonly_temp(self):
        """Local benign process; no IPC, archive, protected storage or service."""
        with tempfile.TemporaryDirectory(prefix="next-broker-local-") as tmp:
            from sora_commander_next.jobs import JobManager
            manager = JobManager(max_concurrent=1, capacity=2)
            request = BoundRequest(
                "f"*32, sys.executable,
                ("-c", "import sys;print('broker-local-ok');sys.stderr.write('err-ok\\n')"),
                tmp, "b"*64, "owner", 1000.0, 8.0, 4096)
            runtime = BrokerRuntime(
                expected_executable=sys.executable, expected_cwd=tmp,
                owner_binding="owner", owner_guard=self.guard,
                clock=lambda: 100.0, manager=manager)
            try:
                started = runtime.start_approved(
                    request, approved_fingerprint=request.fingerprint(),
                    current_task_sha256=request.task_sha256)
                self.assertEqual(started["status"], "RUNNING")
                sid = runtime.sessions[request.request_id]
                finished = manager.wait_for(sid, 6)
                self.assertEqual(finished["status"], "COMPLETED")
                self.assertEqual(finished["exit_code"], 0)
                result = runtime.query_result(request.request_id)
                self.assertEqual(result["exit_code"], 0)
                self.assertIn("broker-local-ok", result["stdout"])
                self.assertIn("err-ok", result["stderr"])
                self.assertEqual(runtime.query_status(request.request_id)["status"],
                                 "COMPLETED")
                dispatcher = runtime.make_local_dispatcher()
                wire = json.dumps({"version": 1, "op": "query",
                                   "request_id": request.request_id}).encode()
                self.assertEqual(json.loads(dispatcher.handle(wire))["status"],
                                 "OK")
                self.assertEqual(len(manager.list_sessions()["sessions"]), 1)
                with self.assertRaises(BrokerRequestDenied):
                    runtime.start_approved(
                        request, approved_fingerprint=request.fingerprint(),
                        current_task_sha256=request.task_sha256)
            finally:
                runtime.close()

    def test_real_five_sequential_jobs_with_capacity_one_and_no_replay(self):
        """A broker must not become permanently BUSY after its first job."""
        from dataclasses import replace
        from sora_commander_next.jobs import JobManager
        with tempfile.TemporaryDirectory(prefix="next-broker-many-") as tmp:
            manager = JobManager(max_concurrent=1, capacity=8, retention=.05)
            fixed = BoundRequest(
                "0"*32, sys.executable,
                ("-I", "-S", "-c", "print('NEXT-JOB-OK')"), tmp,
                "b"*64, "owner", 1000.0, 4.0, 4096)
            runtime = BrokerRuntime(
                expected_executable=sys.executable, expected_cwd=tmp,
                owner_binding="owner", owner_guard=self.guard, clock=lambda: 100.0,
                capacity=1, manager=manager)
            try:
                for i in range(1, 7):
                    req = replace(fixed, request_id=f"{i:032x}")
                    response = runtime.start_approved(
                        req, approved_fingerprint=req.fingerprint(),
                        current_task_sha256=req.task_sha256)
                    self.assertEqual(response["status"], "RUNNING")
                    sid = runtime.sessions[req.request_id]
                    complete = manager.wait(sid)
                    self.assertEqual(complete["status"], "COMPLETED")
                    self.assertEqual(complete["exit_code"], 0)
                    self.assertTrue(manager.confirm_terminal_readonly(req.request_id, sid))
                    if i == 1:
                        spent = req
                        # Test reclamation even after first result is evicted
                        # by the legitimate supervisor retention/prune path.
                        import time
                        time.sleep(.09)
                        self.assertEqual(manager.list_sessions()["count"], 0)
                        self.assertTrue(manager.confirm_terminal_readonly(req.request_id, sid))
                        self.assertFalse(manager.confirm_terminal_readonly(
                            req.request_id, "f"*48))
                        self.assertFalse(manager.confirm_terminal_readonly(
                            "f"*32, sid))
                self.assertEqual(len(runtime.admission._accepted), 6)
                self.assertEqual(len(runtime.admission.active_ids_local()), 1)
                with self.assertRaisesRegex(BrokerRequestDenied, "request already consumed"):
                    runtime.start_approved(
                        spent, approved_fingerprint=spent.fingerprint(),
                        current_task_sha256=spent.task_sha256)
                self.assertEqual(len(runtime.admission._accepted), 6)
            finally:
                runtime.close()

    def test_ambiguous_announcement_does_not_free_slot(self):
        from dataclasses import replace
        manager = Manager()
        def uncertain_start(command):
            manager.starts.append(command)
            return {"status": "UNKNOWN", "request_id": command.request_id}
        manager.start = uncertain_start
        runtime = BrokerRuntime(
            expected_executable=self.request.executable,
            expected_cwd=self.request.cwd,
            owner_binding="owner", owner_guard=self.guard,
            clock=lambda: 100.0, capacity=1, manager=manager)
        start = runtime.start_approved(
            self.request, approved_fingerprint=self.request.fingerprint(),
            current_task_sha256=self.request.task_sha256)
        self.assertEqual(start["status"], "UNKNOWN")
        self.assertEqual(runtime.sessions, {})
        next_request = replace(self.request, request_id="e"*32)
        with self.assertRaisesRegex(BrokerRequestDenied, "broker capacity reached"):
            runtime.start_approved(
                next_request, approved_fingerprint=next_request.fingerprint(),
                current_task_sha256=next_request.task_sha256)
        with self.assertRaisesRegex(BrokerRequestDenied, "request already consumed"):
            runtime.start_approved(
                self.request, approved_fingerprint=self.request.fingerprint(),
                current_task_sha256=self.request.task_sha256)
        self.assertEqual(len(manager.starts), 1)

    def test_close(self):
        self.runtime.close()
        self.assertTrue(self.manager.closed)


if __name__ == "__main__":
    unittest.main()
