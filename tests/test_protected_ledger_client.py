"""Wire/client integration with a fully injected framed native-shaped transport."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from sora_commander_next.broker_frames import pack_frame
from sora_commander_next.broker_host import BrokerHost, encode_frame
from sora_commander_next.offline_b01_full_binding import (
    freeze_command, make_wire_request, make_wire_reply, decode_wire_request, _canonical,
    ContractError,
)
from sora_commander_next.protected_ledger_client import (
    ProtectedLedgerClient, LedgerCallBinding, _unpack,
)
from sora_commander_next.product_composition import build_product_host


def command(**updates):
    data = dict(format_version=1, operation="COMMAND", installation_id="1" * 32,
        generation="2" * 32, broker_instance_id="3" * 32, request_id="4" * 32,
        owner_sid="S-1-5-21-1-2-3-1001", logon_luid=[5, 6], wts_session=1,
        policy_generation=1, release_sequence=1, approval_nonce="5" * 64,
        expires_at_tick_ms=45000, executable=r"C:\Protected\python.exe", argv=["--version"],
        cwd=r"D:\Approved", task_sha256="6" * 64, execution_profile_sha256="7" * 64,
        timeout_ms=1000, output_limit=4096)
    return freeze_command({**data, **updates})


class LedgerClientTests(unittest.TestCase):
    def setUp(self):
        for target in ("ctypes.WinDLL", "subprocess.Popen", "sqlite3.connect"):
            guard = patch(target, side_effect=AssertionError("native or DB forbidden"), create=True)
            guard.start()
            self.addCleanup(guard.stop)
        self.command = command()
        self.now = 100
        self.binding = LedgerCallBinding("S-1-5-21-1-2-3-1001", (5, 6), 1,
                                        "2" * 32, "3" * 32, "7" * 64, 1)
        self.status = "COMMITTED_FRESH"
        self.change = None
        self.transport = Mock(exchange=self.exchange)
        self.sent = []
        self.client = ProtectedLedgerClient(_transport=self.transport, _clock_ms=lambda: self.now)

    def exchange(self, frame):
        req = decode_wire_request(_unpack(frame))
        self.sent.append(req)
        if self.change:
            return self.change(req)
        return pack_frame(make_wire_reply(req, self.status))

    def consume(self, **kw):
        args = dict(operation_id="8" * 32, binding=self.binding, current_binding=lambda: self.binding)
        args.update(kw)
        return self.client.consume_once(self.command, **args)

    def test_framed_consume_correlates_full_snapshot_and_has_no_authority(self):
        result = self.consume()
        self.assertEqual(result.observation, "DATA_UNTRUSTED_FRESH")
        self.assertEqual(result.authority, "NONE_DATA_ONLY")
        self.assertEqual(result.admission, "UNAVAILABLE")
        req = self.sent[0]
        self.assertEqual(req["snapshot_sha256"], self.command.sha256)
        self.assertEqual(req["logon_luid"], [5, 6])
        self.assertEqual(req["challenge"], "5" * 64)
        self.assertNotIn("executable", req)
        self.assertNotIn("execution_profile_sha256", req)  # Bound by domain digest.

    def test_default_transport_is_unavailable_and_attempt_cannot_rearm(self):
        self.client = ProtectedLedgerClient(_clock_ms=lambda: self.now)
        self.assertEqual(self.consume().observation, "UNAVAILABLE")
        self.assertEqual(self.consume().observation, "DATA_IGNORED")

    def test_duplicate_operation_change_cannot_send_again(self):
        self.consume()
        self.assertEqual(self.consume(operation_id="9" * 32).observation, "DATA_IGNORED")
        self.assertEqual(len(self.sent), 1)

    def test_changed_approval_and_challenge_do_not_change_spent_key(self):
        self.consume()
        self.command = command(approval_nonce="a" * 64, argv=["changed"])
        self.assertEqual(self.consume(operation_id="9" * 32).observation, "DATA_IGNORED")
        self.assertEqual(len(self.sent), 1)

    def test_operation_or_challenge_reuse_across_request_ids_refused(self):
        self.consume()
        self.command = command(request_id="a" * 32)
        self.assertEqual(self.consume(operation_id="9" * 32).observation, "DATA_DENIED")
        self.command = command(request_id="b" * 32, approval_nonce="b" * 64)
        self.assertEqual(self.consume().observation, "DATA_DENIED")
        self.assertEqual(len(self.sent), 1)

    def test_lost_reply_terminal_even_after_query_absence(self):
        self.change = Mock(side_effect=ConnectionError("lost ACK"))
        self.assertEqual(self.consume().observation, "OUTCOME_UNKNOWN")
        query = make_wire_request(self.command, operation_id="9" * 32, action="QUERY")
        result = self.client.observe_query_reply(pack_frame(make_wire_reply(
            decode_wire_request(query), "ABSENT")), expected_request=query)
        self.assertEqual(result.observation, "DATA_QUERY_ONLY")
        self.change = None
        self.assertEqual(self.consume().observation, "DATA_IGNORED")
        self.assertEqual(len(self.sent), 1)

    def test_query_can_never_return_fresh_or_consume_echo(self):
        query = make_wire_request(self.command, operation_id="9" * 32, action="QUERY")
        for fields in ({**decode_wire_request(query), "status": "COMMITTED_FRESH"},
                       {**decode_wire_request(query), "status": "SPENT", "action": "CONSUME"}):
            with self.assertRaises(ContractError):
                self.client.observe_query_reply(pack_frame(_canonical(fields, 8192)), expected_request=query)

    def test_five_second_call_deadline_is_stricter_than_approval_expiry(self):
        def delayed(req):
            self.now = 5100
            return pack_frame(make_wire_reply(req, "COMMITTED_FRESH"))
        self.change = delayed
        self.assertEqual(self.consume().observation, "OUTCOME_UNKNOWN")
        self.now = 100
        self.assertEqual(self.consume().observation, "DATA_IGNORED")
        self.assertEqual(len(self.sent), 1)

    def test_cancelled_context_before_send_is_terminal_no_send(self):
        callback = Mock(side_effect=PermissionError("cancelled"))
        self.assertEqual(self.consume(current_binding=callback).observation, "OUTCOME_UNKNOWN")
        self.assertEqual(self.consume().observation, "DATA_IGNORED")
        self.assertEqual(self.sent, [])

    def test_context_observation_delay_cannot_extend_deadline_or_send(self):
        def delayed():
            self.now = 5100
            return self.binding
        self.assertEqual(self.consume(current_binding=delayed).observation, "OUTCOME_UNKNOWN")
        self.assertEqual(self.sent, [])

    def test_context_observation_delay_after_reply_cannot_accept_fresh(self):
        def observed():
            if self.sent:
                self.now = 5100
            return self.binding
        self.assertEqual(self.consume(current_binding=observed).observation, "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.sent), 1)

    def test_cancelled_context_after_reply_cannot_accept_fresh(self):
        def cancelled():
            if self.sent:
                raise PermissionError("cancelled while awaiting COMMIT")
            return self.binding
        self.assertEqual(self.consume(current_binding=cancelled).observation, "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.sent), 1)

    def test_reentrant_consume_cannot_send_twice(self):
        def reentrant(req):
            self.assertEqual(self.consume().observation, "UNAVAILABLE")
            return pack_frame(make_wire_reply(req, "COMMITTED_FRESH"))
        self.change = reentrant
        self.consume()
        self.assertEqual(len(self.sent), 1)

    def test_monotonic_rollback_halts_new_admission(self):
        def rollback(req):
            self.now = 99
            return pack_frame(make_wire_reply(req, "COMMITTED_FRESH"))
        self.change = rollback
        self.assertEqual(self.consume().observation, "OUTCOME_UNKNOWN")
        self.command = command(request_id="a" * 32, approval_nonce="a" * 64)
        self.now = 200
        self.assertEqual(self.consume(operation_id="a" * 32).observation, "UNAVAILABLE")
        self.assertEqual(len(self.sent), 1)

    def test_interrupt_retains_unknown_and_does_not_resend(self):
        self.change = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.consume()
        self.assertEqual(self.consume().observation, "DATA_IGNORED")
        self.assertEqual(len(self.sent), 1)

    def test_history_full_does_not_evict_prior_attempt(self):
        for index in range(64):
            self.command = command(request_id=f"{index:032x}", approval_nonce=f"{index:064x}")
            self.consume(operation_id=f"{index:032x}")
        self.command = command(request_id="f" * 32, approval_nonce="f" * 64)
        self.assertEqual(self.consume(operation_id="f" * 32).observation, "UNAVAILABLE")
        self.assertEqual(len(self.sent), 64)

    def test_no_injected_trusted_boolean_or_spawner_parameter(self):
        for name in ("trusted", "approved", "spawn", "service_verified"):
            with self.assertRaises(TypeError):
                ProtectedLedgerClient(**{name: True})

    def test_forged_service_status_cannot_pass_host_or_product_admission(self):
        def reply(raw):
            from sora_commander_next.broker_host import decode_frame
            req = decode_frame(raw)
            return encode_frame({"v": 1, "action": req["action"], "call_id": req["call_id"],
                                 "result": {"status": "COMMITTED_FRESH"}})
        self.assertEqual(BrokerHost(Mock(request=reply)).request_status("4" * 32)["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(build_product_host().request_status("4" * 32)["status"], "UNAVAILABLE")


def rebound_case(name, value):
    def run(self):
        def changed(req):
            self.binding = replace(self.binding, **{name: value})
            return pack_frame(make_wire_reply(req, "COMMITTED_FRESH"))
        self.change = changed
        self.assertEqual(self.consume().observation, "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.sent), 1)
    return run


for name, value in (("owner_sid", "S-1-5-21-9-2-3-1001"), ("logon_luid", (9, 6)),
                    ("wts_session", 2), ("wts_session", True), ("generation", "a" * 32),
                    ("broker_instance_id", "a" * 32), ("execution_profile_sha256", "a" * 64),
                    ("connection_epoch", 2)):
    setattr(LedgerClientTests, f"test_rebound_{name}_{str(value)[:8]}", rebound_case(name, value))


def corrupt_reply(name, value):
    def run(self):
        self.change = lambda req: pack_frame(_canonical({**req, name: value, "status": "COMMITTED_FRESH"}, 8192))
        self.assertEqual(self.consume().observation, "DATA_UNKNOWN")
        self.assertEqual(self.consume().observation, "DATA_IGNORED")
        self.assertEqual(len(self.sent), 1)
    return run


for name, value in (("v", True), ("installation_id", "a" * 32), ("generation", "a" * 32),
                    ("broker_instance_id", "a" * 32), ("request_id", "a" * 32),
                    ("operation_id", "a" * 32), ("owner_sid", "S-1-5-19"),
                    ("logon_luid", [9, 6]), ("wts_session", 2), ("challenge", "a" * 64),
                    ("snapshot_sha256", "a" * 64)):
    setattr(LedgerClientTests, "test_corrupt_echo_" + name, corrupt_reply(name, value))


def malformed_case(label, raw):
    def run(self):
        self.change = lambda req: raw
        self.assertEqual(self.consume().observation, "OUTCOME_UNKNOWN")
        self.assertEqual(self.consume().observation, "DATA_IGNORED")
    return run


for label, raw in (("partial_header", b"\1"), ("partial_body", b"\x10\0\0\0x"),
                   ("too_large", b"x" * 8197), ("trailing", pack_frame(b"{}") + b"x"),
                   ("nonbytes", bytearray(b"abc"))):
    setattr(LedgerClientTests, "test_frame_" + label, malformed_case(label, raw))


def dialect_case(status):
    def run(self):
        self.change = lambda req: pack_frame(_canonical({**req, "status": status}, 8192))
        result = self.consume()
        self.assertEqual(result.observation, "DATA_UNKNOWN")
        self.assertEqual(result.admission, "UNAVAILABLE")
    return run


for status in ("DATA_RECORDED_ONCE", "DEV_RECORDED_ONCE", "DATA_UNTRUSTED_FRESH", "AUTHENTICATED"):
    setattr(LedgerClientTests, "test_untrusted_dialect_" + status, dialect_case(status))


if __name__ == "__main__":
    unittest.main()
