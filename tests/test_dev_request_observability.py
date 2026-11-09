"""Deterministic isolated DEV tests; no adapter/Host/Broker/native calls."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields
import json
from pathlib import Path
import sys
import traceback
import unittest
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next import dev_request_observability as obs


class Malicious:
    def __str__(self):
        raise AssertionError("must not stringify")

    __repr__ = __str__

    def __eq__(self, other):
        raise AssertionError("must not compare")

    def __hash__(self):
        raise AssertionError("must not hash")


class DevRequestObservabilityTests(unittest.TestCase):
    def setUp(self):
        # Fixed unique entropy fixture; production uses uuid4 without injection.
        self.ids = patch.object(obs, "uuid4", side_effect=(
            UUID(int=index) for index in range(1, 2049)))
        self.ids.start()
        self.addCleanup(self.ids.stop)
        self.recorder = obs.DevRequestRecorder()
        self.handle = self.recorder.begin_request()

    def record(self, **overrides):
        values = dict(request=self.handle, kind=obs.EventKind.ADAPTER_ENTER,
                      outcome=obs.Outcome.UNKNOWN, reason=obs.ReasonCode.NONE)
        values.update(overrides)
        return self.recorder.record(**values)

    def test_fixed_vocabulary_and_schema(self):
        self.assertEqual({item.value for item in obs.EventKind}, {
            "ADAPTER_ENTER", "VALIDATION_REJECT", "HANDLER_ENTER",
            "HANDLER_EXIT", "ADAPTER_EXIT"})
        self.assertEqual({item.value for item in obs.Outcome}, {
            "OK", "DENIED", "ERROR", "CANCELLED", "UNKNOWN"})
        self.assertEqual({item.value for item in obs.ReasonCode}, {
            "NONE", "INVALID_REQUEST", "ACCESS_DENIED", "HANDLER_FAILURE",
            "REQUEST_CANCELLED", "UNDETERMINED"})
        self.assertEqual([item.name for item in fields(obs.Event)], [
            "epoch", "sequence", "correlation_id", "kind", "outcome", "reason"])
        for kind in obs.EventKind:
            for outcome in obs.Outcome:
                for reason in obs.ReasonCode:
                    self.assertTrue(self.record(kind=kind, outcome=outcome, reason=reason))
        snapshot = self.recorder.snapshot()
        self.assertEqual(len(snapshot.events), 150)
        self.assertEqual([item.sequence for item in snapshot.events], list(range(1, 151)))
        self.assertTrue(all(item.correlation_id == self.handle.correlation_id
                            and item.epoch == snapshot.epoch for item in snapshot.events))

    def test_default_unknown_and_immutable_snapshots(self):
        before = self.recorder.snapshot()
        self.assertTrue(self.recorder.record(self.handle, obs.EventKind.ADAPTER_ENTER))
        after = self.recorder.snapshot()
        self.assertEqual(before.events, ())
        self.assertIs(after.events[0].outcome, obs.Outcome.UNKNOWN)
        for target, name, value in [(after, "epoch", "evil"),
                                    (after.events[0], "sequence", 0),
                                    (self.handle, "correlation_id", "evil")]:
            with self.assertRaises(FrozenInstanceError):
                setattr(target, name, value)

    def test_malicious_values_are_rejected_without_hooks_or_echo(self):
        values = [Malicious(), "ADAPTER_ENTER", "OK", "NONE", "secret-token",
                  "f" * 64, "C:\\private\\file", "?q=secret",
                  "OK\nforged", "a" * 100000, None, True, 1, [], {}, b"OK"]
        for field in ("request", "kind", "outcome", "reason"):
            for value in values:
                with self.subTest(field=field, value_type=type(value).__name__):
                    recorder = obs.DevRequestRecorder()
                    handle = recorder.begin_request()
                    args = dict(request=handle, kind=obs.EventKind.ADAPTER_ENTER,
                                outcome=obs.Outcome.OK, reason=obs.ReasonCode.NONE)
                    args[field] = value
                    self.assertFalse(recorder.record(**args))
                    snapshot = recorder.snapshot()
                    self.assertEqual(snapshot.events, ())
                    self.assertIs(snapshot.state, obs.RecorderState.INVALID_INPUT)
                    self.assertIsNone(recorder.begin_request())

    def test_unknown_fields_never_retained(self):
        for name in ("payload", "path", "query", "token", "error_text", "hash",
                     "epoch", "sequence", "correlation_id", "secret-field"):
            for operation in ("record", "begin_request"):
                with self.subTest(name=name, operation=operation):
                    recorder = obs.DevRequestRecorder()
                    handle = recorder.begin_request()
                    if operation == "record":
                        self.assertFalse(recorder.record(
                            handle, obs.EventKind.ADAPTER_ENTER, **{name: Malicious()}))
                    else:
                        self.assertIsNone(recorder.begin_request(**{name: Malicious()}))
                    snapshot = recorder.snapshot()
                    self.assertEqual(snapshot.events, ())
                    self.assertIs(snapshot.state, obs.RecorderState.INVALID_INPUT)

    def test_missing_required_record_arguments_leave_recorder_unchanged(self):
        for latched in (False, True):
            for missing in ("both", "request", "kind", "kind_with_payload"):
                with self.subTest(latched=latched, missing=missing):
                    recorder = obs.DevRequestRecorder()
                    handle = recorder.begin_request()
                    self.assertTrue(recorder.record(handle, obs.EventKind.ADAPTER_ENTER))
                    if latched:
                        self.assertFalse(recorder.record(None, obs.EventKind.ADAPTER_ENTER))
                    before = recorder.snapshot()
                    args = {
                        "both": {},
                        "request": {"kind": obs.EventKind.ADAPTER_ENTER},
                        "kind": {"request": handle},
                        "kind_with_payload": {"request": Malicious(),
                                              "payload": "synthetic-secret"},
                    }[missing]
                    with patch.object(obs, "_encode_event") as encode:
                        with patch.object(obs, "uuid4") as entropy:
                            with self.assertRaises(TypeError) as caught:
                                recorder.record(**args)
                            encode.assert_not_called()
                            entropy.assert_not_called()
                    self.assertNotIn("synthetic-secret", str(caught.exception))
                    self.assertEqual(recorder.snapshot(), before)
                    if not latched:
                        self.assertTrue(recorder.record(handle, obs.EventKind.ADAPTER_EXIT))

    def test_foreign_forged_and_previous_epoch_handles_rejected(self):
        handles = [self.handle, obs.RequestHandle(self.handle.correlation_id),
                   self.handle.correlation_id]
        for handle in handles:
            recorder = obs.DevRequestRecorder()
            self.assertFalse(recorder.record(handle, obs.EventKind.HANDLER_ENTER))
            self.assertEqual(recorder.snapshot().events, ())

    def test_event_capacity_no_eviction_or_resume(self):
        for _ in range(256):
            self.assertTrue(self.record())
        before = self.recorder.snapshot()
        self.assertLessEqual(before.byte_count, 65536)
        self.assertFalse(self.record())
        self.assertIs(self.recorder.snapshot().state, obs.RecorderState.FULL)
        self.assertEqual(self.recorder.snapshot().events, before.events)
        self.assertIsNone(self.recorder.begin_request())
        self.assertFalse(self.record(kind=obs.EventKind.ADAPTER_EXIT))

    def test_handle_capacity_is_also_bounded(self):
        for _ in range(255):
            self.assertIsNotNone(self.recorder.begin_request())
        self.assertIsNone(self.recorder.begin_request())
        self.assertIs(self.recorder.snapshot().state, obs.RecorderState.FULL)
        self.assertFalse(self.record())
        self.assertEqual(self.recorder.snapshot().events, ())

    def test_byte_budget_exact_boundary_and_overflow(self):
        self.assertTrue(self.record())
        before = self.recorder.snapshot()
        event = obs.Event(before.epoch, 2, self.handle.correlation_id,
                          obs.EventKind.ADAPTER_ENTER, obs.Outcome.UNKNOWN, obs.ReasonCode.NONE)
        size = len(obs._encode_event(event))
        with patch.object(obs, "MAX_BYTES", before.byte_count + size):
            self.assertTrue(self.record())
            self.assertFalse(self.record())
        after = self.recorder.snapshot()
        self.assertEqual(len(after.events), 2)
        self.assertEqual(after.byte_count, before.byte_count + size)
        self.assertIs(after.state, obs.RecorderState.FULL)
        self.assertEqual(after.byte_count, sum(len(obs._encode_event(e)) for e in after.events))

    def test_recording_exception_preserves_prefix_and_latches_without_retry(self):
        self.assertTrue(self.record())
        before = self.recorder.snapshot()
        with patch.object(obs, "_encode_event", side_effect=RuntimeError("secret-error")) as encode:
            self.assertFalse(self.record())
            self.assertFalse(self.record())
            self.assertIsNone(self.recorder.begin_request())
            self.assertEqual(encode.call_count, 1)
        after = self.recorder.snapshot()
        self.assertEqual(after.events, before.events)
        self.assertEqual(after.byte_count, before.byte_count)
        self.assertIs(after.state, obs.RecorderState.RECORD_ERROR)
        self.assertNotIn("secret-error", repr(after))
        self.assertFalse(self.record())

    def test_malformed_encoding_fails_closed(self):
        for value in (None, "secret", b""):
            recorder = obs.DevRequestRecorder()
            handle = recorder.begin_request()
            with patch.object(obs, "_encode_event", return_value=value):
                self.assertFalse(recorder.record(handle, obs.EventKind.ADAPTER_ENTER))
            self.assertIs(recorder.snapshot().state, obs.RecorderState.RECORD_ERROR)
            self.assertEqual(recorder.snapshot().byte_count, 0)

    def test_entropy_failure_and_collision_have_no_retry(self):
        for effect in (RuntimeError("secret-entropy"), MemoryError("secret-memory")):
            recorder = obs.DevRequestRecorder()
            with patch.object(obs, "uuid4", side_effect=effect) as entropy:
                self.assertIsNone(recorder.begin_request())
                self.assertIsNone(recorder.begin_request())
                entropy.assert_called_once()
            self.assertIs(recorder.snapshot().state, obs.RecorderState.RECORD_ERROR)
        recorder = obs.DevRequestRecorder()
        handle = recorder.begin_request()
        with patch.object(obs, "uuid4", return_value=UUID(hex=handle.correlation_id)) as entropy:
            self.assertIsNone(recorder.begin_request())
            entropy.assert_called_once()
        self.assertIs(recorder.snapshot().state, obs.RecorderState.RECORD_ERROR)

    def test_event_construction_failure_is_atomic(self):
        self.assertTrue(self.record())
        before = self.recorder.snapshot()
        with patch.object(obs, "Event", side_effect=MemoryError("secret-memory")) as event:
            self.assertFalse(self.record())
            self.assertFalse(self.record())
            event.assert_called_once()
        after = self.recorder.snapshot()
        self.assertEqual(after.events, before.events)
        self.assertEqual(after.byte_count, before.byte_count)
        self.assertIs(after.state, obs.RecorderState.RECORD_ERROR)

    def test_initialization_failure_is_sanitized(self):
        for failure in (RuntimeError("synthetic-secret"), MemoryError("synthetic-secret")):
            with self.subTest(failure_type=type(failure).__name__):
                with patch.object(obs, "uuid4", side_effect=failure) as entropy:
                    with self.assertRaisesRegex(RuntimeError, "^recorder unavailable$") as caught:
                        obs.DevRequestRecorder()
                    entropy.assert_called_once()
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn("synthetic-secret", "".join(
                    traceback.format_exception(caught.exception)))

    def test_initialization_failure_can_retain_callers_active_exception(self):
        ambient = ValueError("synthetic-caller-secret")
        try:
            raise ambient
        except ValueError:
            with patch.object(obs, "uuid4", side_effect=RuntimeError("synthetic-entropy-secret")):
                with self.assertRaisesRegex(RuntimeError, "^recorder unavailable$") as caught:
                    obs.DevRequestRecorder()
            self.assertIs(caught.exception.__context__, ambient)
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn("synthetic-caller-secret", rendered)
            self.assertNotIn("synthetic-entropy-secret", rendered)

    def test_restart_changes_epoch_and_resets_sequence(self):
        self.assertTrue(self.record())
        fresh = obs.DevRequestRecorder()
        handle = fresh.begin_request()
        self.assertNotEqual(fresh.snapshot().epoch, self.recorder.snapshot().epoch)
        self.assertNotEqual(handle.correlation_id, self.handle.correlation_id)
        self.assertEqual(fresh.snapshot().events, ())
        self.assertTrue(fresh.record(handle, obs.EventKind.ADAPTER_ENTER))
        self.assertEqual(fresh.snapshot().events[0].sequence, 1)

    def test_concurrent_writers_readers_and_overflow(self):
        # Scheduling order is intentionally unspecified; assertions are invariant.
        def worker(_):
            results = []
            for _ in range(32):
                results.append(self.record())
                snapshot = self.recorder.snapshot()
                self.assertEqual([event.sequence for event in snapshot.events],
                                 list(range(1, len(snapshot.events) + 1)))
                self.assertEqual(snapshot.byte_count, sum(
                    len(obs._encode_event(event)) for event in snapshot.events))
            return sum(results)

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(worker, range(10))), 256)
        snapshot = self.recorder.snapshot()
        self.assertEqual(len(snapshot.events), 256)
        self.assertIs(snapshot.state, obs.RecorderState.FULL)
        self.assertLessEqual(snapshot.byte_count, 65536)

    def test_concurrent_handle_issuance_is_unique_and_bounded(self):
        recorder = obs.DevRequestRecorder()
        with ThreadPoolExecutor(max_workers=8) as pool:
            handles = list(pool.map(lambda _: recorder.begin_request(), range(300)))
        accepted = [handle for handle in handles if handle is not None]
        self.assertEqual(len(accepted), 256)
        self.assertEqual(len({handle.correlation_id for handle in accepted}), 256)
        self.assertIs(recorder.snapshot().state, obs.RecorderState.FULL)

    def test_encoding_contains_only_fixed_schema(self):
        self.assertTrue(self.record())
        event = self.recorder.snapshot().events[0]
        decoded = json.loads(obs._encode_event(event))
        self.assertEqual(decoded, dict(epoch=event.epoch, sequence=1,
                         correlation_id=self.handle.correlation_id, kind="ADAPTER_ENTER",
                         outcome="UNKNOWN", reason="NONE"))


if __name__ == "__main__":
    unittest.main()
