"""Session listing lifecycle and isolation on disposable subprocesses."""
import sys
import time
from pathlib import Path
from unittest import TestCase
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager

ROOT=Path(__file__).resolve().parents[1]
class SessionLifecycle(TestCase):
    def test_running_completed_and_expiry(self):
        jobs=JobManager(retention=.15)
        self.addCleanup(jobs.close)
        self.assertEqual(jobs.list_sessions()["count"],0)
        cmd=Command(sys.executable,("-c","import time;print('READY',flush=True);time.sleep(.5)"),str(ROOT),timeout=3)
        started=jobs.start(cmd)
        sid=started["session_id"]
        current=jobs.list_sessions()
        self.assertEqual(current["count"],1)
        entry=current["sessions"][0]
        self.assertEqual(entry["session_id"],sid)
        self.assertEqual(entry["status"],"RUNNING")
        self.assertNotIn("argv",str(entry))
        self.assertNotIn("READY",str(entry))
        result=jobs.wait(sid)
        self.assertEqual(result["status"],"COMPLETED")
        done=jobs.list_sessions()["sessions"][0]
        self.assertEqual(done["status"],"COMPLETED")
        self.assertEqual(done["exit_code"],0)
        self.assertEqual(done["captured_bytes"],result["captured_bytes"])
        time.sleep(.2)
        self.assertEqual(jobs.list_sessions(),{"sessions":[],"count":0})

    def test_cancelled_and_isolation(self):
        jobs=JobManager()
        self.addCleanup(jobs.close)
        first=jobs.start(Command(sys.executable,("-c","import time;time.sleep(3)"),str(ROOT),timeout=5))["session_id"]
        second=jobs.start(Command(sys.executable,("-c","print('SECOND')"),str(ROOT),timeout=5))["session_id"]
        self.assertEqual({x["session_id"] for x in jobs.list_sessions()["sessions"]},{first,second})
        self.assertEqual(jobs.stop(first)["status"],"CANCELLING")
        self.assertEqual(jobs.wait(first)["status"],"CANCELLED")
        self.assertEqual(jobs.wait(second)["status"],"COMPLETED")
        self.assertEqual({x["status"] for x in jobs.list_sessions()["sessions"]},{"CANCELLED","COMPLETED"})
