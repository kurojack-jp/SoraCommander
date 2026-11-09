import base64
from dataclasses import replace
import unittest

from test_protected_ledger_client import command
from test_service_spent_ledger_candidate import FakeConnection
from test_sol_broker_vertical import Manager, Owner
from sora_commander_next.broker_host import BrokerHost, BrokerUnavailable, encode_frame
from sora_commander_next.broker_runtime import BrokerRuntime
from sora_commander_next.broker_recovery_candidate import RetainedBrokerCandidate, MockRecoveryConnection
from sora_commander_next.jobs import JobManager
from sora_commander_next.offline_b01_full_binding import make_wire_request
from sora_commander_next.product_composition import build_product_host
from sora_commander_next.service_spent_ledger_candidate import ServiceSpentLedgerCandidate, GenerationFenceCandidate


class RecoveryManager(Manager):
    def request_status_readonly(self, rid):
        return dict(request_id=rid, session_id="c"*48, status=self.state,
            exit_code=self.code, captured_bytes=len(self.output), output_incomplete=False)
    def start(self, command):
        result = super().start(command)
        return {**result, "session_id": "c"*48}


class BrokerRecoveryCandidateTests(unittest.TestCase):
    def setUp(self):
        self.owner = Owner()
        self.command = command()
        # Match the existing Owner fixture's local binding; no OS observation.
        from sora_commander_next.offline_b01_full_binding import _decode, MAX_SNAPSHOT
        data = _decode(self.command.canonical_bytes, MAX_SNAPSHOT)
        self.owner.identity = (data["owner_sid"], tuple(data["logon_luid"]), data["wts_session"])
        self.manager = RecoveryManager([])
        self.runtime = BrokerRuntime(expected_executable=data["executable"], expected_cwd=data["cwd"],
            owner_binding=data["owner_sid"], owner_guard=self.owner, clock=lambda: 0.1,
            manager=self.manager)
        self.broker = RetainedBrokerCandidate._for_mock(self.runtime, broker_instance_id="3"*32)
        self.db = FakeConnection()
        self.ledger = ServiceSpentLedgerCandidate(GenerationFenceCandidate("1"*32, "2"*32, 1),
            _connection=self.db, _clock_ms=lambda: 100)
        self.raw = make_wire_request(self.command, operation_id="8"*32)
        self.receipt = self.ledger.observe_data_only(self.raw)
        self.host = BrokerHost(MockRecoveryConnection(self.broker))

    def start(self, **kw):
        return self.broker._submit_mock_once(kw.get("command", self.command),
            kw.get("receipt", self.receipt), operation_id=kw.get("operation_id", "8"*32))

    def test_default_native_startup_disarmed(self):
        with self.assertRaises(BrokerUnavailable):
            RetainedBrokerCandidate()

    def test_real_job_manager_even_uninitialized_cannot_be_injected(self):
        self.runtime.manager = object.__new__(JobManager)
        with self.assertRaises(TypeError):
            RetainedBrokerCandidate._for_mock(self.runtime, broker_instance_id="3"*32)
        self.assertEqual(self.manager.started, [])

    def test_missing_explicit_simulation_marker_denied(self):
        self.manager.DEV_SIMULATION_ONLY = False
        with self.assertRaises(TypeError):
            RetainedBrokerCandidate._for_mock(self.runtime, broker_instance_id="3"*32)

    def test_mock_commit_binding_reaches_existing_runtime_once(self):
        self.assertEqual(self.start()["status"], "RUNNING")
        self.assertEqual(len(self.manager.started), 1)
        self.assertEqual(self.db.events[-1][0], "COMMIT")
        self.assertEqual(self.runtime.sessions, {"4"*32: "c"*48})
        self.assertEqual(self.broker._records["4"*32].request_bytes, self.raw)

    def test_host_restart_retains_manager_session_binding_logs_and_exit(self):
        self.start()
        first = self.host.request_status("4"*32)
        self.assertEqual(first["session_id"], "c"*48)
        self.assertEqual(first["ledger_binding"], self.receipt.request_sha256)
        self.host.close()
        self.assertEqual(self.manager.closed, 0)
        next_host = BrokerHost(MockRecoveryConnection(self.broker))
        self.manager.output, self.manager.state, self.manager.code = b"finished\n", "COMPLETED", 0
        second = next_host.request_status("4"*32)
        page = next_host.read_process_output("c"*48)
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(second["ledger_binding"], first["ledger_binding"])
        self.assertEqual(page["exit_code"], 0)
        self.assertEqual(base64.b64decode(page["stdout_base64"]), b"finished\n")
        self.assertIs(self.runtime.manager, self.manager)
        self.assertEqual(len(self.manager.started), 1)
        self.assertEqual(sum(sql == "COMMIT" for sql, _ in self.db.events), 1)

    def test_result_paging_bounded_at_4096_and_retains_offsets(self):
        self.start()
        self.manager.output = b"x"*8192
        first = self.host.read_process_output("c"*48)
        second = self.host.read_process_output("c"*48, offset=4096)
        self.assertEqual((first["next_offset"], second["next_offset"]), (4096, 8192))
        self.assertFalse(first["end_of_capture"])
        self.assertTrue(second["end_of_capture"])

    def test_duplicate_does_not_consume_or_start_again(self):
        self.start()
        count = len(self.db.events)
        self.assertEqual(self.start()["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.manager.started), 1)
        self.assertEqual(len(self.db.events), count)

    def test_broker_death_overrides_even_cached_completed_state(self):
        self.start()
        self.manager.state, self.manager.code = "COMPLETED", 0
        self.broker.note_mock_broker_death()
        self.assertEqual(self.host.request_status("4"*32)["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.host.read_process_output("c"*48)["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.start()["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.manager.started), 1)

    def test_new_broker_cannot_recover_session_from_saved_receipt(self):
        self.start()
        self.broker.note_mock_broker_death()
        new_runtime = BrokerRuntime(expected_executable=self.runtime.admission.expected_executable,
            expected_cwd=self.runtime.admission.expected_cwd,
            owner_binding=self.runtime.admission.owner_binding, owner_guard=self.owner,
            clock=lambda: 0.1, manager=RecoveryManager([]))
        new_broker = RetainedBrokerCandidate._for_mock(new_runtime, broker_instance_id="e"*32)
        new_host = BrokerHost(MockRecoveryConnection(new_broker))
        self.assertEqual(new_host.request_status("4"*32)["status"], "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "another Broker instance"):
            new_broker._submit_mock_once(self.command, self.receipt, operation_id="8"*32)
        self.assertEqual(new_runtime.manager.started, [])

    def test_unknown_and_foreign_session_never_leak_output(self):
        self.start()
        self.assertEqual(self.host.request_status("f"*32)["status"], "UNKNOWN")
        self.assertEqual(self.host.read_process_output("f"*48)["status"], "DENIED")

    def test_untrusted_receipt_dialects_cannot_reach_mock_start(self):
        for status in ("COMMITTED_FRESH", "DATA_UNTRUSTED_FRESH", "DATA_RECORDED_ONCE",
                       "DATA_SPENT", "DATA_ABSENT", "OUTCOME_UNKNOWN"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.start(receipt=replace(self.receipt, status=status))
        self.assertEqual(self.manager.started, [])

    def test_digest_operation_challenge_and_command_mismatch_denied(self):
        for kw in ({"receipt": replace(self.receipt, request_sha256="f"*64)},
                   {"operation_id": "f"*32},
                   {"command": command(approval_nonce="f"*64)},
                   {"command": command(argv=["--other"])}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.start(**kw)
        self.assertEqual(self.manager.started, [])

    def test_manager_replacement_denies_start(self):
        self.runtime.manager = object.__new__(JobManager)
        with self.assertRaises(TypeError):
            self.start()
        self.assertEqual(self.manager.started, [])

    def test_lost_mock_start_ack_terminal_without_resubmit(self):
        original = self.manager.start
        def lost(command):
            original(command)
            raise ConnectionError("mock ACK lost")
        self.manager.start = lost
        self.assertEqual(self.start()["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.start()["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.manager.started), 1)

    def test_owner_lock_cannot_disclose_cached_result(self):
        self.start()
        self.owner.active = False
        self.assertEqual(self.host.request_status("4"*32)["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.host.read_process_output("c"*48)["status"], "OUTCOME_UNKNOWN")

    def test_status_rejects_invented_success_and_missing_exit_evidence(self):
        self.start()
        for state, code in (("SUCCESS", 0), ("COMPLETED", None), ("COMPLETED", False),
                            ("COMPLETED", 1), ("RUNNING", 0), ("FAILED", None)):
            self.manager.state, self.manager.code = state, code
            self.assertEqual(self.host.request_status("4"*32)["status"], "OUTCOME_UNKNOWN")

    def test_status_rejects_missing_and_malformed_metadata(self):
        self.start()
        original = self.manager.request_status_readonly
        for changes in ({"captured_bytes": True}, {"output_incomplete": "no"},
                        {"status": "COMPLETED", "exit_code": 0, "output_incomplete": True}):
            self.manager.request_status_readonly = lambda rid: {**original(rid), **changes}
            self.assertEqual(self.host.request_status("4"*32)["status"], "OUTCOME_UNKNOWN")
        def missing_exit(rid):
            result = original(rid)
            del result["exit_code"]
            return result
        self.manager.request_status_readonly = missing_exit
        self.assertEqual(self.host.request_status("4"*32)["status"], "OUTCOME_UNKNOWN")

    def test_forged_page_binding_cannot_disclose_output(self):
        self.start()
        original = self.manager.read_live_page
        self.manager.read_live_page = lambda sid, off: {**original(sid, off), "request_id": "f"*32}
        result = self.host.read_process_output("c"*48)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertNotIn("stdout_base64", result)

    def test_host_execution_action_has_no_submission_or_local_fallback(self):
        result = self.host.run_command(executable="anything", request_id="4"*32)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.manager.started, [])

    def test_remote_approval_and_extra_recovery_fields_rejected(self):
        raw = encode_frame(dict(v=1, action="request_status", call_id="a"*32,
            arguments={"request_id": "4"*32, "trusted": True}))
        from sora_commander_next.broker_host import decode_frame
        self.assertEqual(decode_frame(self.broker.handle(raw))["result"]["status"], "UNAVAILABLE")
        self.assertEqual(self.manager.started, [])

    def test_product_root_remains_unavailable(self):
        self.assertEqual(build_product_host().request_status("4"*32)["status"], "UNAVAILABLE")
        self.assertEqual(build_product_host().run_command(request_id="4"*32)["status"], "UNAVAILABLE")

    def test_recovery_connection_close_does_not_close_runtime(self):
        connection = MockRecoveryConnection(self.broker)
        connection.close()
        with self.assertRaises(ConnectionError):
            connection.request(b"{}")
        self.assertEqual(self.manager.closed, 0)
