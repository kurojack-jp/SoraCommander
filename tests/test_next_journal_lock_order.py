"""Opt-in journal lock order and concurrent startup/exit; no restart replay.

Uses real disposable Windows Python child jobs. No Owner grant, no MCP,
no protected filesystem, no task/service or remote listener.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
from threading import Lock
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager
from sora_commander_next.session_journal import (
    read_previous_snapshot, write_snapshot)


class JournalLockOrderTests(unittest.TestCase):
    def test_both_start_and_finalization_hold_manager_before_journal(self):
        with tempfile.TemporaryDirectory(prefix="next-journal-lock-") as tmp:
            path = Path(tmp) / "jobs.json"
            manager = JobManager(
                journal_path=path, max_concurrent=4, capacity=8)
            original = write_snapshot
            seen = []
            seen_lock = Lock()

            def inspected(journal_path, entries, *, protected_stage=False):
                # CPython RLock diagnostic; proves manager -> journal order
                # for both start and the asynchronous supervisor path.
                held = manager._lock._is_owned()
                with seen_lock:
                    seen.append((held, tuple(x["status"] for x in entries)))
                if not held:
                    raise RuntimeError("journal called without manager lock")
                return original(journal_path, entries, protected_stage=protected_stage)

            def submit(index):
                command = Command(
                    sys.executable,
                    ("-I", "-S", "-c",
                     "import time;time.sleep(.2);print('EXIT')"),
                    tmp,
                    timeout=6, output_limit=4096,
                    request_id="journal-test-" + str(index))
                return manager.start(command)

            try:
                with patch("sora_commander_next.session_journal.write_snapshot",
                           side_effect=inspected):
                    with ThreadPoolExecutor(max_workers=4) as executor:
                        starts = list(executor.map(submit, range(4)))
                    self.assertTrue(all(item["status"] == "RUNNING"
                                        for item in starts), starts)
                    completed = [
                        manager.wait(item["session_id"]) for item in starts
                    ]
                    # done may become visible before the supervisor finishes
                    # its final journal write; wait for that writer explicitly.
                    with manager._lock:
                        threads = tuple(job.thread for job in
                                        manager._jobs.values())
                    for thread in threads:
                        thread.join(timeout=3)
                        self.assertFalse(thread.is_alive(),
                                         "terminal journal writer still active")
                self.assertEqual([item["status"] for item in completed],
                                 ["COMPLETED"] * 4)
                self.assertTrue(seen)
                self.assertTrue(all(owned for owned, _ in seen))
                self.assertTrue(any("RUNNING" in states
                                    for _, states in seen))
                self.assertTrue(any("COMPLETED" in states
                                    for _, states in seen))
                # Metadata only; old RUNNING is never interpreted as
                # permission to re-launch or grant a command.
                previous = read_previous_snapshot(path)
                self.assertEqual(previous["count"], 4)
                self.assertTrue(all(row["status"] == "COMPLETED"
                                    for row in previous["sessions"]))
                self.assertNotIn("EXIT", path.read_text(encoding="utf-8"))
            finally:
                manager.close()

    def test_disabled_journal_does_not_write_or_lock_external_path(self):
        manager = JobManager()
        try:
            with patch("sora_commander_next.session_journal.write_snapshot"
                       ) as write:
                manager._snapshot_journal()
                write.assert_not_called()
            self.assertEqual(manager.previous_sessions(),
                             {"sessions": [], "count": 0})
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
