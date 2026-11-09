"""Pure cursor/connection captures. No SQLite connection or filesystem fixture."""
from dataclasses import replace
from hashlib import sha256
import unittest

from test_protected_ledger_client import command
from sora_commander_next.offline_b01_full_binding import make_wire_request, _canonical, decode_wire_request, MAX_WIRE
from sora_commander_next import service_spent_ledger_candidate as ledger


class Cursor:
    def __init__(self, row=None, rows=None, rowcount=1):
        self.row, self.rows, self.rowcount = row, rows, rowcount
    def fetchone(self):
        return self.row
    def fetchmany(self, n):
        return self.rows[:n]


class FakeConnection:
    isolation_level = None
    in_transaction = False
    def __init__(self):
        self.events = []
        self.anchor = ("1"*32, "2"*32, 1, 1)
        self.schema = list(ledger.SCHEMA_RECORDS)
        self.existing, self.reuse, self.count = None, None, 0
        self.rowcount = 1
        self.fail = None
        self.on_commit = None
        self.policy = {"PRAGMA application_id": ledger.APPLICATION_ID,
            "PRAGMA user_version": 1, "PRAGMA synchronous": 2,
            "PRAGMA busy_timeout": 0, "PRAGMA foreign_keys": 1,
            "PRAGMA journal_mode": "delete"}

    def execute(self, sql, parameters=()):
        self.events.append((sql, parameters))
        if self.fail == sql:
            raise OSError("injected database failure")
        if sql in self.policy:
            return Cursor((self.policy[sql],))
        if sql == "BEGIN IMMEDIATE":
            self.in_transaction = True
        if sql.startswith("SELECT name,sql"):
            return Cursor(rows=self.schema)
        if sql == ledger.ANCHOR_SQL:
            return Cursor(self.anchor)
        if sql == ledger.LOOKUP_SQL:
            return Cursor(self.existing)
        if sql == ledger.REUSE_SQL:
            return Cursor(self.reuse)
        if sql.startswith("SELECT COUNT"):
            return Cursor((self.count,))
        return Cursor(rowcount=self.rowcount)

    def commit(self):
        self.events.append(("COMMIT", ()))
        if self.on_commit:
            self.on_commit()
        if self.fail == "COMMIT":
            raise OSError("ambiguous commit ACK")
        self.in_transaction = False

    def rollback(self):
        self.events.append(("ROLLBACK", ()))
        if self.fail == "ROLLBACK":
            raise OSError("rollback uncertain")
        self.in_transaction = False


class ServiceSpentLedgerCandidateTests(unittest.TestCase):
    def setUp(self):
        self.raw = make_wire_request(command(), operation_id="8"*32)
        self.db = FakeConnection()
        self.now = 100
        self.fence = ledger.GenerationFenceCandidate("1"*32, "2"*32, 1)
        self.adapter = ledger.ServiceSpentLedgerCandidate(self.fence,
            _connection=self.db, _clock_ms=lambda: self.now)

    def call(self, **kw):
        return self.adapter.observe_data_only(self.raw, **kw)

    def sql(self):
        return [entry[0] for entry in self.db.events]

    def update_request(self, **kw):
        return _canonical({**decode_wire_request(self.raw), **kw}, MAX_WIRE)

    def test_default_entrypoint_disabled_without_connect(self):
        result = ledger.ServiceSpentLedgerCandidate(self.fence).observe_data_only(self.raw)
        self.assertEqual(result.status, "NOT_ENABLED")

    def test_commit_before_data_receipt_and_parameterized_exact_binding(self):
        result = self.call()
        self.assertEqual(result.status, "DATA_COMMIT_OBSERVED")
        self.assertEqual(self.sql()[-1], "COMMIT")
        self.assertLess(self.sql().index("BEGIN IMMEDIATE"), self.sql().index(ledger.LOOKUP_SQL))
        self.assertLess(self.sql().index(ledger.INSERT_SQL), self.sql().index("COMMIT"))
        params = next(p for s, p in self.db.events if s == ledger.INSERT_SQL)
        self.assertEqual(params[-2:], (sha256(self.raw).hexdigest(), self.raw))
        self.assertEqual(result.request_bytes, self.raw)
        self.assertEqual(result.authority, "NONE_DATA_ONLY")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertEqual(result.acceptance, "NOT_OS_ACCEPTED")

    def test_commit_callback_cannot_reenter_or_get_receipt_early(self):
        nested = []
        self.db.on_commit = lambda: nested.append(self.call().status)
        self.assertEqual(self.call().status, "DATA_COMMIT_OBSERVED")
        self.assertEqual(nested, ["DATA_DENIED_BUSY_OR_WRONG_THREAD"])

    def test_duplicate_is_terminal_with_zero_second_sql(self):
        self.call()
        count = len(self.db.events)
        self.assertEqual(self.call().status, "DATA_TERMINAL")
        self.assertEqual(len(self.db.events), count)

    def test_operation_and_challenge_reuse_denied(self):
        self.call()
        for changes in ({"request_id": "a"*32},
                        {"request_id": "b"*32, "operation_id": "a"*32}):
            result = self.adapter.observe_data_only(self.update_request(**changes))
            self.assertEqual(result.status, "DATA_DENIED_REUSE_OR_FULL")

    def test_generation_mismatch_has_zero_sql(self):
        result = self.adapter.observe_data_only(self.update_request(generation="f"*32))
        self.assertEqual(result.status, "DATA_DENIED_GENERATION")
        self.assertEqual(self.db.events, [])

    def test_missing_or_stale_anchor_rolls_back_and_halts(self):
        for anchor in (None, ("1"*32, "2"*32, 0, 1), ("1"*32, "2"*32, 1, 2),
                       ("1"*32, "2"*32, 2, 2), ("1"*32, "2"*32, True, True)):
            with self.subTest(anchor=anchor):
                self.setUp()
                self.db.anchor = anchor
                self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
                self.assertEqual(self.sql()[-1], "ROLLBACK")
                self.assertNotIn(ledger.INSERT_SQL, self.sql())
                count = len(self.db.events)
                self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
                self.assertEqual(len(self.db.events), count)

    def test_bad_schema_denies_without_provisioning(self):
        self.db.schema = []
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
        self.assertFalse(any(s.startswith(("CREATE", "DROP", "ALTER")) for s in self.sql()))

    def test_wrong_pragma_policies_fail_closed(self):
        for sql, value in (("PRAGMA synchronous", 1), ("PRAGMA journal_mode", "wal"),
                           ("PRAGMA application_id", 0), ("PRAGMA busy_timeout", 100),
                           ("PRAGMA foreign_keys", 0), ("PRAGMA user_version", True)):
            with self.subTest(sql=sql):
                self.setUp()
                self.db.policy[sql] = value
                self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
                self.assertNotIn(ledger.INSERT_SQL, self.sql())
                if sql == "PRAGMA busy_timeout":
                    self.assertNotIn("BEGIN IMMEDIATE", self.sql())

    def test_begin_and_insert_and_commit_failure_are_terminal(self):
        for stage in ("BEGIN IMMEDIATE", ledger.INSERT_SQL, "COMMIT"):
            with self.subTest(stage=stage):
                self.setUp()
                self.db.fail = stage
                self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
                self.assertEqual(self.sql()[-1], "ROLLBACK")
                count = len(self.db.events)
                self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
                self.assertEqual(len(self.db.events), count)

    def test_rollback_failure_never_rearms(self):
        self.db.schema = []
        self.db.fail = "ROLLBACK"
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")

    def test_late_commit_ack_is_unknown(self):
        self.db.on_commit = lambda: setattr(self, "now", 1100)
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
        self.assertNotIn("ROLLBACK", self.sql())

    def test_clock_rollback_halts(self):
        self.db.on_commit = lambda: setattr(self, "now", 99)
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")

    def test_full_ledger_never_deletes_spent_rows(self):
        self.db.count = 65536
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
        self.assertNotIn(ledger.INSERT_SQL, self.sql())
        self.assertFalse(any(s.startswith("DELETE") for s in self.sql()))

    def test_existing_binding_returns_spent_not_fresh(self):
        self.db.existing = (sha256(self.raw).hexdigest(), self.raw)
        self.assertEqual(self.call().status, "DATA_SPENT")
        self.assertNotIn(ledger.INSERT_SQL, self.sql())

    def test_conflicting_binding_and_operation_are_terminal(self):
        for row, reuse in ((("f"*64, b"bad"), None), (None, ("a"*32,))):
            self.setUp()
            self.db.existing, self.db.reuse = row, reuse
            self.assertEqual(self.call().status, "DATA_CONFLICT")
            self.assertNotIn(ledger.INSERT_SQL, self.sql())

    def test_query_recovery_compares_original_consume_without_inserting(self):
        self.db.existing = (sha256(self.raw).hexdigest(), self.raw)
        query = self.update_request(action="QUERY")
        self.assertEqual(self.adapter.observe_data_only(query).status, "DATA_SPENT")
        self.assertNotIn(ledger.INSERT_SQL, self.sql())

    def test_absent_recovery_cannot_produce_fresh_receipt(self):
        self.assertEqual(self.adapter.observe_data_only(self.update_request(action="QUERY")).status, "DATA_ABSENT")
        self.assertNotIn(ledger.INSERT_SQL, self.sql())
        self.assertEqual(self.call().status, "DATA_TERMINAL")

    def test_invalid_insert_count_rolls_back(self):
        self.db.rowcount = True
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
        self.assertEqual(self.sql()[-1], "ROLLBACK")

    def test_connection_with_existing_transaction_denied(self):
        self.db.in_transaction = True
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")
        self.assertEqual(self.db.events, [])

    def test_interrupt_rolls_back_and_propagates_with_halt(self):
        def interrupt():
            raise KeyboardInterrupt()
        self.db.on_commit = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.call()
        self.assertEqual(self.sql()[-1], "ROLLBACK")
        self.assertEqual(self.call().status, "OUTCOME_UNKNOWN")

    def test_bounded_history_does_not_evict(self):
        self.adapter._maximum = 1
        self.call()
        result = self.adapter.observe_data_only(self.update_request(request_id="a"*32,
            operation_id="b"*32, challenge="c"*64))
        self.assertEqual(result.status, "DATA_DENIED_REUSE_OR_FULL")

    def test_sql_schema_has_per_generation_unique_keys_and_no_implicit_genesis(self):
        sql = ledger.SCHEMA["spent"]
        for clause in ("PRIMARY KEY(installation_id,generation,request_id)",
                       "UNIQUE(installation_id,generation,operation_id)",
                       "UNIQUE(installation_id,generation,challenge)"):
            self.assertIn(clause, sql)
        self.assertEqual(len(ledger.SCHEMA_RECORDS), 7)

    def test_bool_generation_number_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.fence, generation_number=True)

    def test_recreated_adapter_reads_spent_binding_without_second_insert(self):
        self.call()
        self.db.existing = (sha256(self.raw).hexdigest(), self.raw)
        recreated = ledger.ServiceSpentLedgerCandidate(self.fence,
            _connection=self.db, _clock_ms=lambda: self.now)
        self.assertEqual(recreated.observe_data_only(self.raw).status, "DATA_SPENT")
        self.assertEqual(self.sql().count(ledger.INSERT_SQL), 1)

    def test_unprotected_same_generation_snapshot_has_no_authority(self):
        # Fake storage cannot prove rollback resistance. Assert the closed
        # authority boundary even when an empty old snapshot looks consistent.
        result = self.call()
        self.assertEqual(result.authority, "NONE_DATA_ONLY")
        self.assertEqual(result.acceptance, "NOT_OS_ACCEPTED")
        self.assertNotEqual(result.status, "COMMITTED_FRESH")

    def test_other_thread_identifier_refuses_sql_without_spawning_thread(self):
        from unittest.mock import patch
        with patch.object(ledger, "get_ident", return_value=self.adapter._owner + 1):
            self.assertEqual(self.call().status, "DATA_DENIED_BUSY_OR_WRONG_THREAD")
        self.assertEqual(self.db.events, [])
