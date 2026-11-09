"""Synthetic DATA_ONLY tests; no durable restart, Owner or live G2 acceptance."""
import ast
from dataclasses import FrozenInstanceError, fields
import json
from pathlib import Path
import unittest

import offline_operation_intent_contract_model as model
from offline_operation_intent_contract_model import (
    IntentHeld, IntentState, OperationIntent,
)


OP = "a" * 32
REQUEST = "b" * 32
OTHER = "c" * 32
BINDING = "d" * 64


class OperationIntentTests(unittest.TestCase):
    def new(self):
        return OperationIntent(OP, BINDING)

    def states(self):
        new = self.new()
        reserved = new.reserve_data_only(REQUEST)
        unknown = reserved.mark_unknown_data_only()
        return [new, reserved, unknown, unknown.mark_terminal_data_only()]

    def test_initial_state_is_inert_without_request(self):
        intent = self.new()
        self.assertIs(intent.state, IntentState.NEW)
        self.assertIsNone(intent.request_id)
        self.assertEqual(intent.inspect_data_only(), "NEW_INERT_DATA_ONLY")

    def test_first_reservation_records_request_and_preserves_original(self):
        original = self.new()
        reserved = original.reserve_data_only(REQUEST)
        self.assertIs(reserved.state, IntentState.RESERVED_UNKNOWN)
        self.assertEqual(reserved.request_id, REQUEST)
        self.assertEqual(reserved.operation_id, OP)
        self.assertNotEqual(reserved.request_id, reserved.operation_id)
        self.assertEqual(reserved.command_binding, BINDING)
        self.assertIsNone(original.request_id)

    def test_repeated_same_request_holds_and_retains_evidence(self):
        for intent in self.states()[1:]:
            snapshot = intent.serialize()
            with self.assertRaisesRegex(IntentHeld, "^HOLD_"):
                intent.reserve_data_only(REQUEST)
            self.assertEqual(intent.serialize(), snapshot)

    def test_fresh_request_same_operation_holds_and_retains_original(self):
        for intent in self.states()[1:]:
            with self.assertRaises(IntentHeld):
                intent.reserve_data_only(OTHER)
            self.assertEqual(intent.request_id, REQUEST)
            self.assertTrue(intent.inspect_data_only().startswith("HOLD_"))

    def test_unknown_after_reserve_survives_json_reconstruction(self):
        unknown = self.new().reserve_data_only(REQUEST).mark_unknown_data_only()
        restored = OperationIntent.from_data_only_snapshot(
            json.loads(json.dumps(unknown.serialize())))
        self.assertEqual(restored, unknown)
        self.assertIs(restored.state, IntentState.UNKNOWN_HELD)
        self.assertEqual(restored.request_id, REQUEST)
        for request in (REQUEST, OTHER):
            with self.assertRaises(IntentHeld):
                restored.reserve_data_only(request)

    def test_terminal_from_each_unknown_state_never_grants_reuse(self):
        for intent in self.states()[1:3]:
            terminal = intent.mark_terminal_data_only()
            restored = OperationIntent.from_data_only_snapshot(terminal.serialize())
            self.assertIs(restored.state, IntentState.CONFIRMED_TERMINAL)
            self.assertEqual(restored.request_id, REQUEST)
            for request in (REQUEST, OTHER):
                with self.assertRaises(IntentHeld):
                    restored.reserve_data_only(request)

    def test_all_snapshots_are_detached_string_only_roundtrips(self):
        for intent in self.states():
            snapshot = intent.serialize()
            self.assertTrue(all(type(k) is str and type(v) is str
                                for k, v in snapshot.items()))
            self.assertEqual(OperationIntent.from_data_only_snapshot(snapshot), intent)
            snapshot["request_id"] = OTHER
            self.assertNotEqual(snapshot, intent.serialize())

    def test_malformed_forged_snapshot_rejected(self):
        snapshot = self.new().serialize()
        forged = [None, [], {}, {**snapshot, "version": 1},
                  {**snapshot, "version": "2"}, {**snapshot, "owner": "approved"},
                  {**snapshot, "state": "APPROVED"},
                  {**snapshot, "state": "RUNNING"},
                  {**snapshot, "request_id": REQUEST},
                  {**snapshot, "state": "UNKNOWN_HELD"},
                  {**snapshot, "state": "CONFIRMED_TERMINAL"},
                  {**snapshot, "command_binding": "secret" * 100},
                  {**snapshot, "operation_id": OP.upper()}]
        forged.extend({k: v for k, v in snapshot.items() if k != missing}
                      for missing in snapshot)
        for key in snapshot:
            for value in (None, True, 1, [], {}):
                forged.append({**snapshot, key: value})
        for value in forged:
            with self.subTest(value=value), self.assertRaises(ValueError):
                OperationIntent.from_data_only_snapshot(value)

    def test_wrong_types_and_malformed_identifiers_rejected(self):
        class StringSubclass(str):
            pass
        invalid = (None, True, 32, b"a" * 32, [], {}, "", "A" * 32,
                   "g" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n",
                   StringSubclass(OP), "x" * 10000)
        for value in invalid:
            with self.assertRaises(ValueError):
                OperationIntent(value, BINDING)
            for intent in self.states():
                with self.assertRaises(ValueError):
                    intent.reserve_data_only(value)
        for value in (*invalid, "d" * 63, "d" * 65, "D" * 64):
            with self.assertRaises(ValueError):
                OperationIntent(OP, value)

    def test_invalid_constructor_state_invariants_rejected(self):
        for state in (None, True, "NEW", "UNKNOWN_HELD", 0):
            with self.assertRaises(ValueError):
                OperationIntent(OP, BINDING, state)
        with self.assertRaises(ValueError):
            OperationIntent(OP, BINDING, request_id=REQUEST)
        for state in list(IntentState)[1:]:
            for request in (None, "", True, "X" * 32):
                with self.assertRaises(ValueError):
                    OperationIntent(OP, BINDING, state, request)

    def test_unknown_is_idempotent(self):
        unknown = self.states()[2]
        self.assertIs(unknown.mark_unknown_data_only(), unknown)

    def test_invalid_transitions_fail_closed(self):
        new, _, _, terminal = self.states()
        for intent in (new, terminal):
            with self.assertRaises(ValueError):
                intent.mark_unknown_data_only()
            with self.assertRaises(ValueError):
                intent.mark_terminal_data_only()

    def test_aggregate_is_immutable(self):
        intent = self.states()[2]
        for field in fields(intent):
            with self.assertRaises(FrozenInstanceError):
                setattr(intent, field.name, None)
        self.assertFalse(hasattr(intent, "__dict__"))

    def test_no_authorization_or_raw_command_fields_or_methods(self):
        self.assertEqual({field.name for field in fields(OperationIntent)},
                         {"operation_id", "command_binding", "state", "request_id"})
        public_methods = {name for name, value in vars(OperationIntent).items()
                          if not name.startswith("_") and
                          (callable(value) or isinstance(value, classmethod))}
        self.assertEqual(public_methods, {"reserve_data_only", "mark_unknown_data_only",
                         "mark_terminal_data_only", "inspect_data_only", "serialize",
                         "from_data_only_snapshot"})
        for field in ("argv", "secret", "command", "owner_proof", "retry_authorization"):
            with self.assertRaises(TypeError):
                OperationIntent(OP, BINDING, **{field: "untrusted"})

    def test_distinct_operation_same_binding_is_only_an_inert_new_record(self):
        old = self.states()[2]
        distinct = OperationIntent(OTHER, old.command_binding)
        self.assertNotEqual(old.operation_id, distinct.operation_id)
        self.assertIsNone(distinct.request_id)
        self.assertEqual(distinct.inspect_data_only(), "NEW_INERT_DATA_ONLY")
        self.assertEqual(old.request_id, REQUEST)
        self.assertIn("separate trusted Owner authorization", model.__doc__)

    def test_transition_graph_is_monotonic_with_original_identity(self):
        expected = (IntentState.NEW, IntentState.RESERVED_UNKNOWN,
                    IntentState.UNKNOWN_HELD, IntentState.CONFIRMED_TERMINAL)
        for intent, state in zip(self.states(), expected):
            self.assertIs(intent.state, state)
            self.assertEqual((intent.operation_id, intent.command_binding), (OP, BINDING))
            self.assertEqual(intent.request_id, None if state is IntentState.NEW else REQUEST)
        self.assertEqual({state.value for state in IntentState}, {s.value for s in expected})

    def test_invalid_input_errors_never_echo_raw_contents(self):
        secret = "--token=private-command-contents"
        for invoke in (lambda: OperationIntent(secret, BINDING),
                       lambda: self.new().reserve_data_only(secret),
                       lambda: OperationIntent.from_data_only_snapshot(
                           {**self.new().serialize(), "state": secret})):
            with self.assertRaises(ValueError) as raised:
                invoke()
            self.assertNotIn(secret, str(raised.exception))

    def test_source_interface_has_no_broker_fence_or_job_dispatch_connection(self):
        # Source-level dependency/interface check only; never import live modules.
        source = Path(model.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imports.add(node.module)
        self.assertEqual(imports, {"dataclasses", "enum", "re"})
        for name in ("BrokerLaunchFence", "broker_launch_fence", "job_dispatch",
                     "subprocess", "__import__", "eval(", "exec("):
            self.assertNotIn(name, source)
        self.assertIn("DATA_ONLY", ast.get_docstring(tree))
        self.assertLessEqual(len(source.splitlines()), 220)
        self.assertLessEqual(len(Path(__file__).read_text(encoding="utf-8").splitlines()), 300)


    def test_immutable_copies_are_not_a_global_double_reservation_guard(self):
        # Deliberately demonstrate the missing atomic persistent Owner/Broker boundary.
        original = self.new()
        first = original.reserve_data_only(REQUEST)
        second = original.reserve_data_only(OTHER)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertIs(original.state, IntentState.NEW)

    def test_coherent_snapshot_rollback_is_not_authenticated(self):
        reserved = self.new().reserve_data_only(REQUEST).mark_unknown_data_only()
        rollback = reserved.serialize()
        rollback["state"] = "NEW"
        rollback["request_id"] = ""
        reconstructed = OperationIntent.from_data_only_snapshot(rollback)
        self.assertEqual(reconstructed, self.new())
        self.assertIs(reconstructed.state, IntentState.NEW)
        # Only protected durable Broker storage could detect or deny this forgery.

    def test_same_operation_id_can_be_recreated_with_other_binding(self):
        first = self.new().reserve_data_only(REQUEST)
        forged = OperationIntent(first.operation_id, "e" * 64)
        self.assertEqual(first.operation_id, forged.operation_id)
        self.assertNotEqual(first.command_binding, forged.command_binding)
        self.assertEqual(forged.inspect_data_only(), "NEW_INERT_DATA_ONLY")

    def test_terminal_is_only_an_unverified_data_claim(self):
        reserved = self.new().reserve_data_only(REQUEST)
        claimed = reserved.mark_terminal_data_only()
        self.assertEqual(claimed.inspect_data_only(),
                         "HOLD_TERMINAL_NO_REUSE_DATA_ONLY")
        # No OS evidence, Owner proof or capability is accepted or returned.
        self.assertEqual(set(claimed.serialize()),
                         {"version", "operation_id", "command_binding",
                          "state", "request_id"})

    def test_snapshot_rejects_subclassed_strings_and_foreign_enum(self):
        class TextSubclass(str):
            pass
        from enum import Enum
        class ForeignState(Enum):
            OTHER = "NEW"
        snapshot = self.new().serialize()
        for key in snapshot:
            with self.subTest(key=key), self.assertRaises(ValueError):
                OperationIntent.from_data_only_snapshot(
                    {**snapshot, key: TextSubclass(snapshot[key])})
        with self.assertRaises(ValueError):
            OperationIntent(OP, BINDING, ForeignState.OTHER)


if __name__ == "__main__":
    unittest.main()
