"""Disposable Windows data-only tests. No command/approval/Job/GUI launch.

Only helper Python processes doing SQLite claims are started. No ACL/file
permission changes; no production database or protected directory is used.
"""

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.broker_launch_fence import BrokerLaunchFence, LaunchFenceError
from sora_commander_next.commands import Command


_CHILD = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from sora_commander_next.commands import Command
from sora_commander_next.broker_launch_fence import BrokerLaunchFence
command = Command(**json.loads(sys.argv[3]))
sys.stdin.buffer.read(1)
fence = BrokerLaunchFence(sys.argv[2])
try:
    print(fence.reserve_attempt_data_only(command).status, flush=True)
finally:
    fence.close()
"""


@unittest.skipUnless(sys.platform == "win32", "requires independent Windows SQLite/path semantics")
class BrokerLaunchFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="next-launch-fence-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / "fence.sqlite"
        self.command = Command(str(self.home / "NEVER_EXECUTED.exe"),
                               ("fake", "雪", "a b"), str(self.home),
                               request_id="0123456789abcdef" * 2)

    _DEFAULT_PATH = object()

    def open(self, path=_DEFAULT_PATH):
        # Explicit None is invalid input; do not silently use the good path.
        target = self.path if path is self._DEFAULT_PATH else path
        # Test fixture provisioning only. Missing-store denial tests below call
        # the normal constructor directly, without this convenience helper.
        if isinstance(target, Path) and target.parent == self.home and not target.exists():
            fence = BrokerLaunchFence.provision_new_data_only(target)
        else:
            fence = BrokerLaunchFence(target)
        self.addCleanup(fence.close)
        return fence

    def status(self, fence, expected, command=None, method="reserve_attempt_data_only"):
        outcome = getattr(fence, method)(self.command if command is None else command)
        self.assertEqual(outcome.status, expected)
        self.assertEqual(outcome.__slots__, ("status",))
        with self.assertRaises(FrozenInstanceError):
            outcome.status = "permission"

    def sql(self, sql, parameters=()):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            result = db.execute(sql, parameters).fetchall()
            db.commit()
            return result
        finally:
            db.close()

    def child(self, command=None):
        c = self.command if command is None else command
        payload = json.dumps({k: getattr(c, k) for k in
                              ("executable", "argv", "cwd", "timeout", "output_limit", "request_id")})
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", _CHILD,
             str(ROOT / "src"), str(self.path), payload],
            cwd=self.home, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.addCleanup(self.reap, proc)
        return proc

    @staticmethod
    def reap(proc):
        if proc.poll() is None:
            proc.terminate()  # Only our bounded, data-only test helper.
        proc.communicate(timeout=5)

    def child_result(self, proc, gate="x"):
        stdout, stderr = proc.communicate(gate, timeout=8)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stderr, "")
        return stdout.strip()

    def test_inspect_and_terminal_absence_never_insert(self):
        fence = self.open()
        for method in ("inspect_data_only", "mark_terminal_data_only"):
            self.status(fence, "NO_ATTEMPT_RECORDED_DATA_ONLY", method=method)
        self.assertEqual(self.sql("SELECT count(*) FROM launch_fence"), [(0,)])

    def test_durable_reopen_and_second_process_deny_replay(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        fence.close()
        reopened = self.open()
        self.status(reopened, "ALREADY_ATTEMPTED_UNKNOWN")
        self.status(reopened, "ALREADY_ATTEMPTED_UNKNOWN", method="inspect_data_only")
        reopened.close()
        self.assertEqual(self.child_result(self.child()), "ALREADY_ATTEMPTED_UNKNOWN")
        self.assertEqual(self.child_result(self.child(replace(self.command, argv=("different",)))),
                         "DENY_DUPLICATE_CONFLICT")
        self.assertEqual(self.sql("SELECT status FROM launch_fence"), [("UNKNOWN",)])

    def test_two_real_processes_race_on_provisioned_database(self):
        self.open().close()
        first, second = self.child(), self.child()
        # Release both gates before waiting for either process's result.
        for proc in (first, second):
            proc.stdin.write("x")
            proc.stdin.flush()
        statuses = [self.child_result(proc, gate=None) for proc in (first, second)]
        self.assertCountEqual(statuses, ["ATTEMPT_RECORDED_DATA_ONLY", "ALREADY_ATTEMPTED_UNKNOWN"])
        self.assertEqual(self.sql("SELECT count(*),min(status) FROM launch_fence"), [(1, "UNKNOWN")])

    def test_two_connections_keep_distinct_ids_and_deny_existing(self):
        first, second = self.open(), self.open()
        self.status(first, "ATTEMPT_RECORDED_DATA_ONLY")
        self.status(second, "ALREADY_ATTEMPTED_UNKNOWN")
        other = replace(self.command, request_id="f" * 32)
        self.status(second, "ATTEMPT_RECORDED_DATA_ONLY", other)
        self.status(first, "ALREADY_ATTEMPTED_UNKNOWN", other)

    def test_terminal_never_reauthorizes_even_after_reopen_and_process(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        wrong = replace(self.command, argv=("wrong",))
        self.status(fence, "DENY_DUPLICATE_CONFLICT", wrong, "mark_terminal_data_only")
        self.assertEqual(self.sql("SELECT status FROM launch_fence"), [("UNKNOWN",)])
        self.status(fence, "TERMINAL_RECORDED_DATA_ONLY", method="mark_terminal_data_only")
        self.status(fence, "ALREADY_ATTEMPTED_TERMINAL", method="mark_terminal_data_only")
        fence.close()
        reopened = self.open()
        self.status(reopened, "ALREADY_ATTEMPTED_TERMINAL")
        self.status(reopened, "ALREADY_ATTEMPTED_TERMINAL", method="inspect_data_only")
        reopened.close()
        self.assertEqual(self.child_result(self.child()), "ALREADY_ATTEMPTED_TERMINAL")

    def test_independent_fingerprint_and_database_metadata(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        c = self.command
        payload = {"executable": c.executable, "argv": list(c.argv), "cwd": c.cwd,
                   "timeout": ["float", "0x1.4000000000000p+2"],
                   "output_limit": 8192, "request_id": c.request_id}
        expected = hashlib.sha256(
            b"sora-commander-next/broker-launch-fence/command/v1\x00" +
            json.dumps(payload, ensure_ascii=True, allow_nan=False,
                       sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
        self.assertEqual(self.sql("SELECT request_id,fingerprint,status FROM launch_fence"),
                         [(c.request_id, expected, "UNKNOWN")])
        self.assertEqual(self.sql("PRAGMA application_id"), [(0x53434E46,)])
        self.assertEqual(self.sql("PRAGMA user_version"), [(1,)])
        self.assertEqual(fence._db.execute("PRAGMA synchronous").fetchall(), [(2,)])
        self.assertEqual(fence._db.execute("PRAGMA foreign_keys").fetchall(), [(1,)])
        self.assertEqual(self.sql("PRAGMA journal_mode"), [("delete",)])

    def test_every_command_field_changes_exact_fingerprint(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        for change in ({"executable": str(self.home / "other.exe")}, {"argv": ("fake 雪", "a b")},
                       {"cwd": str(self.home / "other")}, {"timeout": 6.0},
                       {"timeout": 5}, {"output_limit": 4096}):
            with self.subTest(change=change):
                self.status(fence, "DENY_DUPLICATE_CONFLICT", replace(self.command, **change))
        other = replace(self.command, request_id="a" * 32)
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY", other)
        fingerprints = self.sql("SELECT fingerprint FROM launch_fence")
        self.assertEqual(len(set(fingerprints)), 2)

    def test_invalid_ids_rejected_without_insert(self):
        fence = self.open()
        for value in ("A" * 32, "g" * 32, "a" * 31, "a" * 33, "a" * 31 + "\n",
                      "a" * 31 + "\0", True, 1, b"a" * 32, None):
            forged = replace(self.command)
            object.__setattr__(forged, "request_id", value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                fence.reserve_attempt_data_only(forged)
        self.assertEqual(self.sql("SELECT count(*) FROM launch_fence"), [(0,)])

    def test_bool_nonfinite_and_malformed_forged_commands_refused(self):
        fence = self.open()
        cases = [("timeout", v) for v in (True, False, float("nan"), float("inf"),
                                          -float("inf"), 0, 3601, "5", 10 ** 1000)]
        cases += [("output_limit", v) for v in (True, 8192.0, 127, 65537)]
        cases += [("argv", v) for v in (["mutable"], (True,), ("\0",), ("a" * 8193,))]
        cases += [("executable", True), ("cwd", "relative"), ("cwd", "\0")]
        for field, value in cases:
            forged = replace(self.command)
            object.__setattr__(forged, field, value)
            with self.subTest(field=field, value=str(value)[:40]), self.assertRaises(ValueError):
                fence.reserve_attempt_data_only(forged)
        for value in (None, {}, True, "command"):
            with self.assertRaises(ValueError):
                fence.reserve_attempt_data_only(value)
        self.assertEqual(self.sql("SELECT count(*) FROM launch_fence"), [(0,)])

    def test_sql_constraints_reject_invalid_rows(self):
        self.open().close()
        for values in (("A" * 32, "a" * 64, "UNKNOWN"), ("a" * 32, "g" * 64, "UNKNOWN"),
                       ("a" * 32, "a" * 64, "GRANTED"), ("a" * 32, "a" * 63, "UNKNOWN"),
                       (None, "a" * 64, "UNKNOWN"),
                       ("a" * 32 + "\0hidden", "a" * 64, "UNKNOWN"),
                       ("a" * 32, "a" * 64 + "\0hidden", "UNKNOWN")):
            with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                self.sql("INSERT INTO launch_fence VALUES (?,?,?)", values)

    def test_corrupt_file_and_mutated_schema_or_metadata_refused(self):
        mutations = ["CREATE TABLE extra (value TEXT)",
                     "ALTER TABLE launch_fence ADD COLUMN extra TEXT",
                     "CREATE INDEX extra ON launch_fence(status)",
                     "CREATE VIEW extra AS SELECT * FROM launch_fence",
                     "CREATE TRIGGER extra AFTER INSERT ON launch_fence BEGIN SELECT 1; END",
                     "PRAGMA user_version=2", "PRAGMA application_id=0",
                     "DROP TABLE launch_fence"]
        for i, mutation in enumerate(mutations):
            path = self.home / f"mutant-{i}.sqlite"
            self.open(path).close()
            db = sqlite3.connect(path)
            try:
                db.execute(mutation)
                db.commit()
            finally:
                db.close()
            with self.subTest(mutation=mutation), self.assertRaises(LaunchFenceError):
                self.open(path)
        corrupt = self.home / "corrupt.sqlite"
        corrupt.write_bytes(b"not a SQLite database" * 100)
        with self.assertRaises(LaunchFenceError):
            self.open(corrupt)

    def test_mutation_after_open_is_denial_and_prior_claim_retained(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        self.sql("CREATE TABLE surprise (x TEXT)")
        self.status(fence, "DENY_STORAGE_ERROR")
        self.status(fence, "DENY_STORAGE_ERROR", replace(self.command, request_id="f" * 32))
        self.assertEqual(self.sql("SELECT status FROM launch_fence"), [("UNKNOWN",)])

    def test_integrity_check_detects_row_constraint_corruption(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        fence.close()
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE launch_fence SET status='GRANTED'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(LaunchFenceError):
            self.open()

    def test_bounded_lock_timeout_fails_closed_and_keeps_prior_unknown(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        blocker = sqlite3.connect(self.path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            self.status(fence, "DENY_STORAGE_ERROR", replace(self.command, request_id="f" * 32))
            self.assertLess(time.monotonic() - started, 4)
        finally:
            blocker.close()
        self.status(self.open(), "ALREADY_ATTEMPTED_UNKNOWN")
        self.assertEqual(self.sql("SELECT count(*) FROM launch_fence"), [(1,)])

    def inject_failure(self, fence, statement, *, after=False):
        real = fence._db

        class Fault:
            def execute(self, sql, *args):
                if sql.startswith(statement):
                    if after:
                        real.execute(sql, *args)
                    raise sqlite3.OperationalError("simulated storage/fsync failure")
                return real.execute(sql, *args)

            def close(self):
                real.close()

        fence._db = Fault()

    def test_ambiguous_commit_failure_denies_but_preserves_durable_unknown(self):
        fence = self.open()
        self.inject_failure(fence, "COMMIT", after=True)
        self.status(fence, "DENY_STORAGE_ERROR")
        self.status(self.open(), "ALREADY_ATTEMPTED_UNKNOWN")

    def test_write_failure_never_returns_fresh_and_keeps_prior_claim(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        self.inject_failure(fence, "INSERT")
        self.status(fence, "DENY_STORAGE_ERROR", replace(self.command, request_id="f" * 32))
        self.status(self.open(), "ALREADY_ATTEMPTED_UNKNOWN")

    def test_terminal_write_failure_leaves_prior_unknown(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        self.inject_failure(fence, "UPDATE")
        self.status(fence, "DENY_STORAGE_ERROR", method="mark_terminal_data_only")
        self.status(self.open(), "ALREADY_ATTEMPTED_UNKNOWN")

    def test_unsafe_paths_refused(self):
        raw = str(self.path)
        paths = ["relative.sqlite", "C:relative.sqlite", r"\\server\share\fence.sqlite",
                 r"\\?\C:\fence.sqlite", r"\\.\C:\fence.sqlite", raw + ":ads",
                 str(self.home / "missing" / "fence.sqlite"), str(self.home),
                 str(self.home / "NUL.sqlite"), str(self.home / "trailing. "),
                 str(self.home) + "\\..\\fence.sqlite", raw + "\0", True, None,
                 raw.replace("\\", "/"), str(self.home) + "\\\\fence.sqlite"]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(LaunchFenceError):
                self.open(path)
        self.assertFalse(self.path.exists())

    def test_network_or_removable_drive_type_refused(self):
        with patch("sora_commander_next.broker_launch_fence._fixed_drive", return_value=False):
            with self.assertRaises(LaunchFenceError):
                self.open()
        self.assertFalse(self.path.exists())

    def test_real_hardlinks_and_sidecars_refused(self):
        self.open().close()
        alias = self.home / "hardlink.sqlite"
        os.link(self.path, alias)
        with self.assertRaises(LaunchFenceError):
            self.open()
        alias.unlink()
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            os.link(self.path, sidecar)
            with self.subTest(suffix=suffix), self.assertRaises(LaunchFenceError):
                self.open()
            sidecar.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            sidecar.write_bytes(b"")
            with self.assertRaises(LaunchFenceError):
                self.open()
            sidecar.unlink()

    def test_each_ancestor_and_database_reparse_attribute_refused(self):
        # Attribute fault injection supplements the independent real Windows
        # hardlink tests; this does not certify production path/ACL protection.
        import stat
        from types import SimpleNamespace
        real = Path.lstat
        for target in (*self.path.parents, self.path):
            self.open().close()

            def fake(path, *, _target=target):
                info = real(path)
                if path == _target:
                    return SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                                           st_mode=info.st_mode, st_nlink=info.st_nlink)
                return info

            with self.subTest(target=target), patch.object(Path, "lstat", fake):
                with self.assertRaises(LaunchFenceError):
                    self.open()

    def test_existing_wal_database_refused_without_conversion(self):
        self.open().close()
        self.assertEqual(self.sql("PRAGMA journal_mode=WAL"), [("wal",)])
        with self.assertRaises(LaunchFenceError):
            self.open()
        self.assertEqual(self.sql("PRAGMA journal_mode"), [("wal",)])

    def test_closed_instance_is_deny_only_and_close_is_idempotent(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        fence.close()
        fence.close()
        self.status(fence, "DENY_STORAGE_ERROR")
        self.status(self.open(), "ALREADY_ATTEMPTED_UNKNOWN")

    def test_normal_open_missing_or_empty_never_connects_or_initializes(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    self.path.write_bytes(b"")
                with patch("sora_commander_next.broker_launch_fence.sqlite3.connect") as connect:
                    with self.assertRaises(LaunchFenceError):
                        BrokerLaunchFence(self.path)
                    connect.assert_not_called()
                self.assertEqual(self.path.exists(), empty)
                if empty:
                    self.assertEqual(self.path.read_bytes(), b"")

    def test_provision_refuses_existing_empty_corrupt_and_spent_store(self):
        for contents in (b"", b"corrupt store"):
            path = self.home / ("empty.sqlite" if not contents else "corrupt.sqlite")
            path.write_bytes(contents)
            with self.subTest(contents=contents), self.assertRaises(LaunchFenceError):
                BrokerLaunchFence.provision_new_data_only(path)
            self.assertEqual(path.read_bytes(), contents)
        fence = BrokerLaunchFence.provision_new_data_only(self.path)
        self.addCleanup(fence.close)
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        fence.close()
        before = self.path.read_bytes()
        with self.assertRaises(LaunchFenceError):
            BrokerLaunchFence.provision_new_data_only(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.status(self.open(), "ALREADY_ATTEMPTED_UNKNOWN")

    def test_provision_orphan_journal_refused_without_creating_database(self):
        journal = Path(str(self.path) + "-journal")
        journal.write_bytes(b"preserve for diagnosis")
        with self.assertRaises(LaunchFenceError):
            BrokerLaunchFence.provision_new_data_only(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(journal.read_bytes(), b"preserve for diagnosis")

    def test_provision_race_never_opens_or_overwrites_winner(self):
        real_open = os.open
        def race(path, flags, mode):
            self.path.write_bytes(b"another initializer owns this")
            return real_open(path, flags, mode)
        with patch("sora_commander_next.broker_launch_fence.os.open", side_effect=race), \
             patch("sora_commander_next.broker_launch_fence.sqlite3.connect") as connect:
            with self.assertRaises(LaunchFenceError):
                BrokerLaunchFence.provision_new_data_only(self.path)
            connect.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"another initializer owns this")

    def test_failed_provision_does_not_repair_or_delete_its_artifact(self):
        with patch("sora_commander_next.broker_launch_fence.sqlite3.connect",
                   side_effect=sqlite3.OperationalError("injected initialization failure")):
            with self.assertRaises(LaunchFenceError):
                BrokerLaunchFence.provision_new_data_only(self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_bytes(), b"")
        for constructor in (BrokerLaunchFence, BrokerLaunchFence.provision_new_data_only):
            with self.subTest(constructor=constructor), self.assertRaises(LaunchFenceError):
                constructor(self.path)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_normal_open_database_with_empty_schema_never_initializes(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA user_version=7")
        db.execute("PRAGMA user_version=0")
        db.close()
        self.assertGreater(self.path.stat().st_size, 0)
        before = self.path.read_bytes()
        with self.assertRaises(LaunchFenceError):
            BrokerLaunchFence(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_deleted_store_cannot_be_reopened_as_fresh(self):
        fence = self.open()
        self.status(fence, "ATTEMPT_RECORDED_DATA_ONLY")
        fence.close()
        self.path.unlink()  # Only this test's disposable database.
        with self.assertRaises(LaunchFenceError):
            BrokerLaunchFence(self.path)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
