"""Temp SQLite DATA_ONLY checks. No live Owner/Broker/G2 acceptance: G2 HOLD."""
import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import offline_operation_intent_store as model
from offline_operation_intent_store import (
    IntentStoreHeld, OfflineOperationIntentStore, provision_new_data_only,
)


OP, OTHER, RID, RID2 = ('a' * 32, 'b' * 32, 'c' * 32, 'd' * 32)
BIND, BIND2 = ('e' * 64, 'f' * 64)


class StoreTests(unittest.TestCase):
    def setUp(self):
        # Ordinary unprivileged TEMP only; never redirect TEMP or change ACLs.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'disposable.sqlite'
        provision_new_data_only(self.path)
        self.store = self.open()

    def open(self):
        store = OfflineOperationIntentStore(self.path)
        self.addCleanup(store.close)
        return store

    def new(self, op=OP):
        return self.store.record_new_data_only(op, BIND)

    def reserve(self, rid=RID):
        return self.store.reserve_first_attempt_data_only(OP, BIND, rid)

    def test_new_and_duplicate_never_reset(self):
        self.assertEqual(self.new()['status'], 'NEW_INERT_DATA_ONLY')
        self.assertIsNone(self.store.inspect_data_only(OP)['request_id'])
        self.reserve()
        result = self.new()
        self.assertEqual(result['status'], 'ALREADY_DATA_ONLY')
        self.assertEqual(result['request_id'], RID)
        self.assertEqual(result['state'], 'RESERVED_UNKNOWN')

    def test_reservation_committed_before_hypothetical_spawn(self):
        self.new()
        result = self.reserve()
        self.assertEqual(result['status'], 'RESERVATION_RECORDED_DATA_ONLY')
        # Hypothetical spawn boundary: independent connection observes commit.
        observed = self.open().inspect_data_only(OP)
        self.assertEqual(observed['request_id'], RID)
        self.assertEqual(observed['state'], 'RESERVED_UNKNOWN')
        self.assertTrue(result['data_only'])
        self.assertNotIn(result['status'], ('ALLOW', 'RUNNING'))

    def test_restart_same_and_different_request_hold(self):
        self.new()
        self.reserve()
        self.store.close()
        reopened = self.open()
        for rid in (RID, RID2):
            result = reopened.reserve_first_attempt_data_only(OP, BIND, rid)
            self.assertTrue(result['status'].startswith('HOLD_'))
            self.assertEqual(result['request_id'], RID)

    def test_binding_conflict_denied_without_change(self):
        self.new()
        before = self.store.inspect_data_only(OP)
        for result in (self.store.record_new_data_only(OP, BIND2),
                       self.store.reserve_first_attempt_data_only(OP, BIND2, RID)):
            self.assertEqual(result['status'], 'HOLD_BINDING_CONFLICT_DATA_ONLY')
        self.assertEqual(self.store.inspect_data_only(OP), before)

    def test_distinct_operation_same_binding_is_inert_unmerged_data(self):
        self.new()
        self.reserve()
        self.assertEqual(self.new(OTHER)['status'], 'NEW_INERT_DATA_ONLY')
        self.assertIsNone(self.store.inspect_data_only(OTHER)['request_id'])
        self.assertEqual(self.store.inspect_data_only(OP)['request_id'], RID)

    def test_two_thread_owned_connections_exactly_one_reservation(self):
        self.new()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def worker(rid):
            try:
                with OfflineOperationIntentStore(self.path) as store:
                    barrier.wait(timeout=5)
                    try:
                        results.append(store.reserve_first_attempt_data_only(OP, BIND, rid))
                    except IntentStoreHeld:
                        results.append({'status': 'HOLD_TRANSACTION_DATA_ONLY'})
            except Exception as exc:
                errors.append(type(exc).__name__)
                barrier.abort()
        threads = [threading.Thread(target=worker, args=(rid,)) for rid in (RID, RID2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(r['status'] == 'RESERVATION_RECORDED_DATA_ONLY'
                             for r in results), 1)
        self.assertEqual(sum(r['status'].startswith('HOLD_') for r in results), 1)
        self.assertIn(self.open().inspect_data_only(OP)['request_id'], (RID, RID2))

    def test_request_unique_across_operations(self):
        self.new()
        self.new(OTHER)
        self.reserve()
        result = self.store.reserve_first_attempt_data_only(OTHER, BIND, RID)
        self.assertEqual(result['status'], 'HOLD_REQUEST_USED_DATA_ONLY')
        self.assertIsNone(result['request_id'])
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE operations SET state='RESERVED_UNKNOWN', "
                             'request_id=? WHERE operation_id=?', (RID, OTHER))

    def test_unknown_persists_and_is_idempotent(self):
        self.new()
        self.reserve()
        first = self.store.mark_unknown_data_only(OP, RID, BIND)
        self.assertEqual(self.store.mark_unknown_data_only(OP, RID, BIND), first)
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.inspect_data_only(OP), first)
        self.assertEqual(self.reserve(RID2)['request_id'], RID)

    def test_terminal_claim_persists_without_reuse(self):
        self.new()
        self.reserve()
        self.store.mark_unknown_data_only(OP, RID, BIND)
        terminal = self.store.mark_terminal_data_only(OP, RID, BIND)
        self.assertEqual(terminal['state'], 'CONFIRMED_TERMINAL')
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.inspect_data_only(OP), terminal)
        for rid in (RID, RID2):
            self.assertEqual(self.reserve(rid), terminal)
        self.assertEqual(self.store.mark_unknown_data_only(OP, RID, BIND), terminal)

    def test_mark_wrong_identity_or_new_cannot_transition(self):
        self.new()
        self.assertTrue(self.store.mark_terminal_data_only(OP, RID, BIND)
                        ['status'].startswith('HOLD_'))
        self.reserve()
        for rid, binding in ((RID2, BIND), (RID, BIND2)):
            result = self.store.mark_unknown_data_only(OP, rid, binding)
            self.assertEqual(result['status'], 'HOLD_IDENTITY_CONFLICT_DATA_ONLY')
            self.assertEqual(result['request_id'], RID)

    def test_corrupt_schema_rejected_without_repair(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('ALTER TABLE operations ADD COLUMN hostile TEXT')
            conn.commit()
        before = self.path.read_bytes()
        with self.assertRaises(IntentStoreHeld):
            OfflineOperationIntentStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_database_rejected_without_replacement(self):
        self.store.close()
        self.path.write_bytes(b'not a SQLite database')
        with self.assertRaises(IntentStoreHeld):
            OfflineOperationIntentStore(self.path)
        self.assertEqual(self.path.read_bytes(), b'not a SQLite database')
        with self.assertRaises(IntentStoreHeld):
            provision_new_data_only(self.path)
        self.assertEqual(self.path.read_bytes(), b'not a SQLite database')

    def test_missing_database_and_parent_not_created(self):
        missing = self.path.parent / 'missing.sqlite'
        with self.assertRaises(IntentStoreHeld):
            OfflineOperationIntentStore(missing)
        self.assertFalse(missing.exists())
        missing_parent = self.path.parent / 'absent' / 'db.sqlite'
        with self.assertRaises(IntentStoreHeld):
            provision_new_data_only(missing_parent)
        self.assertFalse(missing_parent.parent.exists())

    def test_hostile_snapshot_rollback_unprevented_g2_hold(self):
        self.new()
        self.store.close()
        snapshot = self.path.read_bytes()
        self.store = self.open()
        self.reserve()
        self.store.close()
        # Hostile same-user rollback is possible; no protected authority claimed.
        self.path.write_bytes(snapshot)
        self.store = self.open()
        self.assertEqual(self.reserve(RID2)['status'], 'RESERVATION_RECORDED_DATA_ONLY')
        self.assertIn('G2 HOLD', model.__doc__)

    def test_coherent_same_user_tamper_and_forged_binding_unprevented(self):
        self.new()
        self.reserve()
        self.store.close()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE operations SET state='NEW', request_id=NULL, "
                         'command_binding=?', (BIND2,))
            conn.commit()
        self.store = self.open()
        self.assertEqual(self.store.reserve_first_attempt_data_only(OP, BIND2, RID2)
                         ['status'], 'RESERVATION_RECORDED_DATA_ONLY')
        # Valid data cannot establish honest identity, intent or authorization.

    def test_invalid_inputs_rejected_before_transaction_without_echo(self):
        class Text(str):
            pass
        invalid = (None, True, 1, [], {}, b'a' * 32, '', 'A' * 32,
                   'g' * 32, 'a' * 31, 'a' * 33, Text(OP), '--secret=private')
        for value in invalid:
            for call in (lambda: self.store.record_new_data_only(value, BIND),
                         lambda: self.store.reserve_first_attempt_data_only(OP, BIND, value),
                         lambda: self.store.inspect_data_only(value),
                         lambda: self.store.mark_unknown_data_only(OP, value, BIND),
                         lambda: self.store.record_new_data_only(OP, value)):
                with self.assertRaises(IntentStoreHeld) as raised:
                    call()
                self.assertNotIn('--secret=private', str(raised.exception))
        # Validation failures never enter a transaction or poison the store.
        self.assertEqual(self.new()['state'], 'NEW')

    def test_relative_memory_network_and_existing_provision_denied(self):
        for path in (':memory:', 'relative.sqlite', '//server/share/db.sqlite'):
            with self.assertRaises(IntentStoreHeld):
                provision_new_data_only(path)
        before = self.path.read_bytes()
        with self.assertRaises(IntentStoreHeld):
            provision_new_data_only(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_transaction_errors_before_and_after_commit_never_grant_rights(self):
        self.new()
        class Fault:
            def __init__(self, real, *, after_commit=False,
                         rollback_fails=False, close_fails=False):
                self.real = real
                self.after_commit = after_commit
                self.rollback_fails = rollback_fails
                self.close_fails = close_fails
                self.closed = False
            def execute(self, sql, *args):
                if sql == 'COMMIT':
                    if self.after_commit:
                        self.real.execute(sql)
                    raise sqlite3.OperationalError('synthetic ambiguous commit')
                return self.real.execute(sql, *args)
            def rollback(self):
                if self.rollback_fails:
                    raise sqlite3.OperationalError('synthetic rollback failure')
                return self.real.rollback()
            def close(self):
                self.real.close()
                self.closed = True
                if self.close_fails:
                    raise sqlite3.OperationalError('synthetic close failure')

        first = Fault(self.store._conn)
        self.store._conn = first
        with self.assertRaisesRegex(IntentStoreHeld, '^HOLD_TRANSACTION_UNCERTAIN'):
            self.reserve()
        self.assertTrue(first.closed)
        self.assertIsNone(self.store._conn)
        with self.assertRaises(IntentStoreHeld):
            self.reserve(RID2)
        self.assertEqual(self.open().inspect_data_only(OP)['state'], 'NEW')

        # Reopen as a distinct local test store, never resurrect a poisoned one.
        self.store = self.open()
        committed = Fault(self.store._conn, after_commit=True)
        self.store._conn = committed
        with self.assertRaises(IntentStoreHeld):
            self.reserve()
        self.assertTrue(committed.closed)
        with self.assertRaises(IntentStoreHeld):
            self.reserve(RID2)
        self.assertEqual(self.open().inspect_data_only(OP)['request_id'], RID)

    def test_rollback_and_close_failure_still_poison_and_release(self):
        self.new()
        original = self.store._conn
        class Fault:
            def __init__(self):
                self.closed = False
            def execute(self, sql, *args):
                if sql == 'COMMIT':
                    raise sqlite3.OperationalError('private commit detail')
                return original.execute(sql, *args)
            def rollback(self):
                raise sqlite3.OperationalError('private rollback detail')
            def close(self):
                original.close()
                self.closed = True
                raise sqlite3.OperationalError('private close detail')
        broken = Fault()
        self.store._conn = broken
        with self.assertRaises(IntentStoreHeld) as raised:
            self.reserve()
        for secret in ('private commit detail', 'private rollback detail',
                       'private close detail'):
            self.assertNotIn(secret, str(raised.exception))
        self.assertTrue(broken.closed)
        self.assertIsNone(self.store._conn)
        with self.assertRaises(IntentStoreHeld):
            self.reserve(RID2)
        # A fresh reader must not remain stuck behind the old failed lock.
        self.assertEqual(self.open().inspect_data_only(OP)['state'], 'NEW')

    def test_actual_sqlite_write_lock_denies_and_poisoned_instance_stays_closed(self):
        self.new()
        with closing(sqlite3.connect(self.path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            with self.assertRaises(IntentStoreHeld):
                self.reserve()
            with self.assertRaises(IntentStoreHeld):
                self.reserve(RID2)
            self.assertIsNone(self.store._conn)
            blocker.execute('ROLLBACK')
        self.assertEqual(self.open().inspect_data_only(OP)['state'], 'NEW')

    def test_schema_contains_no_secrets_owner_or_production_import(self):
        with closing(sqlite3.connect(self.path)) as conn:
            columns = {r[1] for r in conn.execute('PRAGMA table_info(operations)')}
        self.assertEqual(columns, {'operation_id', 'command_binding', 'request_id',
                                   'state', 'source_version'})
        source = Path(model.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports.update(a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                       for a in n.names)
        self.assertEqual(imports, {'os', 'pathlib', 're', 'sqlite3', 'stat'})
        self.assertFalse(any('owner' in name.lower() for name in vars(self.store)))
        self.assertLessEqual(len(source.splitlines()), 350)
        self.assertLessEqual(len(Path(__file__).read_text(encoding='utf-8').splitlines()), 350)


if __name__ == '__main__':
    unittest.main()
