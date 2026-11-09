import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager
from sora_commander_next.stdio_main import owner_journal_path
import os


def _storage_ready():
    """The Owner storage exists only after the first Host or Broker start."""
    local = os.environ.get("LOCALAPPDATA")
    return bool(local) and (Path(local) / "SoraCommanderNext").is_dir()


class CrossManagerRecoveryTests(unittest.TestCase):
    def test_completed_snapshot_visible_to_new_manager(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'journal.json'
            first = JobManager(journal_path=path)
            try:
                cmd = Command(sys.executable, ('-c', "print('OK')"), str(Path(__file__).resolve().parents[1]), timeout=3)
                sid = first.start(cmd)['session_id']
                self.assertEqual(first.wait(sid)['status'], 'COMPLETED')
            finally:
                first.close()
            second = JobManager(journal_path=path)
            try:
                self.assertEqual(second.previous_sessions()['sessions'][0]['status'], 'COMPLETED')
                self.assertEqual(second.list_sessions()['count'], 0)
            finally:
                second.close()

    @unittest.skipUnless(os.name == "nt" and _storage_ready(),
                         "Windows Owner storage, created on the first start")
    def test_fixed_owner_journal_location(self):
        import os
        self.assertEqual(owner_journal_path(), Path(os.environ['LOCALAPPDATA']) / 'SoraCommanderNext' / 'owner-session-journal.json')
