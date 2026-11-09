"""An unknown session gets the same error name with and without the interactive manager. Nothing is started."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.host import Host
from sora_commander_next.jobs import UnknownSession

SESSION = "a" * 48


class UnknownSessionTests(unittest.TestCase):
    def host(self, interactive):
        host = Host()
        self.addCleanup(host.jobs.close)
        if interactive:
            host._dev_interactive_jobs = SimpleNamespace(_lock=threading.Lock(), _jobs={}, _stdin_pending={})
        return host

    def test_every_read_gives_unknown_session(self):
        for interactive in (False, True):
            host = self.host(interactive)
            for call in (lambda: host.read_process_output(SESSION), lambda: host.await_process_output(SESSION, wait_seconds=0),
                         lambda: host.stop_process(SESSION)):
                with self.subTest(interactive=interactive):
                    self.assertEqual(call(), {"status": "ERROR", "error": "UnknownSession"})

    def test_the_manager_lookup_raises_it(self):
        with self.assertRaises(UnknownSession):
            self.host(True)._manager_for_session(SESSION)

    def test_a_malformed_id_is_still_a_value_error(self):
        with self.assertRaises(ValueError):
            self.host(True)._manager_for_session("not-a-session")

    def test_a_known_session_is_still_found(self):
        host = self.host(True)
        job = object()
        host._dev_interactive_jobs._jobs[SESSION] = job
        self.assertIs(host._manager_for_session(SESSION), host._dev_interactive_jobs)

    def test_a_session_in_both_managers_is_still_refused(self):
        host = self.host(True)
        host._dev_interactive_jobs._jobs[SESSION] = object()
        host.jobs._jobs[SESSION] = object()
        self.addCleanup(host.jobs._jobs.clear)
        with self.assertRaises(PermissionError):
            host._manager_for_session(SESSION)


if __name__ == "__main__":
    unittest.main()
