"""Journal write faults must never produce an untracked running child."""
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager, LaunchNotStarted

ROOT=Path(__file__).resolve().parents[1]

class JournalFaultTests(unittest.TestCase):
    def test_start_journal_failure_cleans_owned_child(self):
        with tempfile.TemporaryDirectory() as t:
            jobs=JobManager(journal_path=Path(t)/"journal.json")
            try:
                request=Command(sys.executable,("-c","import time;time.sleep(5)"),str(ROOT),timeout=6)
                children = []
                original = subprocess.Popen
                fault = OSError("test-only disk fault")
                def spawn(*args, **kwargs):
                    self.assertEqual(kwargs["creationflags"], 0x00000004)
                    child = original(*args, **kwargs)
                    children.append(child)
                    return child
                with patch("sora_commander_next.jobs.subprocess.Popen", side_effect=spawn), patch(
                        "sora_commander_next.jobs.WindowsJob.attach_and_resume") as resume, patch(
                        "sora_commander_next.session_journal.write_snapshot", side_effect=fault):
                    with self.assertRaises(LaunchNotStarted) as raised:
                        jobs.start(request)
                    resume.assert_not_called()
                self.assertEqual(raised.exception.phase, "spawned_suspended")
                self.assertEqual(raised.exception.request_id, request.request_id)
                self.assertIs(raised.exception.__cause__, fault)
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                self.assertTrue(children[0].stdout.closed)
                self.assertTrue(children[0].stderr.closed)
                self.assertEqual(jobs.list_sessions()["count"],0)
                self.assertFalse(jobs._jobs)
                self.assertNotIn(request.request_id, jobs._request_sessions)
                self.assertNotIn(request.request_id, jobs._uncertain_request_ids)
            finally:
                jobs.close()

    def test_terminal_journal_failure_does_not_relabel_success(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/"journal.json"
            jobs=JobManager(journal_path=path)
            try:
                request=Command(sys.executable,("-c","import time;time.sleep(.15);print('FINISHED')"),str(ROOT),timeout=4)
                from threading import Event
                started = Event()
                original = __import__("sora_commander_next.session_journal",fromlist=["write_snapshot"]).write_snapshot
                def injected(path_arg, entries, *, protected_stage=False):
                    if any(e["status"] != "RUNNING" for e in entries):
                        raise OSError("test-only final fault")
                    return original(path_arg, entries, protected_stage=protected_stage)
                with patch("sora_commander_next.session_journal.write_snapshot",side_effect=injected):
                    sid=jobs.start(request)["session_id"]
                    result=jobs.wait(sid)
                self.assertEqual(result["status"],"COMPLETED")
                self.assertEqual(result["stdout"].strip(),"FINISHED")
            finally:
                jobs.close()
