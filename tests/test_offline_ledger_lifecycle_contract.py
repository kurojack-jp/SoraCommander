"""Pure, synthetic contract tests. No Windows service/pipe/Broker acceptance."""
import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest

from sora_commander_next.offline_ledger_lifecycle_contract import (
    BudgetExceeded, Health, LifecycleStatus, ObservationBudget, RequestOutcome,
    RecoveryEvent, RegistrationPhase, RegistrationState,
    RegistrationComparison, compare_registration_data,
)


class ObservationBudgetTests(unittest.TestCase):
    def test_frozen_clock_cannot_create_infinite_observation_budget(self):
        budget = ObservationBudget(started_at=10)
        for count in range(1, 31):
            budget = budget.reserve_observation(10)
            self.assertEqual(budget.observations, count)
        for _ in range(3):
            with self.assertRaises(BudgetExceeded):
                budget.reserve_observation(10)
        self.assertEqual(budget.remaining_seconds(10), 0)

    def test_progress_does_not_extend_deadline(self):
        budget = ObservationBudget(started_at=10)
        for now in (10, 15, 20, 24.9):
            budget = budget.reserve_observation(now)
        self.assertAlmostEqual(budget.remaining_seconds(24.9), 0.1)
        with self.assertRaises(BudgetExceeded):
            budget.reserve_observation(25)
        with self.assertRaises(BudgetExceeded):
            budget.reserve_observation(26)
        self.assertEqual(budget.started_at, 10)

    def test_reservation_charged_before_callback_failure(self):
        budget = ObservationBudget(started_at=0, maximum_observations=1)
        budget = budget.reserve_observation(0)
        # Even if the work never succeeds, the charged budget is exhausted.
        with self.assertRaises(BudgetExceeded):
            budget.reserve_observation(0)

    def test_backward_time_rejected(self):
        budget = ObservationBudget(started_at=10).reserve_observation(12)
        for now in (9, 11):
            with self.subTest(now=now), self.assertRaises(ValueError):
                budget.reserve_observation(now)
            with self.assertRaises(ValueError):
                budget.remaining_seconds(now)

    def test_invalid_numbers_rejected_at_every_timestamp_boundary(self):
        invalid = (True, False, None, "1", -1, float("nan"),
                   float("inf"), float("-inf"), 10**1000)
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    ObservationBudget(started_at=value)
                with self.assertRaises(ValueError):
                    ObservationBudget(started_at=0, timeout_seconds=value)
                budget = ObservationBudget(started_at=0)
                with self.assertRaises(ValueError):
                    budget.reserve_observation(value)

    def test_invalid_counts_and_timeout_rejected(self):
        for value in (True, 0, -1, 1001, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ObservationBudget(started_at=0, maximum_observations=value)
        for value in (0, 3601):
            with self.assertRaises(ValueError):
                ObservationBudget(started_at=0, timeout_seconds=value)
        for fields in ({"observations": True}, {"observations": -1},
                       {"observations": 31, "last_observed_at": 0},
                       {"observations": 1}, {"last_observed_at": 0},
                       {"observations": 1, "last_observed_at": 15}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ObservationBudget(started_at=0, **fields)
        with self.assertRaises(ValueError):
            ObservationBudget(started_at=10, observations=1, last_observed_at=9)

    def test_zero_start_and_exact_count_boundary(self):
        budget = ObservationBudget(started_at=0, timeout_seconds=1,
                                   maximum_observations=2)
        budget = budget.reserve_observation(0).reserve_observation(0.5)
        self.assertEqual(budget.observations, 2)
        with self.assertRaises(BudgetExceeded):
            budget.reserve_observation(0.5)

    def test_immutable_snapshot_and_no_deadline_reset_api(self):
        budget = ObservationBudget(started_at=0)
        with self.assertRaises(FrozenInstanceError):
            budget.started_at = 10
        charged = budget.reserve_observation(1)
        self.assertEqual(budget.observations, 0)
        self.assertEqual(charged.observations, 1)
        self.assertEqual(charged.started_at, 0)
        self.assertFalse(hasattr(charged, "reset"))


class DiagnosticTests(unittest.TestCase):
    def test_health_recovery_does_not_clear_unknown(self):
        status = LifecycleStatus(Health.RECOVERY_REQUIRED,
                                 RequestOutcome.OUTCOME_UNKNOWN)
        for health in Health:
            for outcome in RequestOutcome:
                observed = status.observe_health(health).observe_outcome(outcome)
                self.assertEqual(observed.health, health)
                self.assertEqual(observed.outcome, RequestOutcome.OUTCOME_UNKNOWN)

    def test_invalid_health_outcome_never_accepted(self):
        status = LifecycleStatus(Health.HEALTHY)
        for value in (None, True, "HEALTHY", {}, 0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    status.observe_health(value)
                with self.assertRaises(ValueError):
                    status.observe_outcome(value)
        with self.assertRaises(ValueError):
            LifecycleStatus(Health.HEALTHY, "OUTCOME_UNKNOWN")

    def test_model_has_only_inert_standard_library_dependencies(self):
        import sora_commander_next.offline_ledger_lifecycle_contract as model
        tree = ast.parse(Path(model.__file__).read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module)
        self.assertEqual(modules, {"dataclasses", "enum", "math", "re"})


class RegistrationRecoveryTests(unittest.TestCase):
    OLD = "a" * 32
    NEW = "b" * 32

    def records(self):
        state = RegistrationState(self.OLD)
        result = [state]
        for event in RecoveryEvent:
            arguments = ({"replacement_generation": self.NEW}
                         if event is RecoveryEvent.BEGIN else {})
            state = state.advance(event, **arguments)
            result.append(state)
        return result

    def test_crash_reload_at_each_phase_never_restores_old_active(self):
        states = self.records()
        for index, state in enumerate(states):
            restored = RegistrationState.from_record(state.to_record())
            with self.subTest(phase=restored.phase, index=index):
                self.assertEqual(restored, state)
                self.assertEqual(restored.models_current_generation(self.OLD), index == 0)
                self.assertEqual(restored.models_current_generation(self.NEW), index == 4)
                self.assertFalse(restored.models_current_generation("c" * 32))

    def test_all_skipped_repeated_and_reverse_transitions_rejected(self):
        states = self.records()
        valid = {RegistrationPhase.ACTIVE: RecoveryEvent.BEGIN,
                 RegistrationPhase.QUIESCING: RecoveryEvent.QUIESCENCE_OBSERVED,
                 RegistrationPhase.REVOKED: RecoveryEvent.REPLACEMENT_PREPARED,
                 RegistrationPhase.PREPARED: RecoveryEvent.ACTIVATE}
        for state in states:
            for event in RecoveryEvent:
                if event is valid[state.phase]:
                    continue
                arguments = ({"replacement_generation": "c" * 32}
                             if event is RecoveryEvent.BEGIN else {})
                with self.subTest(phase=state.phase, event=event), self.assertRaises(ValueError):
                    state.advance(event, **arguments)

    def test_pending_target_cannot_change_and_generation_cannot_be_reused(self):
        active = RegistrationState(self.OLD)
        for generation in (None, self.OLD):
            with self.assertRaises(ValueError):
                active.advance(RecoveryEvent.BEGIN, replacement_generation=generation)
        for state, event in zip(self.records()[1:4], list(RecoveryEvent)[1:]):
            for generation in (self.NEW, "c" * 32):
                with self.assertRaises(ValueError):
                    state.advance(event, replacement_generation=generation)

    def test_invalid_record_schema_rejected(self):
        record = RegistrationState(self.OLD).to_record()
        mutations = ({**record, "version": True}, {**record, "version": 2},
                     {**record, "unexpected": 0}, {**record, "phase": True},
                     {**record, "phase": "UNKNOWN"}, {**record, "generation": self.OLD.upper()},
                     {**record, "replacement_generation": self.NEW},
                     {**record, "phase": "QUIESCING"})
        for value in (None, [], *mutations):
            with self.subTest(record=value), self.assertRaises(ValueError):
                RegistrationState.from_record(value)
        for key in record:
            missing = {name: value for name, value in record.items() if name != key}
            with self.assertRaises(ValueError):
                RegistrationState.from_record(missing)

    def test_invalid_identifiers_and_event_types_rejected(self):
        for value in (True, None, 1, "", "a" * 31, "a" * 33, "g" * 32, b"a" * 32):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    RegistrationState(value)
                with self.assertRaises(ValueError):
                    RegistrationState(self.OLD).models_current_generation(value)
        for value in (True, None, "BEGIN", 1):
            with self.assertRaises(ValueError):
                RegistrationState(self.OLD).advance(value, replacement_generation=self.NEW)
        with self.assertRaises(ValueError):
            RegistrationState(self.OLD, "ACTIVE")

    def test_serialized_copy_is_detached_and_model_has_no_repair_side_effect(self):
        state = self.records()[2]
        record = state.to_record()
        record["phase"] = "ACTIVE"
        self.assertEqual(state.phase, RegistrationPhase.REVOKED)
        with self.assertRaises(ValueError):
            RegistrationState.from_record(record)
        with self.assertRaises(FrozenInstanceError):
            state.phase = RegistrationPhase.ACTIVE
        self.assertFalse(hasattr(state, "reset"))
        self.assertFalse(hasattr(state, "persist"))

    def test_future_recovery_needs_another_generation(self):
        active = self.records()[-1]
        for generation in (self.OLD, self.NEW):
            with self.assertRaises(ValueError):
                active.advance(RecoveryEvent.BEGIN, replacement_generation=generation)
        self.assertEqual(active.retired_generations, (self.OLD,))
        later = active.advance(RecoveryEvent.BEGIN, replacement_generation="c" * 32)
        self.assertFalse(later.models_current_generation(self.NEW))

    def test_retirement_history_is_bounded_and_never_evicted(self):
        retired = tuple(f"{number:032x}" for number in range(1024))
        active = RegistrationState(self.NEW, retired_generations=retired)
        restored = RegistrationState.from_record(active.to_record())
        self.assertEqual(restored.retired_generations, retired)
        with self.assertRaises(ValueError):
            restored.advance(RecoveryEvent.BEGIN, replacement_generation="c" * 32)
        for invalid in ([self.OLD], (self.NEW,), (self.OLD, self.OLD),
                        retired + ("c" * 32,), (True,)):
            with self.assertRaises(ValueError):
                RegistrationState(self.NEW, retired_generations=invalid)


class RegistrationComparisonTests(unittest.TestCase):
    INSTALLATION = "1" * 32

    def compare(self, anchor, database, **overrides):
        arguments = dict(expected_installation=self.INSTALLATION,
                         anchor_installation=self.INSTALLATION,
                         database_installation=self.INSTALLATION,
                         anchor=anchor, database=database)
        arguments.update(overrides)
        return compare_registration_data(**arguments)

    def test_all_pairs_of_recovery_write_boundaries(self):
        old = RegistrationState("a" * 32)
        quiescing = old.advance(RecoveryEvent.BEGIN, replacement_generation="b" * 32)
        revoked = quiescing.advance(RecoveryEvent.QUIESCENCE_OBSERVED)
        prepared = revoked.advance(RecoveryEvent.REPLACEMENT_PREPARED)
        new = prepared.advance(RecoveryEvent.ACTIVATE)
        states = [old, quiescing, revoked, prepared, new]
        # Explicit oracle: only the two stable endpoints match. This tests
        # observations at write boundaries, not filesystem crash durability.
        for left, anchor in enumerate(states):
            for right, database in enumerate(states):
                with self.subTest(anchor=left, database=right):
                    matched = self.compare(anchor, database) is RegistrationComparison.MATCHING_ACTIVE_DATA
                    self.assertEqual(matched, (left, right) in {(0, 0), (4, 4)})

    def test_missing_is_never_provisioning_or_matching(self):
        state = RegistrationState("a" * 32)
        for field in ("anchor", "database", "anchor_installation", "database_installation"):
            arguments = dict(anchor=state, database=state)
            arguments[field] = None
            with self.subTest(field=field):
                self.assertIs(self.compare(**arguments), RegistrationComparison.MISSING_RECORD)

    def test_installation_binding_and_retirement_history(self):
        state = RegistrationState("a" * 32)
        for field in ("anchor_installation", "database_installation", "expected_installation"):
            self.assertIs(self.compare(state, state, **{field: "2" * 32}),
                          RegistrationComparison.IDENTITY_MISMATCH)
        different_history = RegistrationState("a" * 32, retired_generations=("b" * 32,))
        self.assertIs(self.compare(state, different_history), RegistrationComparison.HISTORY_MISMATCH)
        self.assertIs(self.compare(different_history, state), RegistrationComparison.HISTORY_MISMATCH)

    def test_bad_observation_is_rejected_even_if_another_is_missing(self):
        state = RegistrationState("a" * 32)
        for value in (False, {}, "ACTIVE", 0):
            with self.assertRaises(ValueError):
                self.compare(None, value)
        for field in ("expected_installation", "anchor_installation", "database_installation"):
            for value in (False, 1, "", "A" * 32):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.compare(state, state, **{field: value})

    def test_matching_stale_pair_is_explicitly_not_rollback_detection(self):
        stale = RegistrationState("a" * 32)
        original = stale.to_record()
        result = self.compare(stale, RegistrationState.from_record(original))
        self.assertIs(result, RegistrationComparison.MATCHING_ACTIVE_DATA)
        self.assertEqual(stale.to_record(), original)
        self.assertNotIsInstance(result, Health)
        self.assertNotIsInstance(result, RequestOutcome)


if __name__ == "__main__":
    unittest.main()
