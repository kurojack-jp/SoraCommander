from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / "src" / "sora_commander_next"

class OwnerAlertWiringTests(unittest.TestCase):
    def test_all_owner_dialogs_schedule_one_alert_per_mainloop(self):
        for filename, expected in (("owner_approval_dialog.py", 1),
                                   ("owner_approval_dialog_v2.py", 1),
                                   ("file_edits.py", 2)):
            with self.subTest(filename=filename):
                src=(ROOT/filename).read_text(encoding="utf-8")
                self.assertEqual(src.count("root.mainloop()"),expected)
                self.assertEqual(src.count("root.after(100, alert_review)"),expected)
                self.assertEqual(src.count("winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)"),expected)
                self.assertIn("root.bell()",src)
    def test_sound_never_calls_approval_decision(self):
        for filename in ("owner_approval_dialog.py","owner_approval_dialog_v2.py","file_edits.py"):
            with self.subTest(filename=filename):
                src=(ROOT/filename).read_text(encoding="utf-8")
                for snippet in src.split("    def alert_review():")[1:]:
                    body=snippet.split("root.after(100, alert_review)",1)[0]
                    self.assertNotIn("decide(",body)
                    self.assertNotIn("APPROVE",body)
