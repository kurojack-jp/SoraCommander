"""Synthetic C6 mutation matrix: no pipe, files, windows or child processes."""
import unittest
from types import SimpleNamespace
from pathlib import Path

from sora_commander_next.broker_host import (
    BrokerHost, MAX_REPLY_BYTES, decode_frame, encode_frame,
)
from sora_commander_next.no_answer import valid_no_answer
from sora_commander_next.owner_broker_service import OwnerBrokerService, _SCHEMAS
from sora_commander_next.owner_broker_dev import DevBrokerHost
from tests.test_owner_broker_service import Backend, Transport, CWD, EXE, RID

PID = "2" * 32


def tuples():
    for stage in ("FIRST_GUI", "SECOND_GUI", "FILE_GUI", "INPUT_GUI"):
        for reason in ("EXPIRED", "CLOSED"):
            key = "launch_outcome" if stage in ("FIRST_GUI", "SECOND_GUI") else "write_outcome"
            yield dict(status="TIMED_OUT", stage=stage, reason=reason,
                       approval_state="EXPIRED", auto_retry=False,
                       required_action="REPORT_AND_WAIT", **{key: "NOT_STARTED"})


def corruptions(value):
    key = "launch_outcome" if "launch_outcome" in value else "write_outcome"
    other = "write_outcome" if key == "launch_outcome" else "launch_outcome"
    changes = [("stage", "FILE_GUI" if key == "launch_outcome" else "FIRST_GUI"),
               ("stage", "FORGED"), (other, "NOT_STARTED"),
               ("session_id", "a" * 48), ("session_id", ""),
               ("auto_retry", True), ("auto_retry", 0), ("auto_retry", None),
               ("required_action", "OBSERVE_EXISTING"), ("reason", "DENIED"),
               ("approval_state", "REJECTED"), (key, "STARTED"),
               ("extra", "forged"), ("pending_id", 1), ("request_id", None)]
    for field, bad in changes:
        yield field, dict(value, **{field: bad})
    for field in value:
        if field != "status":
            yield "missing_" + field, {k: v for k, v in value.items() if k != field}


class NoAnswerBrokerTests(unittest.TestCase):
    def arguments(self, action):
        values = dict(executable=EXE, argv=[], cwd=CWD, request_id=RID,
                      pending_id=PID, session_id="a" * 48, background=False,
                      path=CWD, source=CWD, destination=EXE, content="c",
                      old_text="a", new_text="b", expected_sha256="a" * 64)
        return {key: values[key] for key in _SCHEMAS[action][0]}

    def ends(self, action, value, arguments):
        backend = Backend()
        backend.result = value
        service = OwnerBrokerService(backend)
        raw = encode_frame(dict(v=1, action=action, call_id="3" * 32, arguments=arguments))
        broker = decode_frame(service.handle(raw), max_bytes=MAX_REPLY_BYTES)["result"]
        # Bypass the service, so the client must independently reject forged tuples.
        def reply(raw):
            envelope = decode_frame(raw)
            return encode_frame({k: envelope[k] for k in ("v", "action", "call_id")} |
                                {"result": value}, max_bytes=MAX_REPLY_BYTES)
        client = BrokerHost(SimpleNamespace(request=reply))._call(action, **arguments)
        return broker, client

    def test_all_four_shapes_and_single_field_mutations_at_each_end_all_paths(self):
        for action in _SCHEMAS:
            if action == "activity":  # B's separate metadata envelope, not a result tuple.
                continue
            args = self.arguments(action)
            for value in tuples():
                for label, candidate in [("valid", value), *corruptions(value)]:
                    with self.subTest(action=action, stage=value["stage"], reason=value["reason"], mutation=label):
                        for result in self.ends(action, candidate, args):
                            if label == "valid":
                                self.assertEqual(encode_frame(result), encode_frame(value))
                            else:
                                self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
                                self.assertIs(result["auto_retry"], False)
                                self.assertNotIn("launch_outcome", result)
                                self.assertNotIn("write_outcome", result)

    def test_observation_ids_and_null_session_survive_but_mismatches_do_not(self):
        for value in tuples():
            candidate = dict(value, request_id=RID, pending_id=PID, session_id=None)
            self.assertTrue(valid_no_answer(candidate))
            for result in self.ends("request_status", candidate, dict(request_id=RID)):
                self.assertEqual(result, candidate)
            for action, key in (("request_status", "request_id"), ("review_status", "pending_id")):
                for result in self.ends(action, dict(candidate, **{key: "f" * 32}), {key: candidate[key]}):
                    self.assertEqual(result["status"], "OUTCOME_UNKNOWN")

    def test_other_statuses_are_byte_identical_even_with_mixed_diagnostics(self):
        for action in _SCHEMAS:
            if action == "activity":
                continue
            for status in ("OK", "PENDING", "DENIED", "ERROR", "OUTCOME_UNKNOWN", "UNKNOWN", "RUNNING"):
                value = dict(next(tuples()), status=status, write_outcome="STARTED", extra=[1, None])
                for result in self.ends(action, value, self.arguments(action)):
                    self.assertEqual(encode_frame(result), encode_frame(value))

    def test_lost_oversize_wrong_envelope_id_keep_existing_uncertainty(self):
        for mode in ("lost", "oversize", "wrong_id"):
            calls = []
            def reply(raw):
                calls.append(raw)
                if mode == "lost":
                    raise ConnectionError("lost")
                if mode == "oversize":
                    return b"x" * (MAX_REPLY_BYTES + 1)
                envelope = decode_frame(raw)
                return encode_frame(dict(v=1, action=envelope["action"], call_id="0" * 32,
                                         result=next(tuples())))
            result = BrokerHost(SimpleNamespace(request=reply)).review_status(PID)
            self.assertEqual(result, dict(status="OUTCOME_UNKNOWN", error="BrokerReplyUnconfirmed", auto_retry=False))
            self.assertEqual(len(calls), 1)

    def test_remote_pilot_and_four_dev_tools_use_common_client_check(self):
        for value in (dict(next(tuples()), stage="FILE_GUI", write_outcome="NOT_STARTED"),
                      next(v for v in tuples() if v["stage"] == "FILE_GUI")):
            backend = Backend()
            backend.result = value
            root = Path(__file__).resolve().parents[1]
            path = str(root / "tests" / "c6-synthetic.txt")
            client = DevBrokerHost(Transport(OwnerBrokerService(backend)), read_roots=(str(root),),
                                   edit_pilot=True, create_pilot=True)
            pilot = client._edit_review_pilot
            calls = [lambda: pilot.request(path, "c", "a" * 64),
                     lambda: pilot.request_create(path, "c"), lambda: pilot.status(PID)]
            for action in ("create_directory", "create_file", "move_file", "request_source_patch_dev"):
                args = self.arguments(action)
                args.update({key: path for key in ("path", "source", "destination") if key in args})
                calls.append(lambda action=action, args=args: client.dev_mutate(action, **args))
            for call in calls:
                result = call()
                self.assertEqual(result["status"], "TIMED_OUT" if valid_no_answer(value) else "OUTCOME_UNKNOWN")
            self.assertEqual(len(backend.calls), 7)
