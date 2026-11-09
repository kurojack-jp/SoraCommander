"""Run named test modules with the real-dialog guard on for the whole process.

    .venv\\Scripts\\python.exe -B tests\\run_with_real_dialog_guard.py tests.test_phase9b_owner_ipc

Exits nonzero on any test failure or any attempt to start a real Owner dialog.
Use this for test runs before and around a broker change.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for location in (ROOT, ROOT / "src", ROOT / "tests"):
    sys.path.insert(0, str(location))

from tests._no_real_dialog import RealDialogGuard


def main(names):
    if not names:
        raise SystemExit("Name at least one test module")
    guard = RealDialogGuard().start()
    try:
        suite = unittest.TestLoader().loadTestsFromNames(names)
        result = unittest.TextTestRunner(verbosity=0).run(suite)
    finally:
        guard.stop()
    print("REAL_DIALOG_ATTEMPTS", len(guard.attempts), guard.attempts, flush=True)
    return 0 if result.wasSuccessful() and not guard.attempts else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
