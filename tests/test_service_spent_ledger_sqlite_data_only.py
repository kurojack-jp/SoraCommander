"""Disposable real SQLite DATA_ONLY checks; no protected-service acceptance.

These user-writable fixtures prove neither provenance, sole-writer ownership,
Owner authentication nor persistent antirollback. No OS commands are executed.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_protected_ledger_client import command
from sora_commander_next import service_spent_ledger_candidate as ledger
from sora_commander_next.offline_b01_full_binding import (
    MAX_WIRE, _canonical, decode_wire_request, make_wire_request,
)


class ServiceSpentLedgerSQLiteDataOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A TEMP permission failure stops this class; never reroute TEMP or ACLs.
        cls.temp = tempfile.TemporaryDirectory(prefix="spent-data-only-")
        cls.addClassCleanup(cls.temp.cleanup)

    def setUp(self):
        guards = ExitStack()
        self.addCleanup(guards.close)
        for target in ("subprocess.Popen", "os.system", "os.startfile",
                       "ctypes.WinDLL"):
            guards.enter_context(patch(target, create=True,
                side_effect=AssertionError("OS command/native call forbidden")))
        self.path = Path(self.temp.name) / (self._testMethodName + ".sqlite")
        self.raw = make_wire_request(command(), operation_id="8" * 32)
        self.fence = ledger.GenerationFenceCandidate("1" * 32, "2" * 32, 1)
        self.now = 100
        self.db = self.connect()
        self.provision(self.db)
        self.adapter = self.adapter_for(self.db)

    def connect(self, path=None):
        db = sqlite3.connect(str(path or self.path), isolation_level=None)
        self.addCleanup(db.close)  # Runs before class TemporaryDirectory cleanup.
        for sql in ("PRAGMA journal_mode=DELETE", "PRAGMA synchronous=FULL",
                    "PRAGMA busy_timeout=0", "PRAGMA foreign_keys=ON"):
            db.execute(sql)
        return db

    def provision(self, db, number=1):
        db.execute(f"PRAGMA application_id={ledger.APPLICATION_ID}")
        db.execute(f"PRAGMA user_version={ledger.SCHEMA_VERSION}")
        for name in ("registration", "spent", "registration_key"):
            db.execute(ledger.SCHEMA[name])
        db.execute("INSERT INTO registration VALUES(1,?,?,?,?)",
                   ("1" * 32, "2" * 32, number, number))
        for name, sql in ledger.SCHEMA_RECORDS:
            if "_no_" in name:
                db.execute(sql)
        signature = tuple(db.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY name").fetchall())
        self.assertEqual(signature, ledger.SCHEMA_RECORDS)

    def adapter_for(self, db, fence=None):
        return ledger.ServiceSpentLedgerCandidate(fence or self.fence,
            _connection=db, _clock_ms=lambda: self.now)

    def variant(self, **changes):
        return _canonical({**decode_wire_request(self.raw), **changes}, MAX_WIRE)

    def observe(self, raw=None, adapter=None, status="DATA_COMMIT_OBSERVED"):
        result = (adapter or self.adapter).observe_data_only(
            self.raw if raw is None else raw)
        self.assertEqual(result.status, status)
        self.assertEqual(result.authority, "NONE_DATA_ONLY")
        self.assertEqual(result.admission, "UNAVAILABLE")
        self.assertEqual(result.acceptance, "NOT_OS_ACCEPTED")
        return result

    def count(self, db=None):
        return (db or self.db).execute("SELECT count(*) FROM spent").fetchone()[0]

    def params(self, raw=None):
        raw = self.raw if raw is None else raw
        req = decode_wire_request(raw)
        return tuple(req[key] for key in ("installation_id", "generation",
            "request_id", "operation_id", "challenge")) + (sha256(raw).hexdigest(), raw)

    def assert_latched(self, adapter):
        trace = []
        adapter._db.set_trace_callback(trace.append)
        self.observe(adapter=adapter, status="OUTCOME_UNKNOWN")
        self.assertEqual(trace, [])
        adapter._db.set_trace_callback(None)

    def test_disabled_without_connection(self):
        adapter = ledger.ServiceSpentLedgerCandidate(self.fence,
            _clock_ms=lambda: self.now)
        self.observe(adapter=adapter, status="NOT_ENABLED")
        self.assertEqual(self.count(), 0)

    def test_real_begin_commit_exact_binding_and_independent_visibility(self):
        reader = self.connect()
        trace, visibility = [], []

        def capture(sql):
            trace.append(sql)
            if sql.startswith("INSERT INTO spent") or sql == "COMMIT":
                visibility.append((sql.split()[0], self.count(reader)))

        self.db.set_trace_callback(capture)
        result = self.observe()
        self.db.set_trace_callback(None)
        self.assertEqual(visibility, [("INSERT", 0), ("COMMIT", 0)])
        self.assertEqual(trace[1], "BEGIN IMMEDIATE")
        self.assertEqual(trace[-1], "COMMIT")
        # The service candidate must validate schema and generation within
        # the reservation transaction, before any spent-record insertion.
        prefixes = ("BEGIN IMMEDIATE", "SELECT name,sql FROM sqlite_master",
                    "SELECT installation_id,generation,generation_number,high_water",
                    "SELECT request_sha256,request_bytes FROM spent",
                    "INSERT INTO spent", "COMMIT")
        positions = [next(i for i, sql in enumerate(trace)
                          if sql.startswith(prefix)) for prefix in prefixes]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.count(reader), 1)
        self.assertEqual(reader.execute("SELECT * FROM spent").fetchone(), self.params())
        self.assertEqual(result.request_bytes, self.raw)
        self.assertEqual(result.request_sha256, sha256(self.raw).hexdigest())
        self.assertEqual(result.generation_number, 1)

    def test_same_adapter_terminal_fresh_connection_spent(self):
        self.observe()
        trace = []
        self.db.set_trace_callback(trace.append)
        self.observe(status="DATA_TERMINAL")
        self.assertEqual(trace, [])
        self.db.set_trace_callback(None)
        other = self.connect()
        self.observe(adapter=self.adapter_for(other), status="DATA_SPENT")
        self.assertEqual(self.count(other), 1)

    def test_operation_and_challenge_reuse_conflict_in_database(self):
        self.observe()
        for changes in (
            dict(request_id="a" * 32, challenge="a" * 64),
            dict(request_id="b" * 32, operation_id="b" * 32),
        ):
            with self.subTest(changes=changes):
                raw = self.variant(**changes)
                other = self.connect()
                trace = []
                other.set_trace_callback(trace.append)
                self.observe(raw, self.adapter_for(other), "DATA_CONFLICT")
                self.assertIn("BEGIN IMMEDIATE", trace)
                self.assertTrue(any("(operation_id=" in sql for sql in trace))
                self.assertFalse(any(sql.startswith("INSERT") for sql in trace))
                self.assertEqual(self.count(other), 1)
                with self.assertRaises(sqlite3.IntegrityError):
                    other.execute(ledger.INSERT_SQL, self.params(raw))

    def test_same_request_changed_bytes_conflict(self):
        self.observe()
        raw = self.variant(snapshot_sha256="a" * 64)
        self.observe(raw, self.adapter_for(self.connect()), "DATA_CONFLICT")
        self.assertEqual(self.db.execute(ledger.LOOKUP_SQL,
            self.params()[:3]).fetchone(), (sha256(self.raw).hexdigest(), self.raw))
        self.assertEqual(self.count(), 1)

    def test_query_absent_then_spent_is_recovery_only(self):
        query = self.variant(action="QUERY")
        self.observe(query, status="DATA_ABSENT")
        self.assertEqual(self.count(), 0)
        self.observe(status="DATA_TERMINAL")
        self.observe(adapter=self.adapter_for(self.connect()))
        self.observe(query, status="DATA_SPENT")
        self.observe(status="DATA_TERMINAL")
        self.assertEqual(self.count(), 1)

    def test_query_after_commit_never_upgrades_to_consume(self):
        self.observe()
        recovery = self.adapter_for(self.connect())
        trace = []
        recovery._db.set_trace_callback(trace.append)
        self.observe(self.variant(action="QUERY"), recovery, "DATA_SPENT")
        self.observe(adapter=recovery, status="DATA_TERMINAL")
        self.assertFalse(any(sql.startswith("INSERT") for sql in trace))
        self.assertEqual(self.count(), 1)

    def test_stale_and_future_highwater_fences_latch_without_insert(self):
        for anchor_number, fence_number in ((2, 1), (1, 2)):
            with self.subTest(anchor=anchor_number, fence=fence_number):
                db = self.connect(Path(self.temp.name) / f"anchor-{anchor_number}.sqlite")
                self.provision(db, anchor_number)
                fence = ledger.GenerationFenceCandidate("1" * 32, "2" * 32,
                                                        fence_number)
                adapter = self.adapter_for(db, fence)
                self.observe(adapter=adapter, status="OUTCOME_UNKNOWN")
                self.assertEqual(self.count(db), 0)
                self.assertFalse(db.in_transaction)
                self.assert_latched(adapter)

    def test_immutable_triggers_reject_update_delete_on_both_tables(self):
        self.observe()
        for sql in ("UPDATE spent SET request_sha256='" + "a" * 64 + "'",
                    "DELETE FROM spent", "UPDATE registration SET high_water=1",
                    "DELETE FROM registration"):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable ledger"):
                    self.db.execute(sql)
                self.assertEqual(self.count(), 1)
                self.assertEqual(self.db.execute(ledger.ANCHOR_SQL).fetchone(),
                                 ("1" * 32, "2" * 32, 1, 1))

    def test_real_sql_constraints_reject_invalid_rows(self):
        self.observe()
        base = list(self.params(self.variant(request_id="a" * 32,
            operation_id="b" * 32, challenge="c" * 64)))
        cases = [(0, None), (1, "a" * 32), (2, "short"), (2, "4" * 32),
                 (3, "8" * 32), (4, "5" * 64), (5, "short"),
                 (6, b""), (6, b"x" * 8193), (6, "text not blob")]
        for index, value in cases:
            with self.subTest(index=index, value_type=type(value).__name__):
                params = base.copy()
                params[index] = value
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(ledger.INSERT_SQL, params)
                self.assertEqual(self.count(), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO registration VALUES(2,?,?,1,2)",
                            ("a" * 32, "b" * 32))

    def test_wrong_pragmas_latch_without_insert(self):
        for index, sql in enumerate(("PRAGMA application_id=0",
                "PRAGMA user_version=2", "PRAGMA synchronous=NORMAL",
                "PRAGMA busy_timeout=1", "PRAGMA foreign_keys=OFF",
                "PRAGMA journal_mode=MEMORY")):
            with self.subTest(sql=sql):
                db = self.connect(Path(self.temp.name) / f"policy-{index}.sqlite")
                self.provision(db)
                db.execute(sql)
                adapter = self.adapter_for(db)
                self.observe(adapter=adapter, status="OUTCOME_UNKNOWN")
                self.assertEqual(self.count(db), 0)
                self.assert_latched(adapter)

    def test_extra_schema_rejected_without_insert(self):
        self.db.execute("CREATE TABLE unexpected(value TEXT)")
        self.observe(status="OUTCOME_UNKNOWN")
        self.assertEqual(self.count(), 0)
        self.assert_latched(self.adapter)

    def test_real_write_lock_fails_begin_and_never_retries(self):
        locker = self.connect()
        locker.execute("BEGIN IMMEDIATE")
        self.addCleanup(locker.rollback)
        trace = []
        self.db.set_trace_callback(trace.append)
        self.observe(status="OUTCOME_UNKNOWN")
        self.assertEqual(trace.count("BEGIN IMMEDIATE"), 1)
        self.assertFalse(any(sql.startswith("INSERT") for sql in trace))
        self.assertFalse(self.db.in_transaction)
        locker.rollback()
        self.assert_latched(self.adapter)
        self.assertEqual(self.count(), 0)
        self.observe(adapter=self.adapter_for(self.connect()))

    def test_real_reader_lock_blocks_commit_and_never_retries(self):
        # Rollback-journal reader permits BEGIN IMMEDIATE and INSERT but
        # denies writer COMMIT while it retains its SHARED read lock.
        reader = self.connect()
        reader.execute("BEGIN")
        self.addCleanup(reader.rollback)
        self.assertEqual(reader.execute("SELECT count(*) FROM spent").fetchone(), (0,))
        trace = []
        self.db.set_trace_callback(trace.append)
        self.observe(status="OUTCOME_UNKNOWN")
        self.db.set_trace_callback(None)
        self.assertIn("BEGIN IMMEDIATE", trace)
        self.assertTrue(any(sql.startswith("INSERT INTO spent") for sql in trace))
        self.assertIn("COMMIT", trace)
        self.assertIn("ROLLBACK", trace)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(reader.execute("SELECT count(*) FROM spent").fetchone(), (0,))
        reader.rollback()
        self.assertEqual(self.count(), 0)
        self.assert_latched(self.adapter)
        # Recovery can observe ABSENT only. No automatic replay permission.
        query = self.variant(action="QUERY")
        self.observe(query, self.adapter_for(self.connect()), "DATA_ABSENT")

    def test_corrupt_file_rejected_without_error_or_path_disclosure(self):
        path = Path(self.temp.name) / "corrupt.sqlite"
        marker = "SYNTHETIC_SECRET_DO_NOT_DISCLOSE"
        path.write_bytes((marker * 32).encode("ascii"))
        # Do not run provisioning PRAGMAs against the deliberately corrupt file.
        db = sqlite3.connect(str(path), isolation_level=None)
        self.addCleanup(db.close)
        db.execute("PRAGMA busy_timeout=0")
        adapter = self.adapter_for(db)
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = self.observe(adapter=adapter, status="OUTCOME_UNKNOWN")
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(marker, repr(result))
        self.assertNotIn(str(path), repr(result))
        self.assertFalse(hasattr(result, "error"))
        self.assertFalse(db.in_transaction)
        self.assert_latched(adapter)

    def test_user_can_remove_triggers_and_erase_spent_data_no_protection(self):
        self.observe()
        user = self.connect()
        user.execute("DROP TRIGGER spent_no_delete")
        user.execute("DELETE FROM spent")
        self.assertEqual(self.count(), 0)
        user.execute(ledger.SCHEMA["spent_no_delete"])
        self.assertEqual(tuple(user.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY name")), ledger.SCHEMA_RECORDS)
        # Restoring matching schema hides this user tamper from a fresh adapter.
        # DATA_COMMIT_OBSERVED here explicitly does not prove antirollback.
        self.observe(adapter=self.adapter_for(self.connect()))
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
