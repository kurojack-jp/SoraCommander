"""The durable launch record, with real SQLite and real harmless child processes.

Files live in the inherited test scratch folder, never in AppData. No Owner window.
"""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests._inherited_test_temp import inherited_test_temp
from sora_commander_next.commands import Command
from sora_commander_next.owner_broker_main import LEDGER_NAME, run_ledger_command
from sora_commander_next.owner_broker_service import _SCHEMAS
from sora_commander_next.owner_launch_ledger import (
    DurableJobManager, LaunchLedger, LaunchLedgerError)

ROOT = Path(__file__).resolve().parents[1]
R1, R2, R3 = "1" * 32, "2" * 32, "3" * 32


def command(request_id, code="print('ok')", timeout=30):
    return Command(sys.executable, ("-c", code), str(ROOT), timeout=timeout,
                   output_limit=1024, request_id=request_id)


@unittest.skipUnless(os.name == "nt", "Windows only")
class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(inherited_test_temp()))
        self.path = self.home / LEDGER_NAME
        self.opened = []
        self.addCleanup(self.close_all)

    def close_all(self):
        for item in reversed(self.opened):
            item.close()

    def ledger(self):
        item = LaunchLedger(self.path)
        self.opened.append(item)
        return item

    def manager(self, ledger=None):
        item = DurableJobManager(ledger=ledger or self.ledger())
        self.opened.append(item)
        return item

    def status_of(self, request_id):
        db = sqlite3.connect(self.path)
        try:
            row = db.execute("SELECT status FROM launches WHERE request_id=?",
                             (request_id,)).fetchone()
        finally:
            db.close()
        return row[0] if row else None

    def wait_status(self, request_id, expected, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.status_of(request_id) == expected:
                return
            time.sleep(0.05)
        self.fail(f"status stayed {self.status_of(request_id)}")


class LedgerRowTests(LedgerTestBase):
    def test_row_is_committed_before_the_spawn(self):
        seen = []
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            seen.append(self.status_of(R1))
            return real_popen(*args, **kwargs)

        manager = self.manager()
        with patch("subprocess.Popen", side_effect=spy):
            self.assertEqual(manager.start(command(R1))["status"], "RUNNING")
        self.assertEqual(seen, ["UNKNOWN"])

    def test_confirmed_end_records_terminal(self):
        manager = self.manager()
        self.assertEqual(manager.start(command(R1))["status"], "RUNNING")
        self.wait_status(R1, "TERMINAL")

    def test_failing_program_is_still_a_confirmed_end(self):
        manager = self.manager()
        manager.start(command(R1, "raise SystemExit(3)"))
        self.wait_status(R1, "TERMINAL")

    def test_no_argv_or_output_is_stored(self):
        manager = self.manager()
        manager.start(command(R1, "print('marker-secret-value')"))
        self.wait_status(R1, "TERMINAL")
        self.assertNotIn(b"marker-secret-value", self.path.read_bytes())

    def test_key_differs_between_ledger_files(self):
        first = self.ledger()
        other_path = self.home / "other.sqlite"
        second = LaunchLedger(other_path)
        self.opened.append(second)
        self.assertNotEqual(first.key_for(command(R1)), second.key_for(command(R1)))


class RestartTests(LedgerTestBase):
    def leave_unknown(self, request_id=R1, code="print('ok')"):
        earlier = self.ledger()
        self.assertEqual(earlier.reserve(command(request_id, code)), "NEW")
        earlier.close()

    def test_same_command_with_a_new_id_is_refused_before_any_window(self):
        self.leave_unknown()
        manager = self.manager()
        self.assertIs(manager.blocked_unknown_command_readonly(command(R2)), True)
        with patch("subprocess.Popen", side_effect=AssertionError("no launch")) as popen:
            result = manager.start(command(R2))
        popen.assert_not_called()
        self.assertEqual(result["status"], "DENY_DUPLICATE_UNKNOWN")
        self.assertEqual(result["error"], "PossibleDuplicateLaunchOutcomeUnknown")
        self.assertIsNone(self.status_of(R2))

    def test_a_different_command_is_not_held_back(self):
        self.leave_unknown()
        manager = self.manager()
        self.assertIs(manager.blocked_unknown_command_readonly(command(R2, "print(2)")), False)
        self.assertEqual(manager.start(command(R2, "print(2)"))["status"], "RUNNING")

    def test_used_request_id_is_refused_after_restart_even_when_finished(self):
        first = self.ledger()
        first.reserve(command(R1))
        first.finish(R1, "TERMINAL")
        first.close()
        manager = self.manager()
        with patch("subprocess.Popen", side_effect=AssertionError("no launch")):
            result = manager.start(command(R1))
        self.assertEqual(result["error"], "RequestIdUsedInEarlierRun")

    def test_same_id_for_another_command_is_a_conflict(self):
        self.leave_unknown()
        manager = self.manager()
        result = manager.start(command(R1, "print('other')"))
        self.assertEqual(result["status"], "DENY_DUPLICATE_CONFLICT")

    def test_repeating_a_finished_command_is_allowed(self):
        first = self.ledger()
        first.reserve(command(R1))
        first.finish(R1, "TERMINAL")
        first.close()
        manager = self.manager()
        self.assertIs(manager.blocked_unknown_command_readonly(command(R2)), False)
        self.assertEqual(manager.start(command(R2))["status"], "RUNNING")

    def test_same_run_may_repeat_a_command_it_is_still_running(self):
        manager = self.manager()
        manager.start(command(R1, "import time; time.sleep(1)"))
        self.assertIs(manager.blocked_unknown_command_readonly(
            command(R2, "import time; time.sleep(1)")), False)

    def test_failed_spawn_stays_unknown_and_blocks_the_next_run(self):
        ledger = self.ledger()
        manager = DurableJobManager(ledger=ledger)
        self.opened.append(manager)
        with patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(Exception):
                manager.start(command(R1))
        self.assertEqual(self.status_of(R1), "UNKNOWN")
        ledger.close()
        next_run = self.manager()
        self.assertIs(next_run.blocked_unknown_command_readonly(command(R2)), True)

    def test_busy_refusal_records_not_started_and_blocks_nothing(self):
        manager = DurableJobManager(ledger=self.ledger(), max_concurrent=1)
        self.opened.append(manager)
        self.assertEqual(manager.start(command(R1, "import time; time.sleep(3)"))["status"],
                         "RUNNING")
        self.assertEqual(manager.start(command(R2, "print(2)"))["status"], "BUSY")
        self.assertEqual(self.status_of(R2), "NOT_STARTED")

    def test_real_process_death_leaves_the_command_blocked(self):
        env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT}")
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tests" / "_launch_ledger_crash_child.py"),
             str(self.path), str(ROOT), R1],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "RUNNING")
        self.assertEqual(self.status_of(R1), "UNKNOWN")
        manager = self.manager()
        same = Command(sys.executable, ("-c", "import time; time.sleep(60)"), str(ROOT),
                       timeout=90, output_limit=1024, request_id=R2)
        self.assertIs(manager.blocked_unknown_command_readonly(same), True)
        with patch("subprocess.Popen", side_effect=AssertionError("no launch")):
            self.assertEqual(manager.start(same)["status"], "DENY_DUPLICATE_UNKNOWN")


class ConcurrencyTests(LedgerTestBase):
    def process_race(self, request_ids):
        # Bootstrap only the schema, so this tests reservation contention,
        # independently of competing first-time schema creation.
        self.ledger().close()
        signal = self.home / "reserve-go"
        env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT}",
                   PYTHONDONTWRITEBYTECODE="1")
        children = []
        try:
            for request_id in request_ids:
                child = subprocess.Popen(
                    [sys.executable, "-B", str(ROOT / "tests" / "_launch_ledger_reserve_child.py"),
                     str(self.path), str(ROOT), request_id, str(signal)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env, cwd=str(ROOT))
                children.append(child)
            self.assertNotEqual(children[0].pid, children[1].pid)
            with ThreadPoolExecutor(max_workers=2) as readers:
                ready = [readers.submit(child.stdout.readline) for child in children]
                for line in ready:
                    self.assertEqual(line.result(timeout=25).strip(), "READY")
            signal.write_text("go", encoding="ascii")
            outcomes = []
            for child in children:
                stdout, stderr = child.communicate(timeout=25)
                self.assertEqual(child.returncode, 0, stdout + stderr)
                self.assertEqual(stderr, "")
                outcomes.append(stdout.strip())
            return outcomes
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)

    def test_separate_processes_racing_on_one_id_reserve_once(self):
        self.assertCountEqual(self.process_race((R1, R1)), ["NEW", "USED"])
        self.assertEqual(self.status_of(R1), "UNKNOWN")

    def test_separate_processes_racing_on_one_command_reserve_once(self):
        self.assertCountEqual(self.process_race((R1, R2)), ["NEW", "BLOCKED"])
        self.assertCountEqual([self.status_of(R1), self.status_of(R2)], ["UNKNOWN", None])

    def race(self, *calls):
        start = threading.Barrier(len(calls))
        results = [None] * len(calls)
        errors = []

        def run(index, call):
            try:
                start.wait()
                results[index] = call()
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=run, args=item) for item in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive(), "a racing call did not finish")
        self.assertEqual(errors, [])
        return results

    def test_two_runs_racing_on_one_id_give_one_new_row(self):
        first, second = self.ledger(), self.ledger()
        outcomes = self.race(lambda: first.reserve(command(R1)),
                             lambda: second.reserve(command(R1)))
        self.assertEqual(sorted(outcomes), ["NEW", "USED"])

    def test_two_runs_racing_on_one_command_let_one_through(self):
        first, second = self.ledger(), self.ledger()
        outcomes = self.race(lambda: first.reserve(command(R1)),
                             lambda: second.reserve(command(R2)))
        self.assertEqual(sorted(outcomes), ["BLOCKED", "NEW"])
        statuses = [self.status_of(R1), self.status_of(R2)]
        self.assertEqual(sorted(str(item) for item in statuses), ["None", "UNKNOWN"])

    def test_one_run_racing_on_one_id_gives_one_new_row(self):
        ledger = self.ledger()
        outcomes = self.race(*[lambda: ledger.reserve(command(R1))] * 4)
        self.assertEqual(sorted(outcomes), ["NEW"] + ["SAME_RUN"] * 3)

    def test_racing_starts_of_one_id_spawn_one_child(self):
        manager = self.manager()
        real_popen = subprocess.Popen
        spawned = []

        def spy(*args, **kwargs):
            spawned.append(args)
            return real_popen(*args, **kwargs)

        with patch("subprocess.Popen", side_effect=spy):
            self.race(*[lambda: manager.start(command(R1))] * 4)
        self.assertEqual(len(spawned), 1)
        self.wait_status(R1, "TERMINAL")

    def test_no_end_is_recorded_after_the_ledger_closes(self):
        ledger = self.ledger()
        ledger.reserve(command(R1))
        ledger.close()
        self.assertIs(ledger.finish(R1, "TERMINAL"), False)
        self.assertEqual(self.status_of(R1), "UNKNOWN")

    def test_a_ledger_held_by_another_writer_refuses_the_launch(self):
        self.ledger().close()
        holder = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(holder.close)
        ledger = self.ledger()
        manager = DurableJobManager(ledger=ledger)
        self.opened.append(manager)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with patch("subprocess.Popen", side_effect=AssertionError("no launch")) as popen:
                result = manager.start(command(R1))
        finally:
            holder.execute("ROLLBACK")
        popen.assert_not_called()
        self.assertEqual(result["status"], "DENY_REPLAY_GUARD_FULL")


class FailClosedTests(LedgerTestBase):
    def test_garbage_empty_or_foreign_files_refuse_to_open(self):
        self.path.write_bytes(b"not a database")
        with self.assertRaises(LaunchLedgerError):
            LaunchLedger(self.path)
        self.assertEqual(self.path.read_bytes(), b"not a database")
        empty = self.home / "empty.sqlite"
        empty.write_bytes(b"")
        with self.assertRaises(LaunchLedgerError):
            LaunchLedger(empty)
        foreign = self.home / "foreign.sqlite"
        db = sqlite3.connect(foreign)
        db.execute("CREATE TABLE t (a)")
        db.commit()
        db.close()
        with self.assertRaises(LaunchLedgerError):
            LaunchLedger(foreign)

    def test_wal_sidecar_refuses_to_open(self):
        self.ledger().close()
        (self.home / (LEDGER_NAME + "-wal")).write_bytes(b"x")
        with self.assertRaises(LaunchLedgerError):
            LaunchLedger(self.path)

    def test_closed_ledger_denies_with_no_launch(self):
        ledger = self.ledger()
        manager = DurableJobManager(ledger=ledger)
        self.opened.append(manager)
        ledger.close()
        self.assertEqual(manager.blocked_unknown_command_readonly(command(R1)),
                         "GUARD_UNAVAILABLE")
        with patch("subprocess.Popen", side_effect=AssertionError("no launch")):
            result = manager.start(command(R1))
        self.assertEqual(result["status"], "DENY_REPLAY_GUARD_FULL")

    def test_write_failure_denies_with_no_launch(self):
        ledger = self.ledger()
        manager = DurableJobManager(ledger=ledger)
        self.opened.append(manager)
        with patch.object(LaunchLedger, "reserve", side_effect=LaunchLedgerError("disk")), \
                patch("subprocess.Popen", side_effect=AssertionError("no launch")):
            result = manager.start(command(R1))
        self.assertEqual(result["status"], "DENY_REPLAY_GUARD_FULL")

    def test_manager_requires_a_real_ledger(self):
        with self.assertRaises(TypeError):
            DurableJobManager(ledger=object())


class LocalClearTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        earlier = self.ledger()
        earlier.reserve(command(R1))
        earlier.close()
        self.current = self.ledger()
        self.lines = []

    def run_command(self, resolve_id, answer="yes"):
        return run_ledger_command(self.current, resolve_id, confirm=lambda prompt: answer,
                                  write=self.lines.append)

    def test_list_shows_id_and_program_with_no_arguments(self):
        self.assertEqual(self.run_command(None), 0)
        text = "\n".join(self.lines)
        self.assertIn(R1, text)
        self.assertIn(Path(sys.executable).name, text)
        self.assertNotIn("print('ok')", text)

    def test_typed_yes_clears_and_allows_the_command(self):
        self.assertEqual(self.run_command(R1), 0)
        self.assertEqual(self.status_of(R1), "RESOLVED")
        self.assertIs(self.current.blocked(command(R2)), False)

    def test_anything_but_yes_changes_nothing(self):
        for answer in ("", "y", "YES", "no"):
            with self.subTest(answer=answer):
                self.assertEqual(self.run_command(R1, answer), 2)
                self.assertEqual(self.status_of(R1), "UNKNOWN")

    def test_input_that_ends_changes_nothing(self):
        def ended(prompt):
            raise EOFError

        code = run_ledger_command(self.current, R1, confirm=ended, write=self.lines.append)
        self.assertEqual(code, 2)
        self.assertEqual(self.status_of(R1), "UNKNOWN")

    def test_unknown_id_changes_nothing(self):
        self.assertEqual(self.run_command(R3), 2)
        self.assertEqual(self.status_of(R1), "UNKNOWN")

    def test_clearing_cannot_be_reached_over_the_wire(self):
        self.assertFalse({name for name in _SCHEMAS if "resolve" in name or "ledger" in name})


if __name__ == "__main__":
    unittest.main()
