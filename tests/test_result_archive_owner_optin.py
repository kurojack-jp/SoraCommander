"""Owner archive startup opt-in contract with all native operations mocked.

No real Owner identity verification, protected Windows files, GUI, pipes,
service, grant, job process or network listener is executed by this test.
"""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next import stdio_main


class OwnerResultArchiveOptInTests(unittest.TestCase):
    def run_config(self, *, archive, journal):
        with tempfile.TemporaryDirectory(prefix="next-owner-output-optin-") as tmp:
            base = Path(tmp) / "owner-session-journal.json"
            calls = []
            class FakeGuard:
                sid = "FAKE_OWNER_SID"
                def verify(self):
                    return True
            class FakeHost:
                def __init__(self, **kwargs):
                    self.config = kwargs
                def attach_owner_review_local(self, reviewer):
                    self.reviewer = reviewer
            def fake_job_manager(**kwargs):
                calls.append(kwargs)
                return object()
            flags = {
                "SORA_NEXT_OWNER_REVIEW_PILOT": "1",
                "SORA_NEXT_JOURNAL_PILOT": "1" if journal else "0",
                "SORA_NEXT_RESULT_ARCHIVE_PILOT": "1" if archive else "0",
                "SORA_NEXT_OWNER_PROFILE": "tests",
                "SORA_NEXT_EDIT_PILOT": "0",
                "SORA_NEXT_CREATE_PILOT": "0",
            }
            # Isolate archive startup from unrelated Owner configuration.
            # A configured OGF bridge validates its own protected directory
            # through owner_journal_path twice; it never enables the archive.
            # Loader/storage behavior is outside this mocked archive contract.
            with patch.dict(os.environ, flags), \
                 patch("sora_commander_next.task_presets.load_task_presets",
                       return_value=()), \
                 patch("sora_commander_next.ogf_cue.load_ogf_bridge",
                       return_value=None), \
                 patch.object(stdio_main, "ensure_owner_storage"), \
                 patch.object(stdio_main, "owner_journal_path",
                              return_value=base) as owner_path, \
                 patch.object(stdio_main, "Host", FakeHost), \
                 patch.object(stdio_main, "serve_stdio") as serve, \
                 patch("sora_commander_next.windows_owner.OwnerSessionGuard",
                       FakeGuard), \
                 patch("sora_commander_next.windows_owner_controller."
                       "WindowsOwnerController", return_value=object()), \
                 patch("sora_commander_next.owner_review_dispatch."
                       "OwnerReviewDispatcher", return_value=object()), \
                 patch("sora_commander_next.jobs.JobManager",
                       side_effect=fake_job_manager):
                if archive and not journal:
                    with self.assertRaises(RuntimeError):
                        stdio_main.main()
                    serve.assert_not_called()
                    owner_path.assert_not_called()
                else:
                    stdio_main.main()
                    serve.assert_called_once()
                    if journal:
                        owner_path.assert_called_once()
            return calls

    def test_output_archive_off_does_not_activate_file_storage(self):
        calls = self.run_config(archive=False, journal=True)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["output_archive_path"])
        self.assertTrue(calls[0]["protected_stage"])
        self.assertEqual(calls[0]["journal_path"].name,
                         "owner-session-journal.json")

    def test_output_archive_requires_metadata_journal(self):
        calls = self.run_config(archive=True, journal=False)
        self.assertEqual(calls, [])

    def test_explicit_owner_opt_in_sets_both_fixed_sibling_paths(self):
        calls = self.run_config(archive=True, journal=True)
        self.assertEqual(len(calls), 1)
        options = calls[0]
        self.assertTrue(options["protected_stage"])
        self.assertEqual(options["journal_path"].name,
                         "owner-session-journal.json")
        self.assertEqual(options["output_archive_path"].name,
                         "owner-output-archive.json")
        self.assertEqual(options["journal_path"].parent,
                         options["output_archive_path"].parent)


if __name__ == "__main__":
    unittest.main()
