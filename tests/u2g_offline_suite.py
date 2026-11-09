"""U2-g bounded offline suite. Run with .venv Python and PYTHONPATH=src;tests.

The current sandbox denies access inside TemporaryDirectory-created folders.
Do not change ACLs or retry those blocked fixtures. Keep them explicit skips,
and prohibit subprocess/system launches for every test that is actually run.
This runner does not certify skipped fixture tests or real OS acceptance.
"""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

FOCUSED = (
    "test_offline_b01_receipt_adjudication",
    "test_offline_b01_broker_actor",
)
BROAD = (*FOCUSED,
    "test_offline_b01_full_binding", "test_offline_b01_execution_profile",
    "test_offline_b01_service_fixture", "test_offline_b01_handoff_harness",
    "test_offline_ledger_lifecycle_contract", "test_offline_grant_contract",
    "test_approval_v2_contract", "test_phase9f_inventory",
    "test_release_broker_import_audit",
)
BLOCKED_MODULES = frozenset((
    "test_offline_b01_service_fixture", "test_offline_b01_handoff_harness",
    "test_phase9f_inventory",
))
FIXTURE_REASON = "U2G_ENV_BLOCKED: TemporaryDirectory contents denied; no ACL repair/retry"
CHILD_REASON = "U2G_SCOPE_SKIP: real child-process launch forbidden by Owner"


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in ("focused", "broad"):
        raise SystemExit("expected focused or broad")
    root = Path(__file__).resolve().parents[1]
    # Only a repo-local target; never the external OS Temp directory.
    tempfile.tempdir = str(root / ".tmp-u2g-offline")
    suite = unittest.TestSuite()
    for name in FOCUSED if mode == "focused" else BROAD:
        module = importlib.import_module(name)
        for value in vars(module).values():
            if not isinstance(value, type) or not issubclass(value, unittest.TestCase):
                continue
            blocked_fixture = (name in BLOCKED_MODULES and not (
                name == "test_phase9f_inventory" and
                value.__name__ == "U2GOfflineInventoryTests"))
            if blocked_fixture or (
                    name == "test_release_broker_import_audit" and
                    value.__name__ == "BrokerImportAuditTests"):
                # Mark individual methods so the child-launch exclusion is
                # distinguishable from the filesystem environment block.
                for method in unittest.defaultTestLoader.getTestCaseNames(value):
                    reason = (CHILD_REASON if method == "test_staged_host_import_smoke_is_locally_deny_only"
                              else FIXTURE_REASON)
                    setattr(value, method, unittest.skip(reason)(getattr(value, method)))
            if name == "test_approval_v2_contract":
                method = "test_script_edit_does_not_invalidate_surface_grant"
                setattr(value, method, unittest.skip(FIXTURE_REASON)(getattr(value, method)))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    with patch("subprocess.Popen", side_effect=AssertionError(CHILD_REASON)), \
            patch.object(os, "system", side_effect=AssertionError(CHILD_REASON)):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    reasons = {}
    for _, reason in result.skipped:
        reasons[reason] = reasons.get(reason, 0) + 1
    print(dict(mode=mode, run=result.testsRun,
               passed=result.testsRun - len(result.skipped) - len(result.failures) - len(result.errors),
               skipped=len(result.skipped), failures=len(result.failures), errors=len(result.errors),
               skip_reasons=reasons))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
