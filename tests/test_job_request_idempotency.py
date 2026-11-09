"""Real Windows JobManager: retained session replay never respawns a command.

This is a subordinate defense. After retention/prune, in-process consumed IDs
remain tombstoned. Cross-process and post-restart uniqueness still require
an independent protected Broker ledger.
"""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from threading import Barrier, Thread
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager, LaunchOutcomeUnknown


class RequestIdempotencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sora-job-replay-")
        self.addCleanup(temporary.cleanup)
        self.cwd = str(Path(temporary.name).resolve())
        self.marker = Path(self.cwd) / "one-child.txt"
        self.manager = JobManager(max_concurrent=2, capacity=5, retention=60)
        self.addCleanup(self.manager.close)

    def make_command(self, *, request_id="identical", text="created"):
        payload = "from pathlib import Path; Path('one-child.txt').open('a').write(" + repr(text + "\n") + ")"
        return Command(
            sys.executable, ("-I", "-S", "-c", payload), self.cwd,
            timeout=4, output_limit=4096, request_id=request_id)

    def test_repeated_identical_request_reuses_one_windows_child(self):
        request = self.make_command()
        first = self.manager.start(request)
        second = self.manager.start(request)
        self.assertEqual(first["status"], "RUNNING")
        self.assertEqual(second["status"], "RUNNING")
        self.assertTrue(second["reused_session"])
        self.assertEqual(first["session_id"], second["session_id"])
        done = self.manager.wait(first["session_id"])
        self.assertEqual(done["status"], "COMPLETED", done)
        self.assertEqual(self.marker.read_text(), "created\n")
        third = self.manager.start(request)
        self.assertEqual(third["session_id"], first["session_id"])
        self.assertEqual(self.marker.read_text(), "created\n")

    def test_same_id_with_modified_command_or_limits_denied_without_spawn(self):
        request = self.make_command()
        first = self.manager.start(request)
        variants = (
            replace(request, argv=("-V",)),
            replace(request, timeout=3),
            replace(request, output_limit=5000),
            replace(request, cwd=str(ROOT)),
        )
        for changed in variants:
            with self.subTest(changed=changed):
                reply = self.manager.start(changed)
                self.assertEqual(reply["status"], "DENY_DUPLICATE_CONFLICT")
                self.assertEqual(reply["error"], "RequestIdConflict")
        self.assertEqual(self.manager.wait(first["session_id"])["status"],
                         "COMPLETED")
        self.assertEqual(self.marker.read_text(), "created\n")
        self.assertEqual(self.manager.list_sessions()["count"], 1)

    def test_concurrent_threads_same_id_start_one_child(self):
        request = self.make_command()
        ready = Barrier(5)
        outputs = []
        def worker():
            ready.wait()
            outputs.append(self.manager.start(request))
        threads = [Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        ready.wait()
        for thread in threads:
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(outputs), 4)
        self.assertEqual(len({o["session_id"] for o in outputs}), 1)
        self.assertEqual(sum(o.get("reused_session") is True for o in outputs), 3)
        self.assertEqual(self.manager.wait(outputs[0]["session_id"])["status"],
                         "COMPLETED")
        self.assertEqual(self.marker.read_text(), "created\n")

    def test_retention_prune_cannot_relaunch_same_request_id(self):
        self.manager.retention = .03
        original = self.make_command(request_id="expired-but-consumed")
        first = self.manager.start(original)
        self.assertEqual(self.manager.wait(first["session_id"])["status"],
                         "COMPLETED")
        self.assertEqual(self.marker.read_text(), "created\n")
        time.sleep(.07)
        self.assertEqual(self.manager.list_sessions()["count"], 0)
        # Observing an expired job does not imply it never ran.
        self.assertEqual(self.manager.request_status_readonly(
            original.request_id)["status"], "OUTCOME_UNKNOWN")
        repeated = self.manager.start(original)
        self.assertEqual(repeated["status"], "DENY_DUPLICATE_EXPIRED")
        self.assertEqual(repeated["error"], "RequestIdRetired")
        changed = self.manager.start(replace(original, argv=("-V",)))
        self.assertEqual(changed["status"], "DENY_DUPLICATE_EXPIRED")
        self.assertEqual(self.marker.read_text(), "created\n")
        # Distinct new jobs remain possible when the bounded guard has room.
        fresh = self.manager.start(self.make_command(
            request_id="a-fresh-id", text="next"))
        self.assertEqual(self.manager.wait(fresh["session_id"])["status"],
                         "COMPLETED")
        self.assertEqual(self.marker.read_text(), "created\nnext\n")

    def test_full_retired_guard_blocks_fresh_start_not_replay(self):
        self.manager.retention = .03
        done = self.manager.start(self.make_command(request_id="retired-id"))
        self.manager.wait(done["session_id"])
        time.sleep(.07)
        # Test reduced capacity; real guard holds 65,536 consumed IDs.
        with patch("sora_commander_next.jobs._MAX_RETIRED_REQUESTS", 1):
            result = self.manager.start(self.make_command(request_id="new-id"))
            self.assertEqual(result["status"], "DENY_REPLAY_GUARD_FULL")
            self.assertEqual(result["error"], "ProtectedBrokerLedgerRequired")
            spent = self.manager.start(self.make_command(request_id="retired-id"))
            self.assertEqual(spent["status"], "DENY_DUPLICATE_EXPIRED")
        self.assertEqual(self.manager.list_sessions()["count"], 0)
        self.assertEqual(self.marker.read_text(), "created\n")

    def test_full_replay_guard_still_allows_existing_running_session_lookup(self):
        self.manager.retention = .03
        active_cmd = Command(
            sys.executable, ("-I", "-S", "-c", "import time;time.sleep(.7)"),
            self.cwd, timeout=3, output_limit=4096, request_id="still-active")
        active = self.manager.start(active_cmd)
        done = self.manager.start(self.make_command(request_id="now-expired"))
        self.assertEqual(self.manager.wait(done["session_id"])["status"],
                         "COMPLETED")
        time.sleep(.07)
        with patch("sora_commander_next.jobs._MAX_RETIRED_REQUESTS", 1):
            repeated = self.manager.start(active_cmd)
            self.assertEqual(repeated["session_id"], active["session_id"])
            self.assertTrue(repeated["reused_session"])
            new = self.manager.start(self.make_command(request_id="new-one"))
            self.assertEqual(new["status"], "DENY_REPLAY_GUARD_FULL")
        self.assertEqual(self.manager.wait(active["session_id"])["status"],
                         "COMPLETED")

    def test_failure_after_windows_resume_never_replays_uncertain_request(self):
        import time as _time
        req = self.make_command(request_id="resume-exception", text="ran")
        # Simulate supervision failing AFTER the Windows Job Object has
        # resumed the process. Marker may or may not have been written by
        # the time failure is observed; neither allows a fresh spawn.
        def broken_start():
            _time.sleep(.20)
            raise RuntimeError("simulated monitor start failure")
        with patch("sora_commander_next.jobs.Thread.start",
                   side_effect=broken_start):
            with self.assertRaises(RuntimeError):
                self.manager.start(req)
        before = self.marker.read_text() if self.marker.exists() else ""
        self.assertIn(before, ("", "ran\n"))
        self.assertEqual(
            self.manager.request_status_readonly(req.request_id)["status"],
            "OUTCOME_UNKNOWN")
        with patch("sora_commander_next.jobs.subprocess.Popen",
                   side_effect=AssertionError("must not respawn")) as spawn:
            self.assertEqual(
                self.manager.start(req)["status"], "DENY_DUPLICATE_UNKNOWN")
            changed = self.manager.start(replace(req, argv=("-V",)))
            self.assertEqual(changed["status"], "DENY_DUPLICATE_UNKNOWN")
            spawn.assert_not_called()
        self.assertEqual(self.manager.list_sessions()["count"], 0)

    def test_failed_spawn_is_consumed_even_when_no_pid_returned(self):
        req = self.make_command(request_id="spawn-failed")
        with patch("sora_commander_next.jobs.subprocess.Popen",
                   side_effect=OSError("ambiguous OS spawn")) as spawn:
            # Launch classification changed in the OUTCOME_UNKNOWN fix:
            # the spawn failure is now a LaunchOutcomeUnknown caused by
            # the OSError. No-replay behavior below is unchanged.
            with self.assertRaises(LaunchOutcomeUnknown) as caught:
                self.manager.start(req)
            self.assertEqual(caught.exception.phase, "spawning")
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertEqual(self.manager.start(req)["status"],
                             "DENY_DUPLICATE_UNKNOWN")
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(self.manager.request_status_readonly(
            req.request_id)["status"], "OUTCOME_UNKNOWN")
        self.assertFalse(self.marker.exists())

    def test_busy_rejection_does_not_mark_new_id_as_attempted(self):
        running = Command(
            sys.executable, ("-I", "-S", "-c", "import time;time.sleep(.55)"),
            self.cwd, timeout=3, output_limit=4096, request_id="blocking")
        small = JobManager(max_concurrent=1, capacity=3, retention=60)
        self.addCleanup(small.close)
        started = small.start(running)
        fresh = self.make_command(request_id="after-busy")
        self.assertEqual(small.start(fresh)["status"], "BUSY")
        self.assertEqual(small.request_status_readonly(
            fresh.request_id)["status"], "UNKNOWN")
        self.assertEqual(small.wait(started["session_id"])["status"],
                         "COMPLETED")
        new = small.start(fresh)
        self.assertEqual(new["status"], "RUNNING")
        self.assertEqual(small.wait(new["session_id"])["status"], "COMPLETED")

    def test_different_id_remains_distinct_job(self):
        first = self.manager.start(self.make_command(request_id="one"))
        second = self.manager.start(self.make_command(request_id="two"))
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.manager.wait(first["session_id"])
        self.manager.wait(second["session_id"])
        self.assertEqual(self.marker.read_text().count("created\n"), 2)


if __name__ == "__main__":
    unittest.main()
