"""SCN16 T2 child environment allowlist: synthetic values only."""
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sora_commander_next.jobs import _child_environment


class ChildEnvironmentTests(unittest.TestCase):
    def test_only_explicit_runtime_values_survive(self):
        fake = {"SystemRoot": r"C:\Windows", "PATH": r"C:\bin",
                "TEMP": r"C:\scratch", "USERPROFILE": r"C:\owner",
                "CONTROL_PLANE_API_KEY": "DUMMY_DO_NOT_USE",
                "AWS_SECRET_ACCESS_KEY": "DUMMY_DO_NOT_USE",
                "GIT_SSH_COMMAND": "dummy", "UNRELATED": "x"}
        child = _child_environment(fake)
        self.assertEqual(child, {k: fake[k] for k in
                         ("SystemRoot", "PATH", "TEMP", "USERPROFILE")})
        self.assertEqual(fake["CONTROL_PLANE_API_KEY"], "DUMMY_DO_NOT_USE")

    def test_casefold_whitelist_and_no_mutation(self):
        source = {"systemroot": "win", "path": "bin", "CONTROL_plane_api_key": "fake"}
        self.assertEqual(_child_environment(source),
                         {"systemroot": "win", "path": "bin"})
        self.assertEqual(len(source), 3)


if __name__ == "__main__":
    unittest.main()
