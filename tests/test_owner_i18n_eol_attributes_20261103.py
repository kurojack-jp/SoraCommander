"""SHA-pinned Owner UI locale JSON must survive a Windows Git checkout byte-exact.

With core.autocrlf=true and no attribute, checkout rewrites LF as CRLF and the
pinned digests in owner_review_i18n / owner_command_caution fail closed.
Read-only: asks Git for attributes, never changes configuration or files.
"""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("en.json", "ja.json", "caution_en.json", "caution_ja.json")


class OwnerLocaleEolAttributeTests(unittest.TestCase):
    def test_pinned_locale_json_is_not_eol_converted(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("git not available")
        paths = ["src/sora_commander_next/i18n/" + name for name in LOCALES]
        run = subprocess.run([git, "check-attr", "text", "--", *paths],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        values = dict(line.rsplit(": text: ", 1) for line in run.stdout.splitlines())
        self.assertEqual(values, {path: "unset" for path in paths})

    def test_pinned_locale_json_bytes_are_lf_only(self):
        for name in LOCALES:
            with self.subTest(name=name):
                data = (ROOT / "src" / "sora_commander_next" / "i18n" / name).read_bytes()
                self.assertNotIn(b"\r", data)


if __name__ == "__main__":
    unittest.main()
